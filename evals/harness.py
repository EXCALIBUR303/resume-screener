"""Evaluation harness.

Loads the golden corpus into a scratch database, indexes it through the real
retrieval path, and measures three retrievers against construction-derived
labels. Runs offline: embeddings are local ONNX, and no model is called.

Reports every number with n and the corpus version attached. See README.md for
what these labels are and are not.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import uuid
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api" / "src"))
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from evals.metrics import (  # noqa: E402
    mean,
    mean_reciprocal_rank,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)
from screener_api.retrieval.chunking import chunk_text  # noqa: E402
from screener_api.retrieval.embedding import embed_documents, embed_query  # noqa: E402
from screener_api.retrieval.search import fuse, lexical_search, vector_search  # noqa: E402

GOLDEN = ROOT / "evals" / "golden"


@dataclass
class RetrieverResult:
    name: str
    ndcg10: float
    p5: float
    recall20: float
    mrr: float


async def _load(session, corpus: dict, org_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """Index every resume through the real chunker and embedder."""
    # Start from a clean slate so a rerun cannot accumulate.
    for table in ("resume_chunks", "matches", "resumes", "candidates", "files"):
        await session.execute(text(f"DELETE FROM {table} WHERE org_id = :org"),  # noqa: S608
                              {"org": org_id})
    await session.commit()

    mapping: dict[str, uuid.UUID] = {}
    texts, metas = [], []
    for entry in corpus["resumes"]:
        resume_uuid = uuid.uuid5(uuid.NAMESPACE_OID, entry["resume_id"])
        mapping[entry["resume_id"]] = resume_uuid

        # Chunks reference resumes, which reference candidates and files. The
        # harness indexes through the REAL schema rather than a loosened copy,
        # so it exercises the same constraints production does.
        file_uuid = uuid.uuid5(uuid.NAMESPACE_OID, f"file:{entry['resume_id']}")
        cand_uuid = uuid.uuid5(uuid.NAMESPACE_OID, f"cand:{entry['resume_id']}")
        await session.execute(
            text(
                "INSERT INTO files (id, org_id, sha256, storage_key, byte_size, "
                "mime_sniffed, mime_resolved) VALUES (:id, :org, :sha, :key, 1, "
                "'application/pdf', 'application/pdf') ON CONFLICT DO NOTHING"
            ),
            {"id": file_uuid, "org": org_id,
             "sha": uuid.uuid5(uuid.NAMESPACE_OID, entry["resume_id"]).hex * 2,
             "key": entry["resume_id"]},
        )
        await session.execute(
            text("INSERT INTO candidates (id, org_id, pseudonym) VALUES "
                 "(:id, :org, :p) ON CONFLICT DO NOTHING"),
            {"id": cand_uuid, "org": org_id, "p": entry["resume_id"].upper()},
        )
        await session.execute(
            text("INSERT INTO resumes (id, org_id, candidate_id, file_id, "
                 "parse_status) VALUES (:id, :org, :c, :f, 'parsed') "
                 "ON CONFLICT DO NOTHING"),
            {"id": resume_uuid, "org": org_id, "c": cand_uuid, "f": file_uuid},
        )
        for chunk in chunk_text(entry["text"]):
            texts.append(chunk.text)
            metas.append((resume_uuid, chunk))

    vectors = embed_documents(texts)
    for (resume_uuid, chunk), vector in zip(metas, vectors, strict=True):
        await session.execute(
            text(
                "INSERT INTO resume_chunks (id, org_id, resume_id, chunk_index, "
                "text_redacted, char_start, char_end, section, embedding) VALUES "
                "(:id, :org, :rid, :idx, :txt, :cs, :ce, :sec, :emb)"
            ),
            {"id": uuid.uuid4(), "org": org_id, "rid": resume_uuid,
             "idx": chunk.index, "txt": chunk.text, "cs": chunk.char_start,
             "ce": chunk.char_end, "sec": chunk.section, "emb": str(vector)},
        )
    await session.commit()
    return mapping


def _grades_for(hits, mapping, labels, job_id) -> list[int]:
    """Collapse chunk hits to resumes, keeping first appearance, then grade."""
    reverse = {v: k for k, v in mapping.items()}
    seen: list[str] = []
    for hit in hits:
        rid = reverse.get(hit.resume_id)
        if rid and rid not in seen:
            seen.append(rid)
    return [labels[(job_id, rid)] for rid in seen]


async def run(dsn: str) -> dict:
    corpus = json.loads((GOLDEN / "corpus.json").read_text())
    labels = {
        (row["job_id"], row["resume_id"]): row["grade"]
        for row in (json.loads(line) for line in
                    (GOLDEN / "labels.jsonl").read_text().splitlines() if line.strip())
    }

    engine = create_async_engine(dsn)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    org_id = uuid.uuid5(uuid.NAMESPACE_OID, "eval-org")

    async with maker() as session:
        await session.execute(
            text("INSERT INTO organizations (id, name) VALUES (:id, 'Eval Org') "
                 "ON CONFLICT (id) DO NOTHING"),
            {"id": org_id},
        )
        await session.commit()
        mapping = await _load(session, corpus, org_id)

        per_retriever: dict[str, list[tuple[float, float, float, float]]] = {
            "vector": [], "lexical": [], "hybrid": [],
        }

        for job in corpus["jobs"]:
            query = job["description"]
            all_grades = [labels[(job["id"], r["resume_id"])] for r in corpus["resumes"]]

            vec = await vector_search(session, org_id=org_id,
                                      query_vector=embed_query(query), limit=60)
            lex = await lexical_search(session, org_id=org_id, query=query, limit=60)
            hyb = fuse(vec, lex, limit=60)

            for name, hits in (("vector", vec), ("lexical", lex), ("hybrid", hyb)):
                ranked = _grades_for(hits, mapping, labels, job["id"])
                per_retriever[name].append((
                    ndcg_at_k(ranked, 10),
                    precision_at_k(ranked, 5),
                    recall_at_k(ranked, all_grades, 20),
                    mean_reciprocal_rank(ranked),
                ))

    await engine.dispose()

    results = {
        name: RetrieverResult(
            name=name,
            ndcg10=round(mean([r[0] for r in rows]), 4),
            p5=round(mean([r[1] for r in rows]), 4),
            recall20=round(mean([r[2] for r in rows]), 4),
            mrr=round(mean([r[3] for r in rows]), 4),
        )
        for name, rows in per_retriever.items()
    }

    return {
        "corpus_version": corpus["version"],
        "resumes": len(corpus["resumes"]),
        "jobs": len(corpus["jobs"]),
        "pairs": len(labels),
        "labels_are": corpus["labels_are"],
        "retrievers": {n: vars(r) for n, r in results.items()},
    }


def main() -> int:
    import os

    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5433")
    dsn = os.environ.get(
        "EVAL_DSN",
        f"postgresql+psycopg://screener:{password}@{host}:{port}/screener",
    )
    report = asyncio.run(run(dsn))

    print(f"\n  Golden set {report['corpus_version']}: {report['resumes']} resumes "
          f"x {report['jobs']} jobs = {report['pairs']} labelled pairs")
    print(f"  Labels: {report['labels_are']}\n")
    print(f"  {'retriever':<12}{'nDCG@10':>10}{'P@5':>8}{'Recall@20':>12}{'MRR':>8}")
    print("  " + "-" * 50)
    for name in ("vector", "lexical", "hybrid"):
        r = report["retrievers"][name]
        print(f"  {name:<12}{r['ndcg10']:>10.3f}{r['p5']:>8.3f}"
              f"{r['recall20']:>12.3f}{r['mrr']:>8.3f}")

    out = ROOT / "evals" / "reports" / "latest.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\n  written to {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
