"""Counterfactual fairness harness.

Runs the **real** scoring pipeline — the same `handle_score_job` the worker
calls, over the same schema, with the same retrieval — across counterfactual
resume sets, and reports what changed.

Two questions, and they are not the same question:

1. *Does redaction erase the signal?*  For a removable axis every variant must
   reduce to byte-identical redacted text. This is the strong claim, and it is
   the one worth making: everything downstream is a pure function of that text
   and the job posting, so identical text means an identical score without
   having to measure one.

2. *Does the score move anyway?*  For axes that legitimately change the
   document, the scores are compared directly and any spread is reported.

Offline: embeddings are local ONNX and the LLM is the stub provider, so this
costs nothing and produces the same numbers on every run.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import uuid
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api" / "src"))
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from evals.fairness.air import adverse_impact_ratio  # noqa: E402
from evals.fairness.variants import JOB, Variant, all_sets  # noqa: E402
from screener_api.llm.gateway import LLMGateway  # noqa: E402
from screener_api.llm.prompts import latest_version, load  # noqa: E402
from screener_api.llm.provider import StubProvider  # noqa: E402
from screener_api.models import Match  # noqa: E402
from screener_api.privacy.redact import redact  # noqa: E402
from screener_api.retrieval.chunking import chunk_text  # noqa: E402
from screener_api.retrieval.embedding import embed_documents  # noqa: E402
from screener_api.scoring.pipeline import handle_score_job  # noqa: E402

ORG_ID = uuid.uuid5(uuid.NAMESPACE_OID, "fairness-org")
JOB_ID = uuid.uuid5(uuid.NAMESPACE_OID, "fairness-job")

# The score at or above which a candidate is treated as "selected" for the
# adverse-impact calculation. 0.5 of 1.0 — stated here rather than buried,
# because the AIR is a function of where this line is drawn.
SELECTION_THRESHOLD = 0.5

# Pinned so the prompt is byte-identical for byte-identical redacted text.
# Only the harness does this; production takes a fresh random value per request
# (see the docstring on handle_score_job).
FIXED_NONCE = "0" * 16


def variant_key(v: Variant) -> str:
    return f"{v.base_id}|{v.axis}|{v.label}"


async def _reset(session) -> None:
    for table in ("matches", "resume_chunks", "resume_texts", "resumes",
                  "candidates", "files", "job_postings"):
        await session.execute(text(f"DELETE FROM {table} WHERE org_id = :org"),  # noqa: S608
                              {"org": ORG_ID})
    await session.execute(
        text("INSERT INTO organizations (id, name) VALUES (:id, 'Fairness Org') "
             "ON CONFLICT (id) DO NOTHING"),
        {"id": ORG_ID},
    )
    await session.execute(
        text("INSERT INTO job_postings (id, org_id, title, description, "
             "required_skills, nice_to_have, hard_requirements, min_years) VALUES "
             "(:id, :org, :t, :d, :req, :nice, :hard, :yrs)"),
        {"id": JOB_ID, "org": ORG_ID, "t": JOB["title"], "d": JOB["description"],
         "req": json.dumps(JOB["required_skills"]), "nice": json.dumps(JOB["nice_to_have"]),
         "hard": json.dumps(JOB["hard_requirements"]), "yrs": JOB["min_years"]},
    )
    await session.commit()


async def _create_slot(session, slot: str) -> uuid.UUID:
    """One reusable resume row per counterfactual set.

    Every variant in a set is scored through the SAME row. The prompt embeds
    the resume id alongside the nonce, so giving each variant its own row would
    reintroduce exactly the prompt variation the fixed nonce is there to
    remove — a subtler version of the same mistake, and one that would have
    left the harness quietly noisy instead of obviously noisy.
    """
    resume_uuid = uuid.uuid5(uuid.NAMESPACE_OID, f"fair:{slot}")
    file_uuid = uuid.uuid5(uuid.NAMESPACE_OID, f"fair-file:{slot}")
    cand_uuid = uuid.uuid5(uuid.NAMESPACE_OID, f"fair-cand:{slot}")

    await session.execute(
        text("INSERT INTO files (id, org_id, sha256, storage_key, byte_size, "
             "mime_sniffed, mime_resolved) VALUES (:id, :org, :sha, :k, 1, "
             "'application/pdf', 'application/pdf')"),
        {"id": file_uuid, "org": ORG_ID,
         "sha": uuid.uuid5(uuid.NAMESPACE_OID, slot).hex * 2, "k": slot[:120]},
    )
    await session.execute(
        text("INSERT INTO candidates (id, org_id, pseudonym) VALUES (:id, :org, :p)"),
        {"id": cand_uuid, "org": ORG_ID, "p": slot[:60]},
    )
    await session.execute(
        text("INSERT INTO resumes (id, org_id, candidate_id, file_id, parse_status) "
             "VALUES (:id, :org, :c, :f, 'parsed')"),
        {"id": resume_uuid, "org": ORG_ID, "c": cand_uuid, "f": file_uuid},
    )
    return resume_uuid


async def _load_variant(session, resume_uuid: uuid.UUID, v: Variant) -> str:
    """Replace the slot's text and index with this variant. Returns redacted text."""
    redacted = redact(v.text, header=v.header).text

    await session.execute(
        text("DELETE FROM resume_chunks WHERE resume_id = :r"), {"r": resume_uuid}
    )
    await session.execute(
        text("DELETE FROM resume_texts WHERE resume_id = :r"), {"r": resume_uuid}
    )
    await session.execute(
        text("INSERT INTO resume_texts (id, org_id, resume_id, raw_text, "
             "text_redacted, char_count, extractor) VALUES "
             "(:id, :org, :r, :raw, :red, :n, 'fairness-harness')"),
        {"id": uuid.uuid4(), "org": ORG_ID, "r": resume_uuid, "raw": v.text,
         "red": redacted, "n": len(redacted)},
    )

    chunks = list(chunk_text(redacted))
    for chunk, vector in zip(chunks, embed_documents([c.text for c in chunks]), strict=True):
        # Derived, not random. The prompt interpolates chunk ids into the
        # document block, so a fresh uuid4 per index would change the prompt
        # for identical text — the third randomness source this harness had to
        # find the hard way, after the nonce and the resume id. Production
        # still uses uuid4 here, which is why a stored score is reproducible
        # only up to its prompt TEMPLATE (see ADR-0017).
        chunk_id = uuid.uuid5(uuid.NAMESPACE_OID, f"{resume_uuid}:{chunk.index}")
        await session.execute(
            text("INSERT INTO resume_chunks (id, org_id, resume_id, chunk_index, "
                 "text_redacted, char_start, char_end, section, embedding) VALUES "
                 "(:id, :org, :r, :i, :t, :cs, :ce, :s, :e)"),
            {"id": chunk_id, "org": ORG_ID, "r": resume_uuid, "i": chunk.index,
             "t": chunk.text, "cs": chunk.char_start, "ce": chunk.char_end,
             "s": chunk.section, "e": str(vector)},
        )
    await session.commit()
    return redacted


