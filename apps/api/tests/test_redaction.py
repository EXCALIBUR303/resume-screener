"""AC-2 (redaction recall) and AC-3 (zero PII egress).

Recall is measured against *seeded* markers: values planted into synthetic
resumes whose positions are known ground truth, so the number is a measurement
rather than an impression.
"""

from __future__ import annotations

import pytest

from screener_api.privacy.redact import redact, rehydrate

NAMES = [
    "Priya Ramanathan",
    "Zoe Fictional",
    "James Okonkwo",
    "Wei Chen",
    "Aisha Bello",
    "Diego Fernandez",
    "Lars Andersen",
    "Fatima Al-Rashid",
    "Kenji Watanabe",
    "Olga Petrova",
    "Sam Invented",
    "Mary-Jane Placeholder",
]
EMAILS = ["p.raman@example.com", "zoe.f@mail.example.org", "j.okonkwo+cv@example.net"]
PHONES = ["+91 98765 44321", "+1 (555) 123-4567", "+44 20 7946 0958", "9876543210"]
PROFILES = ["linkedin.com/in/invented-person", "github.com/madeupuser"]
IDS = ["123-45-6789", "ABCDE1234F"]


def _resume(name: str, email: str, phone: str, profile: str, ident: str) -> tuple[str, str]:
    header = (
        f"{name}\n{email} | {phone} | {profile}\nFemale, married. D.O.B: 12/04/1997. ID {ident}\n"
    )
    body = (
        "\nSUMMARY\n"
        "Backend engineer with seven years building payment systems.\n"
        "\nWORK EXPERIENCE\n"
        f"Senior Backend Engineer, Invented Systems (2021-2026)\n"
        f"{name} led the migration to six services.\n"
        "\nEDUCATION\n"
        "B.Tech Computer Science, Imaginary Institute, 2019\n"
        "\nSKILLS\nPython, PostgreSQL, Redis\n"
    )
    return header + body, header


def _cases() -> list[tuple[str, str, list[str]]]:
    """(text, header, seeded markers that must not survive)."""
    out = []
    for i, name in enumerate(NAMES):
        email = EMAILS[i % len(EMAILS)]
        phone = PHONES[i % len(PHONES)]
        profile = PROFILES[i % len(PROFILES)]
        ident = IDS[i % len(IDS)]
        text, header = _resume(name, email, phone, profile, ident)
        out.append((text, header, [name, email, phone, profile, ident]))
    return out


CASES = _cases()


@pytest.mark.parametrize(
    ("text", "header", "markers"), CASES, ids=[c[2][0].replace(" ", "-") for c in CASES]
)
def test_seeded_markers_are_removed(text: str, header: str, markers: list[str]) -> None:
    result = redact(text, header=header)
    survivors = [m for m in markers if m in result.text]
    assert not survivors, f"PII survived redaction: {survivors}"


def test_ac2_recall_across_the_whole_set() -> None:
    """AC-2: >=98% of seeded markers removed. Reported as a measurement."""
    total = removed = 0
    missed: list[str] = []
    for text, header, markers in CASES:
        result = redact(text, header=header)
        for marker in markers:
            total += 1
            if marker not in result.text:
                removed += 1
            else:
                missed.append(marker)

    recall = removed / total
    assert recall >= 0.98, (
        f"AC-2 recall {recall:.1%} ({removed}/{total}); survivors: {sorted(set(missed))}"
    )


def test_names_are_caught_even_when_ner_misses_them() -> None:
    """The structural layer exists because NER alone measured 90% on names.
    Position in the header is more dependable than recognition."""
    text, header = _resume(
        "Zoe Fictional", "z@example.com", "9876543210", "github.com/z", "ABCDE1234F"
    )
    result = redact(text, header=header, use_ner=False)
    assert "Zoe Fictional" not in result.text


def test_protected_attributes_are_removed() -> None:
    text, header = _resume(
        "Sam Invented", "s@example.com", "9876543210", "github.com/s", "ABCDE1234F"
    )
    result = redact(text, header=header)
    lowered = result.text.lower()
    for attribute in ("female", "married", "d.o.b"):
        assert attribute not in lowered, f"{attribute!r} reached the model"


def test_graduation_year_is_removed_as_an_age_proxy() -> None:
    text, header = _resume("Wei Chen", "w@example.com", "9876543210", "github.com/w", "ABCDE1234F")
    assert "2019" not in redact(text, header=header).text


def test_graduation_year_can_be_kept_when_configured() -> None:
    text, header = _resume("Wei Chen", "w@example.com", "9876543210", "github.com/w", "ABCDE1234F")
    assert "2019" in redact(text, header=header, redact_grad_years=False).text


def test_skills_and_experience_survive_redaction() -> None:
    """Over-redaction destroys the thing being measured. The signal a screener
    actually needs must come through intact."""
    text, header = _resume(
        "Priya Ramanathan", "p@example.com", "9876543210", "github.com/p", "ABCDE1234F"
    )
    result = redact(text, header=header)
    for signal in ("Python", "PostgreSQL", "Redis", "Backend engineer", "migration"):
        assert signal in result.text, f"redaction destroyed the signal {signal!r}"


def test_line_structure_is_preserved() -> None:
    """Regression: an untrimmed NER span glued tokens together
    ("PERSON_1EMAIL_1"), destroying the line breaks section detection needs."""
    text, header = _resume(
        "Priya Ramanathan", "p@example.com", "9876543210", "github.com/p", "ABCDE1234F"
    )
    result = redact(text, header=header)
    assert "PERSON_1EMAIL" not in result.text
    assert result.text.count("\n") >= text.count("\n") - 2


