"""Can a transcript carry the evidence this system scores on?

M14 lists "audio answers via faster-whisper". Before building it, the question
worth answering is not "does transcription work" — it does — but "does a
transcript preserve the thing the scorer reads". This project matches skill
names literally and gates every competency on a verbatim quote, so the answer
decides whether audio may enter the scoring path at all.

Three conditions, deliberately including the two that could embarrass the idea:

``technical``
    Answers that genuinely mention a required skill. Measures **recall**: does
    the term survive transcription? A term lost here is a candidate penalised
    for the transcriber.

``control``
    Answers with no technical vocabulary whatsoever. Measures whether the
    decoder invents skills from nothing.

``near_miss``
    Ordinary English that sounds like a skill name — "the airflow in the
    building", "a spark of creativity", "the python at the zoo". Measures
    **precision**, and it is the condition that matters: a candidate credited
    for a word they used in a completely different sense.

Each condition runs with and without a *glossary* — the job posting's required
skills passed as `initial_prompt` to bias the decoder. That is information the
system genuinely has, and it is the obvious mitigation for the recall problem,
so it is measured rather than assumed.

Speech is synthesised, because a corpus of real candidate audio is exactly the
personal data this project refuses to handle (see `evals/README.md`). That is a
real limitation and it is stated in the report: synthetic speech is clean,
single-speaker and unaccented, which makes these numbers a **best case**.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parents[2]
SAMPLE_RATE = 16_000

# The vocabulary the scorer actually looks for, drawn from the golden corpus's
# job postings so this measures the real matching surface.
SKILLS: tuple[str, ...] = (
    "PostgreSQL",
    "Kubernetes",
    "Python",
    "FastAPI",
    "pytest",
    "Redis",
    "Docker",
    "Spark",
    "Airflow",
    "Snowflake",
    "PyTorch",
    "TypeScript",
    "Terraform",
    "Prometheus",
)


@dataclass(frozen=True)
class Clip:
    """One spoken answer, and the skills the speaker actually claimed.

    `expects` is written by hand. It is empty for every control and near-miss
    clip even when the sentence contains the word, because saying "the python
    at the zoo" is not claiming Python.
    """

    text: str
    expects: tuple[str, ...] = ()


TECHNICAL = [
    Clip(
        "I designed PostgreSQL schemas and tuned queries under production load.",
        ("PostgreSQL",),
    ),
    Clip(
        "I ran workloads on Kubernetes, owning rollouts, probes and autoscaling.",
        ("Kubernetes",),
    ),
    Clip(
        "I built async APIs in Python with FastAPI and pytest.",
        ("Python", "FastAPI", "pytest"),
    ),
    Clip(
        "I used Redis for caching and rate limiting in high traffic paths.", ("Redis",)
    ),
    Clip(
        "I containerised services with Docker and maintained base images.", ("Docker",)
    ),
    Clip("I wrote Spark jobs processing several terabytes per day.", ("Spark",)),
    Clip(
        "I authored Airflow DAGs for nightly pipelines loading Snowflake.",
        ("Airflow", "Snowflake"),
    ),
    Clip(
        "I trained ranking models in PyTorch and shipped them to production.",
        ("PyTorch",),
    ),
    Clip(
        "I migrated a large codebase to TypeScript and enforced strict mode.",
        ("TypeScript",),
    ),
    Clip(
        "I managed infrastructure as code in Terraform across environments.",
        ("Terraform",),
    ),
    Clip(
        "I instrumented services with Prometheus metrics and wrote alerts.",
        ("Prometheus",),
    ),
]

CONTROL = [
    Clip("I mostly worked on customer support tickets and internal reporting."),
    Clip("My last role was scheduling deliveries and reconciling invoices."),
    Clip("I spent two years teaching secondary school mathematics."),
    Clip("I managed a small team in a retail environment for three years."),
    Clip("I helped organise community events and handled the mailing list."),
    Clip("I answered phones and maintained the appointment calendar."),
]

# Ordinary English whose sound overlaps a skill name. Not adversarial in the
# security sense — nobody is attacking anything. Just how people talk.
NEAR_MISS = [
    Clip("The airflow in the building was poor so we opened the windows."),
    Clip("She brought a spark of creativity to every meeting we ran."),
    Clip("I worked with red ink on the printed proofs before sending them."),
    Clip("We docked the boat at the marina and unloaded the crates."),
    Clip("The python at the zoo was fed once a fortnight by the keeper."),
    Clip("He was the anchor of the team and kept everyone calm."),
]

GLOSSARY = "Glossary: " + ", ".join(SKILLS) + "."


@dataclass
class ConditionResult:
    condition: str
    model: str
    glossary: bool
    clips: int
    # Recall: skill mentions that survived. Only meaningful for `technical`.
    mentions_expected: int = 0
    mentions_found: int = 0
    lost: list[str] = field(default_factory=list)
    # Precision: skills the transcript claims that were never said.
    false_claims: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float | None:
        if not self.mentions_expected:
            return None
        return round(self.mentions_found / self.mentions_expected, 4)

    def as_dict(self) -> dict[str, object]:
        return {
            "condition": self.condition,
            "model": self.model,
            "glossary": self.glossary,
            "clips": self.clips,
            "recall": self.recall,
            "mentions_expected": self.mentions_expected,
            "mentions_found": self.mentions_found,
            "lost": sorted(set(self.lost)),
            "false_claims": self.false_claims,
        }


def _tts_command(text: str, out: pathlib.Path) -> list[str] | None:
    """A TTS that can write 16 kHz mono PCM, or None if the host has neither."""
    if shutil.which("say"):  # macOS
        return ["say", "-o", str(out), f"--data-format=LEI16@{SAMPLE_RATE}", text]
    if shutil.which("espeak-ng"):  # Linux
        return ["espeak-ng", "-w", str(out), "-s", "150", text]
    return None


def synthesise(
    clips: list[Clip], directory: pathlib.Path, tag: str
) -> list[pathlib.Path]:
    paths: list[pathlib.Path] = []
    for index, clip in enumerate(clips, start=1):
        out = directory / f"{tag}{index}.wav"
        command = _tts_command(clip.text, out)
        if command is None:
            raise RuntimeError(
                "no text-to-speech available (need `say` or `espeak-ng`)"
            )
        subprocess.run(command, check=True, capture_output=True)
        paths.append(out)
    return paths


def decode_wav(path: pathlib.Path):
    """Decode with the standard library, never with ffmpeg.

    faster-whisper accepts a float32 array, so the bytes never reach `av`. On a
    real upload path that distinction is the difference between parsing
    attacker-controlled audio with a decoder that has a decade of CVEs and
    parsing it with 40 lines of `wave`.
    """
    import numpy as np

    with wave.open(str(path)) as handle:
        if handle.getsampwidth() != 2 or handle.getnchannels() != 1:
            raise ValueError("expected 16-bit mono PCM")
        frames = handle.readframes(handle.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


def _skills_in(text: str) -> set[str]:
    """What a keyword matcher reads out of this text.

    Case-insensitive substring, which is exactly what `score_deterministic`
    does. That is the point of the near-miss condition: this function cannot
    tell "I wrote Python" from "the python at the zoo", and neither can the
    scorer.
    """
    lowered = text.lower()
    return {s for s in SKILLS if s.lower() in lowered}


def run_condition(
    model,
    condition: str,
    clips: list[Clip],
    paths: list[pathlib.Path],
    *,
    name: str,
    glossary: bool,
) -> ConditionResult:
    result = ConditionResult(
        condition=condition, model=name, glossary=glossary, clips=len(paths)
    )
    prompt = GLOSSARY if glossary else None

    for clip, path in zip(clips, paths, strict=True):
        segments, _ = model.transcribe(
            decode_wav(path), language="en", beam_size=1, initial_prompt=prompt
        )
        heard = _skills_in(" ".join(s.text for s in segments))
        # `expects` is declared per clip, NOT derived from the sentence.
        #
        # The first version derived it, and reported zero false claims for the
        # near-miss set — because "the python at the zoo" contains the substring
        # "python", so the harness scored a correct hit. It was measuring
        # agreement between two runs of the same broken rule. What a clip is
        # entitled to claim is a property of what the speaker meant, and only a
        # human writing the corpus knows that.
        expected = set(clip.expects)
        result.mentions_expected += len(expected)
        result.mentions_found += len(expected & heard)
        result.lost.extend(sorted(expected - heard))
        for invented in sorted(heard - expected):
            result.false_claims.append(f"{invented} <- {clip.text}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", default="tiny,small")
    parser.add_argument(
        "--out", default=str(ROOT / "evals" / "reports" / "asr_vocabulary.json")
    )
    args = parser.parse_args()

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("\n  faster-whisper is not installed, and it is deliberately NOT in")
        print("  requirements.lock — this benchmark is the reason the dependency was")
        print("  never added (ADR-0022). To reproduce:\n")
        print("      pip install faster-whisper\n")
        return 2

    if _tts_command("x", pathlib.Path("/dev/null")) is None:
        print(
            "\n  No text-to-speech on this host. Needs `say` (macOS) or `espeak-ng`.\n"
        )
        return 2

    conditions = [
        ("technical", TECHNICAL),
        ("control", CONTROL),
        ("near_miss", NEAR_MISS),
    ]
    results: list[ConditionResult] = []

    with tempfile.TemporaryDirectory() as raw:
        directory = pathlib.Path(raw)
        audio = {tag: synthesise(clips, directory, tag) for tag, clips in conditions}

        for name in args.models.split(","):
            model = WhisperModel(name.strip(), device="cpu", compute_type="int8")
            for glossary in (False, True):
                for tag, clips in conditions:
                    results.append(
                        run_condition(
                            model,
                            tag,
                            clips,
                            audio[tag],
                            name=name.strip(),
                            glossary=glossary,
                        )
                    )

    print(
        f"\n  ASR vocabulary survival: {len(TECHNICAL)} technical, {len(CONTROL)} control, "
        f"{len(NEAR_MISS)} near-miss clips\n"
    )
    header = f"  {'model':<8}{'glossary':>10}{'condition':>12}{'recall':>10}{'false claims':>15}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for r in results:
        recall = f"{r.recall:.0%}" if r.recall is not None else "n/a"
        print(
            f"  {r.model:<8}{r.glossary!s:>10}{r.condition:>12}{recall:>10}"
            f"{len(r.false_claims):>15}"
        )

    near_miss_claims = sorted(
        {c for r in results if r.condition == "near_miss" for c in r.false_claims}
    )
    print("\n  Skills claimed by a transcript of ordinary English:")
    for claim in near_miss_claims:
        print(f"    {claim}")
    if not near_miss_claims:
        print("    none")

    report = {
        "skills_tested": list(SKILLS),
        "speech": "synthesised; clean, single-speaker, unaccented. These are a BEST case.",
        "clips": {
            "technical": len(TECHNICAL),
            "control": len(CONTROL),
            "near_miss": len(NEAR_MISS),
        },
        "results": [r.as_dict() for r in results],
        "near_miss_claims": near_miss_claims,
    }
    out = pathlib.Path(args.out)
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\n  written to {out.relative_to(ROOT)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
