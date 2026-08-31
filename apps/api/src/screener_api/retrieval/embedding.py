"""Embedding via ONNX, in-process.

In-process rather than a service call: no network hop, so the parse worker keeps
`network_mode: none`, and the model is pinned by name in the image rather than
by whatever a remote endpoint happens to be serving today.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import structlog

log = structlog.get_logger()

MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384

# bge models are trained with an asymmetric prefix: queries get one, documents
# do not. Omitting it costs real retrieval quality, and it is the single easiest
# thing to get wrong with this family of models.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class DimensionMismatchError(RuntimeError):
    """The model's output width does not match the database column."""


@lru_cache(maxsize=1)
def _model() -> Any:
    from fastembed import TextEmbedding

    # The worker has no network. The model must already be in the cache
    # directory, baked in at build time.
    cache_dir = os.environ.get("FASTEMBED_CACHE_PATH", "/opt/models")
    return TextEmbedding(model_name=MODEL_NAME, cache_dir=cache_dir)


def embed_documents(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    vectors = [list(map(float, v)) for v in _model().embed(texts)]
    _assert_dimensions(vectors)
    return vectors


def embed_query(text: str) -> list[float]:
    vectors = [list(map(float, v)) for v in _model().query_embed([text])]
    _assert_dimensions(vectors)
    return vectors[0]


def _assert_dimensions(vectors: list[list[float]]) -> None:
    for vector in vectors:
        if len(vector) != EMBEDDING_DIM:
            raise DimensionMismatchError(
                f"model produced {len(vector)}-dim vectors, column expects {EMBEDDING_DIM}"
            )


def check_model_available() -> tuple[bool, str]:
    """Startup probe. A worker that cannot embed should say so loudly at boot
    rather than dead-lettering every job it claims."""
    try:
        vector = embed_documents(["startup probe"])[0]
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, f"{MODEL_NAME} ready ({len(vector)} dims)"