async def run(dsn: str) -> dict:
    sets = all_sets()
    variants = [v for _, _, group in sets for v in group]

    engine = create_async_engine(dsn)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    gateway = LLMGateway(StubProvider())
    prompt = load("match_score", latest_version("match_score"))

    stored: dict[str, str] = {}   # variant key -> redacted text
    scores: dict[str, float] = {}  # variant key -> fused score

    async with maker() as session:
        await _reset(session)

        for base, axis, group in sets:
            slot = f"{base.base_id}:{axis.name}"
            resume_uuid = await _create_slot(session, slot)
            await session.commit()

            for v in group:
                key = variant_key(v)
                stored[key] = await _load_variant(session, resume_uuid, v)
                await handle_score_job(
                    session,
                    {"job_id": str(JOB_ID), "resume_id": str(resume_uuid)},
                    gateway=gateway,
                    prompt=prompt,
                    nonce=FIXED_NONCE,
                )
                await session.commit()
                match = (
                    await session.execute(
                        select(Match).where(Match.resume_id == resume_uuid)
                    )
                ).scalar_one()
                scores[key] = match.score
                # The row is keyed on (job, resume, prompt, model) and upserts,
                # so the next variant through this slot would otherwise be
                # compared against a stale row if scoring ever failed silently.
                await session.execute(
                    text("DELETE FROM matches WHERE resume_id = :r"), {"r": resume_uuid}
                )
                await session.commit()

    await engine.dispose()

    def measure(axis_name: str) -> dict:
        axis = next(a for _, a, _ in sets if a.name == axis_name)
        per_setting: dict[str, list[float]] = defaultdict(list)
        groups: list[dict] = []
        max_delta = 0.0
        # Spread among the settings that actually say something. Presence of a
        # line at all is a different question from which value the line holds,
        # and conflating them made "career_gap moves the score" look like a
        # finding when most of it was "a resume with an extra line is a
        # different resume".
        max_value_delta = 0.0

        for base, a, group in sets:
            if a.name != axis_name:
                continue
            group_scores = [scores[variant_key(v)] for v in group]
            for v, s in zip(group, group_scores, strict=True):
                per_setting[v.label].append(s)
            max_delta = max(max_delta, max(group_scores) - min(group_scores))
            present_scores = [
                scores[variant_key(v)] for v in group if v.label != "<absent>"
            ]
            if present_scores:
                max_value_delta = max(
                    max_value_delta, max(present_scores) - min(present_scores)
                )

            texts = {v.label: stored[variant_key(v)] for v in group}
            # "<absent>" is the setting where the line is not there at all. It
            # is excluded from the value check on purpose: the interesting
            # question is whether the system can tell WHICH value was
            # disclosed, which is separate from whether it can tell that
            # something was.
            present = {label: t for label, t in texts.items() if label != "<absent>"}
            groups.append({
                "base_id": base.base_id,
                "variants": len(group),
                "distinct_texts_all_settings": len(set(texts.values())),
                "distinct_texts_value_only": len(set(present.values())),
                "score_min": round(min(group_scores), 4),
                "score_max": round(max(group_scores), 4),
            })

        value_invisible = all(g["distinct_texts_value_only"] == 1 for g in groups)
        disclosure_invisible = all(g["distinct_texts_all_settings"] == 1 for g in groups)
        air = adverse_impact_ratio(axis_name, per_setting, threshold=SELECTION_THRESHOLD)

        return {
            "axis": axis_name,
            "removable": axis.removable,
            "rationale": axis.rationale,
            # Can the system tell which value the candidate has?
            "value_invisible_after_redaction": value_invisible,
            # Can it tell the candidate disclosed anything at all?
            "disclosure_invisible_after_redaction": disclosure_invisible,
            "max_score_delta": round(max_delta, 4),
            "max_score_delta_between_values": round(max_value_delta, 4),
            "mean_score_by_setting": {
                label: round(sum(v) / len(v), 4) for label, v in sorted(per_setting.items())
            },
            "adverse_impact_ratio": air.ratio,
            "adverse_impact_informative": air.informative,
            "selection_rates": {g.group: round(g.rate, 4) for g in air.groups},
            "groups": groups,
        }

    control = measure("control")
    # Identical documents, different row ids. Whatever they spread by is what
    # the pipeline does on its own, and no axis below it is evidence.
    noise_floor = control["max_score_delta"]

    axes: list[dict] = []
    for axis_name in dict.fromkeys(v.axis for v in variants):
        if axis_name == "control":
            continue
        result = measure(axis_name)
        result["exceeds_noise_floor"] = result["max_score_delta_between_values"] > noise_floor
        # For a removable axis the strong claim stands on text identity alone:
        # everything downstream is a function of the redacted text, so identical
        # text is an identical score by construction and needs no measurement.
        # For the rest, a spread inside the noise floor is not a finding — but
        # it is not a clearance either, and the report says so.
        result["verdict"] = (
            "value invisible to the scorer"
            if result["value_invisible_after_redaction"]
            else ("VALUE MOVES THE SCORE"
                  if result["exceeds_noise_floor"]
                  else "value visible, score unmoved")
        )
        axes.append(result)

    failures = [a["axis"] for a in axes if a["exceeds_noise_floor"]]

    return {
        "corpus": "counterfactual-v1",
        "bases": len({v.base_id for v in variants}),
        "axes": len(axes),
        "variants": len(variants),
        "selection_threshold": SELECTION_THRESHOLD,
        "llm_provider": "stub",
        "noise_floor": noise_floor,
        "noise_floor_source": (
            "Score spread across six byte-identical documents. The prompt "
            "carries a per-request nonce and the resume id, so two identical "
            "resumes do not produce identical prompts, and the stub provider "
            "echoes prompt content by construction. Any axis effect at or below "
            "this number is measurement noise."
        ),
        "what_this_is": (
            "Counterfactual invariance on synthetic resumes. It measures whether "
            "this pipeline responds to a protected-attribute signal it claims to "
            "remove. It is not an applicant-flow study, not a validated adverse-"
            "impact analysis, and passing it is not evidence the system is fair."
        ),
        "results": axes,
        "control": control,
        "axes_exceeding_noise_floor": failures,
    }