def test_the_same_entity_gets_one_stable_token() -> None:
    """A name appearing twice must map to one token, or the model sees two
    people where there is one."""
    text, header = _resume(
        "Priya Ramanathan", "p@example.com", "9876543210", "github.com/p", "ABCDE1234F"
    )
    result = redact(text, header=header)
    assert result.text.count("PERSON_1") >= 2
    assert "PERSON_2" not in result.text


def test_rehydration_restores_the_original() -> None:
    text, header = _resume(
        "Priya Ramanathan", "p@example.com", "9876543210", "github.com/p", "ABCDE1234F"
    )
    result = redact(text, header=header)
    assert "Priya Ramanathan" in rehydrate(result.text, result.token_map)


def test_redaction_is_deterministic() -> None:
    text, header = _resume("Wei Chen", "w@example.com", "9876543210", "github.com/w", "ABCDE1234F")
    a, b = redact(text, header=header), redact(text, header=header)
    assert a.text == b.text
    assert a.token_map == b.token_map


def test_empty_and_whitespace_input_is_safe() -> None:
    for value in ("", "   ", "\n\n"):
        assert redact(value).text == value


def test_token_map_is_the_only_way_back() -> None:
    """Without the map the text is not reversible — that is what makes storing
    it separately and encrypted meaningful."""
    text, header = _resume(
        "Olga Petrova", "o@example.com", "9876543210", "github.com/o", "ABCDE1234F"
    )
    result = redact(text, header=header)
    assert "Olga Petrova" not in rehydrate(result.text, {})


def test_pathological_input_does_not_hang() -> None:
    for value in ("a" * 50_000, "\n" * 5_000, "@" * 5_000, "+1 " * 5_000):
        redact(value[:20_000])


# ---- Regressions found by running a real document through the pipeline --------
# Every one of these passed the fixture corpus and failed on the first realistic
# resume. The corpus always used the full name in the body; real resumes do not.


def test_a_standalone_first_name_does_not_survive() -> None:
    """The leak: 'Priya designed payment services...' kept the first name,
    because propagation only matched the exact full string."""
    text = (
        "Priya Ramanathan\npriya@example.com\n\nWORK EXPERIENCE\n"
        "Priya designed payment services in Python on PostgreSQL.\n"
        "Ramanathan led the migration using Docker and Redis.\n"
    )
    result = redact(text, header=text.split("WORK")[0])
    assert "Priya" not in result.text
    assert "Ramanathan" not in result.text


def test_one_person_gets_exactly_one_token() -> None:
    """A full name, a first name and a surname are one candidate. Three tokens
    would tell the model there are three people in the resume."""
    text = (
        "Priya Ramanathan\npriya@example.com\n\nWORK EXPERIENCE\n"
        "Priya designed services. Ramanathan led the migration.\n"
    )
    result = redact(text, header=text.split("WORK")[0])
    person_tokens = {k for k in result.token_map if k.startswith("PERSON")}
    assert person_tokens == {"PERSON_1"}


@pytest.mark.parametrize(
    "line",
    [
        "Python, PostgreSQL, Redis, Docker, Kubernetes, REST APIs, pytest",
        "Built services in Python on PostgreSQL.",
        "Migrated using Docker and Redis.",
        "Experience with Kafka, Airflow and Spark.",
    ],
)
def test_technology_names_are_never_redacted(line: str) -> None:
    """NER classifies Redis, Docker and Oracle as organisations. Redacting them
    destroys the primary scoring signal.

    Two distinct bugs lived here: a whole-span lookup let "Redis, Docker" through
    as one unmatched string, and connector words made "Python on PostgreSQL"
    fail the allowlist.
    """
    text = f"Zoe Fictional\nz@example.com\n\nSKILLS\n{line}\n"
    result = redact(text, header=text.split("SKILLS")[0])
    for token in (
        "Python",
        "PostgreSQL",
        "Redis",
        "Docker",
        "Kubernetes",
        "Kafka",
        "Airflow",
        "Spark",
        "pytest",
    ):
        if token in line:
            assert token in result.text, f"redaction destroyed the skill {token!r}"


def test_employment_date_ranges_survive() -> None:
    """(2021-2026) matched the phone pattern and was redacted, destroying the
    years-of-experience input the deterministic scorer is built on."""
    text = (
        "Zoe Fictional\nz@example.com\n\nWORK EXPERIENCE\n"
        "Senior Backend Engineer, Invented Systems Ltd (2021-2026)\n"
    )
    result = redact(text, header=text.split("WORK")[0])
    assert "2021" in result.text
    assert "2026" in result.text


def test_ner_spans_do_not_swallow_the_following_line() -> None:
    """NER returned 'Name\\nemail@example.com' as one PERSON span. Being longest
    it won the merge and displaced the EMAIL pattern span — a priority
    inversion, since patterns are more reliable than the model, not less."""
    text = "Priya Ramanathan\npriya@example.com | +91 98765 44321\n\nSUMMARY\nEngineer.\n"
    result = redact(text, header=text.split("SUMMARY")[0])
    assert "EMAIL_1" in result.text
    assert "PHONE_1" in result.text
    assert "PERSON_1\n" in result.text
