"""Hybrid retrieval: dense vectors, full text, and reciprocal rank fusion.

**Tenant scoping.** Every query carries `org_id = :org` in its WHERE clause.
That is what guarantees isolation, and it is guaranteed by SQL semantics, not by
index behaviour — Postgres never returns a row failing a predicate.

A note on the blueprint's phrasing: it claimed the filter is "applied before the
ANN scan", conflating two separate things. With an HNSW index, pgvector may scan
the index first and apply the predicate afterwards. That is a *recall* concern
(a query can come back with fewer than k rows), never a *correctness* one — no
cross-tenant row can be returned either way. `hnsw.iterative_scan` addresses the
recall side; the security property never depended on it. See ADR-0011.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()

# Reciprocal rank fusion. k damps the influence of top ranks so one method
# cannot dominate; 60 is the value from the original RRF paper and a sane default.
RRF_K = 60


@dataclass(frozen=True)
class Hit:
    chunk_id: uuid.UUID
    resume_id: uuid.UUID
    chunk_index: int
    text: str
    char_start: int
    char_end: int
    section: str | None
    score: float
    vector_rank: int | None = None
    lexical_rank: int | None = None


async def vector_search(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    query_vector: list[float],
    limit: int = 20,
    resume_ids: list[uuid.UUID] | None = None,
) -> list[Hit]:
    # Iterative scan keeps recall usable when the tenant predicate filters out
    # most of what the HNSW index returned (pgvector >= 0.8).
    await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))

    # Fixed SQL literal. The optional resume filter is a bound parameter, not an
    # interpolated fragment, so nothing is ever built into a query by string.
    rows = (
        await session.execute(
            text(
                """
                SELECT id, resume_id, chunk_index, text_redacted, char_start, char_end,
                       section, 1 - (embedding <=> :qv) AS similarity
                  FROM resume_chunks
                 WHERE org_id = :org
                   AND embedding IS NOT NULL
                   AND (NOT :filter_resumes OR resume_id = ANY(:resume_ids))
                 ORDER BY embedding <=> :qv
                 LIMIT :lim
                """
            ),
            {
                "org": org_id,
                "qv": str(query_vector),
                "lim": limit,
                "filter_resumes": bool(resume_ids),
                "resume_ids": [str(r) for r in (resume_ids or [])],
            },
        )
    ).all()

    return [
        Hit(
            chunk_id=r[0],
            resume_id=r[1],
            chunk_index=r[2],
            text=r[3],
            char_start=r[4],
            char_end=r[5],
            section=r[6],
            score=float(r[7]),
            vector_rank=i + 1,
        )
        for i, r in enumerate(rows)
    ]


# Words that carry no retrieval signal in a job description. Not a full stopword
# list — Postgres's english config already strips those — just the recruiting
# boilerplate that would otherwise dominate an OR query.
_NOISE = frozenset(
    {
        "we",
        "you",
        "your",
        "our",
        "will",
        "and",
        "the",
        "for",
        "with",
        "who",
        "are",
        "have",
        "this",
        "that",
        "role",
        "team",
        "work",
        "working",
        "need",
        "want",
        "looking",
        "join",
        "help",
        "build",
        "building",
        "own",
        "owning",
        "across",
        "using",
        "used",
        "able",
        "strong",
        "good",
        "great",
        "experience",
        "years",
        "candidate",
        "engineer",
        "developer",
        "company",
        "business",
        "product",
    }
)


def to_or_query(text_value: str, *, max_terms: int = 30) -> str:
    """Turn free text into a disjunctive websearch query.

    `websearch_to_tsquery` ANDs bare terms, so passing a whole job description
    demanded that a chunk contain EVERY word — and matched nothing at all. The
    lexical half of hybrid search silently contributed zero on every query until
    this was measured. Terms are OR'd instead, which is what ts_rank_cd is for.

    Still parameterised: the OR-joined string goes to websearch_to_tsquery as a
    bound value, which handles arbitrary input safely.
    """
    seen: list[str] = []
    for raw in re.findall(r"[A-Za-z0-9+#.]{2,}", text_value.lower()):
        token = raw.strip(".")
        if len(token) < 3 or token in _NOISE or token in seen:
            continue
        seen.append(token)
        if len(seen) >= max_terms:
            break
    return " or ".join(seen)


async def lexical_search(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    query: str,
    limit: int = 20,
    resume_ids: list[uuid.UUID] | None = None,
) -> list[Hit]:
    """BM25-style ranking via Postgres full text.

    Catches the exact-token matches dense retrieval is weakest at: a rare
    framework name, a certification code, an unusual spelling.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT id, resume_id, chunk_index, text_redacted, char_start, char_end,
                       section, ts_rank_cd(tsv, q) AS rank
                  FROM resume_chunks, websearch_to_tsquery('english', :q) AS q
                 WHERE org_id = :org
                   AND tsv @@ q
                   AND (NOT :filter_resumes OR resume_id = ANY(:resume_ids))
                 ORDER BY rank DESC
                 LIMIT :lim
                """
            ),
            {
                "org": org_id,
                "q": to_or_query(query),
                "lim": limit,
                "filter_resumes": bool(resume_ids),
                "resume_ids": [str(r) for r in (resume_ids or [])],
            },
        )
    ).all()

    return [
        Hit(
            chunk_id=r[0],
            resume_id=r[1],
            chunk_index=r[2],
            text=r[3],
            char_start=r[4],
            char_end=r[5],
            section=r[6],
            score=float(r[7]),
            lexical_rank=i + 1,
        )
        for i, r in enumerate(rows)
    ]


def fuse(
    vector_hits: list[Hit], lexical_hits: list[Hit], *, limit: int = 20, k: int = RRF_K
) -> list[Hit]:
    """Reciprocal rank fusion.

    Ranks rather than raw scores, because cosine similarity and ts_rank_cd are
    not on comparable scales and normalising them is guesswork. RRF only needs
    the ordering, which is exactly what both methods are trustworthy about.
    """
    merged: dict[uuid.UUID, Hit] = {}
    scores: dict[uuid.UUID, float] = {}

    for hits, attr in ((vector_hits, "vector_rank"), (lexical_hits, "lexical_rank")):
        for rank, hit in enumerate(hits, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (k + rank)
            existing = merged.get(hit.chunk_id)
            if existing is None:
                merged[hit.chunk_id] = hit
            else:
                merged[hit.chunk_id] = Hit(
                    **{
                        **existing.__dict__,
                        attr: getattr(hit, attr) or getattr(existing, attr),
                    }
                )

    ranked = sorted(merged.values(), key=lambda h: scores[h.chunk_id], reverse=True)
    return [Hit(**{**h.__dict__, "score": scores[h.chunk_id]}) for h in ranked[:limit]]


async def hybrid_search(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    query: str,
    limit: int = 20,
    resume_ids: list[uuid.UUID] | None = None,
) -> list[Hit]:
    from screener_api.retrieval.embedding import embed_query

    vector_hits = await vector_search(
        session,
        org_id=org_id,
        query_vector=embed_query(query),
        limit=limit * 2,
        resume_ids=resume_ids,
    )
    lexical_hits = await lexical_search(
        session, org_id=org_id, query=query, limit=limit * 2, resume_ids=resume_ids
    )
    return fuse(vector_hits, lexical_hits, limit=limit)