def main() -> int:
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5433")
    dsn = os.environ.get(
        "EVAL_DSN", f"postgresql+psycopg://screener:{password}@{host}:{port}/screener"
    )
    report = asyncio.run(run(dsn))

    print(f"\n  Counterfactual fairness probe: {report['variants']} variants "
          f"across {report['axes']} axes, {report['bases']} base resumes")
    print(f"  Selection threshold for AIR: {report['selection_threshold']}")
    print(f"  Noise floor (six identical documents): {report['noise_floor']:.3f}\n")
    header = (f"  {'axis':<18}{'value hidden':>14}{'disclosure hidden':>19}"
              f"{'d value':>9}{'d any':>8}{'AIR':>7}  verdict")
    print(header)
    print("  " + "-" * (len(header) - 2))
    for a in report["results"]:
        air = f"{a['adverse_impact_ratio']:.2f}" if a["adverse_impact_informative"] else "n/a"
        print(f"  {a['axis']:<18}{str(a['value_invisible_after_redaction']):>14}"
              f"{str(a['disclosure_invisible_after_redaction']):>19}"
              f"{a['max_score_delta_between_values']:>9.3f}"
              f"{a['max_score_delta']:>8.3f}{air:>7}  {a['verdict']}")

    print(f"\n  {report['noise_floor_source']}")
    print(f"\n  {report['what_this_is']}\n")
    print("  'd value' is the spread between settings that all say something; "
          "'d any' also\n  includes the setting where the line is absent, which is "
          "a genuinely different resume.\n")
    if report["axes_exceeding_noise_floor"]:
        print("  Axes where the VALUE moves the score: "
              f"{', '.join(report['axes_exceeding_noise_floor'])}")
    else:
        print("  No axis: changing which value a candidate discloses left every "
              "score identical.")

    out = ROOT / "evals" / "reports" / "fairness.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\n  written to {out.relative_to(ROOT)}")

    # A gate, not a report. Two ways to fail, and they are different failures:
    # a removable axis whose value survives redaction is a leak, and any axis
    # whose value moves the score past the noise floor is a leak that already
    # reached the arithmetic.
    leaked = [
        a["axis"] for a in report["results"]
        if a["removable"] and not a["value_invisible_after_redaction"]
    ]
    if leaked:
        print(f"\n  FAIL: a removable signal survived redaction on {', '.join(leaked)}")
    if report["axes_exceeding_noise_floor"]:
        print("\n  FAIL: the value of a protected attribute moved the score on "
              f"{', '.join(report['axes_exceeding_noise_floor'])}")
    return 1 if (leaked or report["axes_exceeding_noise_floor"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
