"""Counterfactual invariance, and a regression for every leak that found it.

These run offline and without a database. That is not a shortcut — it is the
whole argument. Everything downstream of redaction (chunking, embedding,
retrieval, prompt, verification, fusion) is a pure function of the redacted
text and the job posting, so two resumes that redact to the same bytes get the
same score by construction. Asserting on the text is therefore a *stronger*
claim than asserting two measured scores came out close, and it needs no
Postgres and no model.

The full-pipeline version lives in `evals/fairness/run.py` and confirms it
end to end; `make fairness` runs it.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

from screener_api.privacy.redact import Span, _merge, redact
from screener_api.scoring.deterministic import score_deterministic

# The counterfactual corpus lives with the eval harness, not the app.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from evals.fairness.variants import AXES, BASES, counterfactual_set


def _redact(v) -> str:
    return redact(v.text, header=v.header).text


# --------------------------------------------------------------------------- #
#  The property the whole milestone is about
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("base", BASES, ids=lambda b: b.base_id)
@pytest.mark.parametrize("axis", AXES, ids=lambda a: a.name)
def test_the_value_of_a_protected_attribute_never_survives_redaction(base, axis) -> None:
    """Changing WHICH value a candidate discloses must change nothing.

    Whether they disclosed at all is a separate question — an affinity group or
    a location is a real extra line of content — so the absent setting is
    excluded here and checked separately below.
    """
    present = [v for v in counterfactual_set(base, axis) if v.label != "<absent>"]
    texts = {_redact(v) for v in present}
    assert len(texts) == 1, (
        f"axis {axis.name!r} leaks its value: {len(texts)} distinct redacted "
        f"texts across {len(present)} settings that differ only in a "
        f"protected-attribute signal"
    )


@pytest.mark.parametrize(
    "axis_name", ["name", "gender_marker", "personal_details", "graduation_year"]
)
def test_disclosing_a_protected_attribute_is_itself_invisible(axis_name) -> None:
    """For these axes even the ACT of disclosing must leave no trace.

    Pseudonymisation used to defeat this: a resume declaring pronouns produced
    `EMAIL_1 | PHONE_1 | GENDER_1` where one that did not produced `EMAIL_1 |
    PHONE_1`. The value was hidden, the disclosure was not, and who volunteers
    that line correlates with the very attributes being removed. Protected
    entities are deleted rather than tokenised for this reason.
    """
    axis = next(a for a in AXES if a.name == axis_name)
    texts = {_redact(v) for v in counterfactual_set(BASES[0], axis)}
    assert len(texts) == 1, f"{axis_name}: disclosure is still visible"


def test_a_name_does_not_change_a_single_scoring_input() -> None:
    """The end-to-end claim, stated as arithmetic rather than as text identity."""
    axis = next(a for a in AXES if a.name == "name")
    scores = {
        (
            round(d.skill_score, 6),
            round(d.experience_score, 6),
            d.years_found,
            d.looks_stuffed,
        )
        for d in (
            score_deterministic(
                _redact(v),
                required_skills=["Python", "PostgreSQL", "Kubernetes"],
                nice_to_have=["Redis", "Docker"],
                min_years=5,
                hard_requirements=[],
            )
            for v in counterfactual_set(BASES[0], axis)
        )
    }
    assert len(scores) == 1


# --------------------------------------------------------------------------- #
#  Regressions. Each one is a leak the harness above actually found.
# --------------------------------------------------------------------------- #


def test_merge_prefers_the_reliable_layer_over_an_overlapping_ner_fragment() -> None:
    """NER returned `"| +91"` at offset 34; the phone pattern matched at 36.

    The old merge sorted by start position and dropped anything overlapping the
    span before it, so the five-character fragment suppressed the fifteen-
    character phone match. Whether NER produced that fragment depended on the
    candidate's name, which is how a name ended up changing the redaction of a
    phone number.
    """
    fragment = Span(34, 39, "PERSON", "| +91", "ner")
    phone = Span(36, 51, "PHONE", "+91 90000 00000", "pattern")
    kept = _merge([fragment, phone])
    assert [s.entity for s in kept] == ["PHONE"]


@pytest.mark.parametrize(
    "line",
    [
        "Mitigated man-in-the-middle attacks by pinning certificates.",
        "Ran a threat model covering man-in-the-browser and replay.",
        "Reduced cache miss rate from 40% to 6%.",
        "Disabled TLS 1.0 and 1.1 across the estate.",
        "Implemented Single Sign-On with SAML across 20 services.",
        "Removed the single point of failure in the ingest path.",
        "Owned the single-tenant to multi-tenant migration.",
        "Wrote the man page for the internal CLI.",
    ],
)
def test_protected_terms_do_not_eat_security_vocabulary(line: str) -> None:
    """Every one of these was destroyed by a bare protected-term match.

    In an application-security screener they are the phrases that ought to
    score. Same failure as ADR-0009, in the layer ADR-0009 did not audit.
    """
    assert redact(line, use_ner=False).text == line


@pytest.mark.parametrize(
    "line",
    [
        "Handled 50000 concurrent connections.",
        "Reduced p99 latency from 12000 ms to 400 ms.",
        "Processed 10000 records nightly.",
        "Reduced p99 from 400 ms. Deployed weekly.",
    ],
)
def test_metrics_survive(line: str) -> None:
    """`\\b\\d{5}\\b` matched every five-digit number, and on a resume those are
    quantified impact, not postal codes."""
    assert redact(line, use_ner=False).text == line


@pytest.mark.parametrize(
    "line",
    [
        "Address: 12 Fake Street, Springfield, IL 62704",
        "Springfield, IL 62704",
        "Zip 94107",
    ],
)
def test_a_postal_code_with_address_context_is_still_caught(line: str) -> None:
    assert "POSTAL_US" in redact(line, use_ner=False).text


@pytest.mark.parametrize(
    ("line", "cue"),
    [
        ("Personal Information: single, no dependents.", "single"),
        ("Gender: man", "man"),
        ("Disability: registered disabled, requires accommodations.", "disabled"),
    ],
)
def test_ambiguous_terms_are_redacted_when_a_demographic_cue_is_near(line, cue) -> None:
    assert cue not in redact(line, use_ner=False).text


def test_a_degree_is_not_an_organisation() -> None:
    """NER returns "B.Tech Computer Science" as ORGANIZATION, so the
    qualification was redacted while the institution beside it survived."""
    out = redact("EDUCATION\nB.Tech Computer Science, Imaginary Institute, 2018").text
    assert "B.Tech Computer Science" in out


@pytest.mark.parametrize(
    "line",
    ["Ms. Alex Placeholder", "Mr Smith reported to me.", "Contact: Mrs. Placeholder"],
)
def test_honorifics_are_redacted(line: str) -> None:
    """They were in the word list and could never match: the list is wrapped in
    `\\b...\\b`, and a `\\b` after a period needs a word character next."""
    out = redact(line, use_ner=False).text
    assert not any(h in out for h in ("Ms.", "Mr ", "Mrs."))


def test_age_is_redacted_in_the_form_a_resume_actually_writes_it() -> None:
    """ "age:" was in the word list with the same trailing-boundary defect: it
    fired on "Age:34" and never on "Age: 34"."""
    assert "34" not in redact("Age: 34", use_ner=False).text
    assert redact("Average: 34 ms per request", use_ner=False).text.startswith("Average")


def test_a_labelled_field_loses_its_value_not_just_its_label() -> None:
    """ "Nationality: Indian" produced "NATIONALITY_1: Indian" — the word naming
    the attribute removed, the attribute itself left behind."""
    out = redact("Nationality: Indian | Languages: English, Hindi", use_ner=False).text
    assert "Indian" not in out
    assert "Languages: English, Hindi" in out


@pytest.mark.parametrize("reason", ["parental leave", "caregiving", "sabbatical", "medical leave"])
def test_the_reason_for_a_career_break_is_removed_and_the_dates_are_not(reason) -> None:
    """Parental leave and caregiving track sex, age and disability. The gap is
    legitimate information; the reason is a proxy. Every reason goes, including
    "sabbatical", because redacting only the protected ones would make their
    absence the signal."""
    out = redact(f"Career break 2022-2023 ({reason}).", use_ner=False).text
    assert reason not in out
    assert "2022-2023" in out


def test_every_career_break_reason_redacts_to_the_same_text() -> None:
    outs = {
        redact(f"Career break 2022-2023 ({r}).", use_ner=False).text
        for r in ("parental leave", "caregiving", "sabbatical", "bereavement leave")
    }
    assert len(outs) == 1


def test_an_accommodation_request_does_not_reach_the_model() -> None:
    """The last thing still standing after the rest of a personal-details block
    had been removed around it."""
    assert redact("Requires accommodations: screen reader", use_ner=False).text == ""
    # ...without eating a product that happens to be called that.
    line = "Built the accommodations booking service for 40 hotels."
    assert redact(line, use_ner=False).text == line


def test_a_protected_attribute_is_deleted_rather_than_tokenised() -> None:
    with_marker = redact("a@example.com | +91 90000 00000 | (she/her)", use_ner=False).text
    without = redact("a@example.com | +91 90000 00000", use_ner=False).text
    assert with_marker == without


def test_a_line_holding_only_protected_attributes_is_dropped_entirely() -> None:
    out = redact(
        "PERSONAL DETAILS\nMarital status: Single | Gender: Male | Nationality: Indian\n"
        "Languages: English",
        use_ner=False,
    ).text
    assert out == "PERSONAL DETAILS\nLanguages: English"


def test_a_deletion_takes_exactly_one_separator_with_it() -> None:
    """Both sides was the opposite mistake, and it fused the neighbours.

    "LOCATION_2 | Female, married | DOB_1" came out as "LOCATION_2DOB_1" — two
    unrelated identifiers glued into one token that means nothing.
    """
    out = redact(
        "Bengaluru, India | Female, married | D.O.B: 12/04/1997 | PAN ABCDE1234F",
        use_ner=False,
    ).text
    assert "|" in out
    assert "DOB_1 | PAN" in out
    assert "Female" not in out and "married" not in out
