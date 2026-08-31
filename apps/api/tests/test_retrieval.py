"""Chunking, embedding and fusion. Database-free unit coverage."""

from __future__ import annotations

import itertools
import uuid

import pytest

from screener_api.retrieval.chunking import chunk_text
from screener_api.retrieval.search import RRF_K, Hit, fuse

TEXT = (
    "PERSON_1\nEMAIL_1\n\nSUMMARY\nBackend engineer with seven years of experience "
    "building payment and ledger systems at scale.\n\nWORK EXPERIENCE\n"
    "Senior Backend Engineer, ORG_1 (2021-2026). Designed payment services in "
    "Python on PostgreSQL handling twelve thousand requests per second. Led the "
    "migration from a monolith to six independently deployed services.\n\n"
    "EDUCATION\nB.Tech Computer Science, ORG_2, GRAD_YEAR_1\n\n"
    "TECHNICAL SKILLS\nPython, PostgreSQL, Redis, Docker, Kubernetes, pytest\n"
)


# ---- the invariant M6 depends on -----------------------------------------------


def test_every_chunk_reproduces_its_own_slice() -> None:
    """A chunk that cannot reproduce itself from its offsets makes verbatim
    evidence verification impossible, which is the whole anti-hallucination
    mechanism."""
    for chunk in chunk_text(TEXT, size=300, overlap=60):
        assert chunk.verify(TEXT), f"chunk {chunk.index} offsets do not match its text"


@pytest.mark.parametrize(("size", "overlap"), [(200, 40), (400, 100), (1200, 200), (5000, 0)])
def test_offsets_hold_at_every_size(size: int, overlap: int) -> None:
    for chunk in chunk_text(TEXT, size=size, overlap=overlap):
        assert chunk.verify(TEXT)


def test_chunks_cover_the_whole_document() -> None:
    chunks = chunk_text(TEXT, size=300, overlap=60)
    assert chunks[0].char_start == 0
    assert chunks[-1].char_end == len(TEXT)


def test_chunks_are_ordered_and_indexed_contiguously() -> None:
    chunks = chunk_text(TEXT, size=300, overlap=60)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    for previous, current in itertools.pairwise(chunks):
        assert current.char_start > previous.char_start


def test_overlap_is_applied() -> None:
    chunks = chunk_text(TEXT, size=300, overlap=100)
    if len(chunks) > 1:
        assert chunks[1].char_start < chunks[0].char_end


def test_empty_input_yields_no_chunks() -> None:
    for value in ("", "   ", "\n\n\n"):
        assert chunk_text(value) == []


def test_overlap_must_be_smaller_than_size() -> None:
    with pytest.raises(ValueError, match="smaller"):
        chunk_text(TEXT, size=100, overlap=100)


def test_a_single_huge_sentence_still_chunks() -> None:
    """No natural boundary exists; the chunker must still make progress rather
    than loop or emit one enormous chunk."""
    text = "word " * 4000
    chunks = chunk_text(text, size=500, overlap=50)
    assert len(chunks) > 1
    assert all(c.verify(text) for c in chunks)
    assert all(len(c.text) <= 600 for c in chunks)


def test_sections_are_attached_to_chunks() -> None:
    sections = {"header": "PERSON_1\nEMAIL_1", "skills": "Python, PostgreSQL, Redis"}
    chunks = chunk_text(TEXT, size=250, overlap=50, sections=sections)
    assert any(c.section for c in chunks)


# ---- reciprocal rank fusion -----------------------------------------------------


def _hit(n: int, *, vector_rank: int | None = None, lexical_rank: int | None = None) -> Hit:
    return Hit(
        chunk_id=uuid.UUID(int=n),
        resume_id=uuid.UUID(int=1000),
        chunk_index=n,
        text=f"chunk {n}",
        char_start=n * 10,
        char_end=n * 10 + 9,
        section=None,
        score=0.0,
        vector_rank=vector_rank,
        lexical_rank=lexical_rank,
    )


def test_fusion_rewards_agreement_between_methods() -> None:
    """A chunk both methods rank highly must beat one that only a single method
    likes. That is the entire reason for running two retrievers."""
    vector = [_hit(1), _hit(2), _hit(3)]
    lexical = [_hit(3), _hit(4), _hit(1)]
    fused = fuse(vector, lexical, limit=5)
    assert fused[0].chunk_id in {uuid.UUID(int=1), uuid.UUID(int=3)}
    ids = [h.chunk_id for h in fused]
    assert uuid.UUID(int=1) in ids[:2]
    assert uuid.UUID(int=3) in ids[:2]


def test_fusion_deduplicates() -> None:
    fused = fuse([_hit(1), _hit(2)], [_hit(1), _hit(2)], limit=10)
    assert len({h.chunk_id for h in fused}) == len(fused) == 2


def test_fusion_scores_match_the_rrf_formula() -> None:
    fused = fuse([_hit(1)], [_hit(1)], limit=1)
    assert fused[0].score == pytest.approx(2.0 / (RRF_K + 1))


def test_fusion_keeps_results_found_by_only_one_method() -> None:
    """Dense retrieval misses rare exact tokens; lexical misses paraphrase.
    Neither may be silently dropped."""
    fused = fuse([_hit(1)], [_hit(9)], limit=10)
    assert {h.chunk_id for h in fused} == {uuid.UUID(int=1), uuid.UUID(int=9)}


def test_fusion_handles_an_empty_side() -> None:
    assert len(fuse([_hit(1), _hit(2)], [], limit=10)) == 2
    assert len(fuse([], [_hit(1)], limit=10)) == 1
    assert fuse([], [], limit=10) == []


def test_fusion_respects_the_limit() -> None:
    assert len(fuse([_hit(i) for i in range(20)], [_hit(i) for i in range(10, 30)], limit=5)) == 5


def test_fused_hits_retain_their_offsets() -> None:
    """Fusion must not lose the offsets evidence verification needs."""
    fused = fuse([_hit(7)], [_hit(7)], limit=1)
    assert fused[0].char_start == 70
    assert fused[0].char_end == 79
