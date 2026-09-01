"""Versioned prompt loader.

A prompt is part of the system's behaviour, so it is versioned like code and its
hash is stored on every score it produced. Without that, a score from last month
cannot be explained: you know what the model said, not what it was asked.

Rule D-12: a prompt file is immutable once committed. Change it by adding
`v2.md`, never by editing `v1.md`, because scores already reference v1's hash.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()


def _default_prompts_dir() -> Path:
    """Find the prompts directory by searching upward.

    Counting parents broke the moment the container laid the tree out one level
    shallower than the repo (`/app/src/...` vs `apps/api/src/...`), and the
    os.environ.get default was evaluated eagerly, so it crashed even with
    PROMPTS_DIR set. Searching works in both layouts and cannot go out of range.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "prompts"
        if candidate.is_dir():
            return candidate
    return here.parent / "prompts"


PROMPTS_DIR = (
    Path(os.environ["PROMPTS_DIR"]) if os.environ.get("PROMPTS_DIR") else _default_prompts_dir()
)

_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_SECTION = re.compile(r"^##\s+(system|user)\s*$", re.M)


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    version: int
    system: str
    user: str
    content_hash: str
    metadata: dict[str, Any]

    @property
    def version_id(self) -> str:
        return f"{self.name}.v{self.version}"

    def render(self, **values: Any) -> tuple[str, str]:
        """Fill the user template. The system prompt never takes user content —
        that separation is what makes the untrusted-document fence meaningful."""
        return self.system, self.user.format(**values)


def _parse_front_matter(raw: str) -> tuple[dict[str, Any], str]:
    match = _FRONT_MATTER.match(raw)
    if match is None:
        return {}, raw
    meta: dict[str, Any] = {}
    for line in match.group(1).split("\n"):
        if ":" in line and not line.startswith((" ", "-")):
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, raw[match.end() :]


@lru_cache(maxsize=32)
def active_version(name: str, pinned: int | None) -> int:
    """The version to use: an explicit pin, or the highest on disk.

    Exists so "which prompt is running" is a decision rather than a consequence
    of which files happen to be in the directory. Prompt files are immutable
    once committed (rule D-12), but `latest_version` is implicit — so adding a
    file to run an experiment silently promotes it.
    """
    return pinned if pinned is not None else latest_version(name)


def load(name: str, version: int, *, base: Path | None = None) -> PromptTemplate:
    path = (base or PROMPTS_DIR) / name / f"v{version}.md"
    raw = path.read_text(encoding="utf-8")
    # Hash the file as committed, including front matter: a metadata change is a
    # behaviour change.
    content_hash = hashlib.sha256(raw.encode()).hexdigest()

    meta, body = _parse_front_matter(raw)
    parts = _SECTION.split(body)
    sections: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        sections[parts[i].strip()] = parts[i + 1].strip()

    if "system" not in sections or "user" not in sections:
        raise ValueError(f"{path} must define both '## system' and '## user'")

    return PromptTemplate(
        name=name,
        version=version,
        system=sections["system"],
        user=sections["user"],
        content_hash=content_hash,
        metadata=meta,
    )


def latest_version(name: str, *, base: Path | None = None) -> int:
    directory = (base or PROMPTS_DIR) / name
    versions = [int(p.stem[1:]) for p in directory.glob("v*.md") if p.stem[1:].isdigit()]
    if not versions:
        raise FileNotFoundError(f"no prompt versions under {directory}")
    return max(versions)
