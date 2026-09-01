"""Deterministic PII patterns.

These run before NER and carry most of the recall. A regex for an email address
is not cleverer than a model, but it is *reliable*, and reliability is what a
privacy control needs. NER handles what patterns cannot: names in prose.
"""

from __future__ import annotations

import re
from typing import Final, NamedTuple


class Pattern(NamedTuple):
    entity: str
    regex: re.Pattern[str]


# Ordered: earlier patterns win, so a phone inside a longer ID is not split.
PATTERNS: Final[tuple[Pattern, ...]] = (
    Pattern("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")),
    Pattern("URL", re.compile(r"\bhttps?://[^\s<>\"')]+", re.I)),
    Pattern(
        "PROFILE",
        re.compile(
            r"\b(?:linkedin\.com|github\.com|gitlab\.com|x\.com|twitter\.com)/[\w./-]+", re.I
        ),
    ),
    # International and national formats. Bounded so it cannot fire inside a
    # UUID or hash — the same mistake the log redactor made in M1.
    Pattern(
        "PHONE",
        re.compile(
            r"(?<![0-9a-zA-Z-])"
            r"(?:\+\d{1,3}[\s.-]?)?"
            r"(?:\(?\d{2,4}\)?[\s.-]?){2,4}\d{2,4}"
            r"(?![0-9a-zA-Z-])"
        ),
    ),
    Pattern("US_SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    Pattern("AADHAAR", re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")),
    Pattern("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    Pattern("PASSPORT", re.compile(r"\b[A-Z]\d{7}\b")),
    Pattern(
        "DOB",
        re.compile(
            r"\b(?:d\.?o\.?b\.?|date of birth|born)\s*[:\-]?\s*"
            r"[\d]{1,4}[/\-. ][\d]{1,2}[/\-. ][\d]{2,4}",
            re.I,
        ),
    ),
    Pattern("POSTAL_UK", re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b")),
    # POSTAL_US is deliberately NOT in this table. `\b\d{5}\b` matches every
    # five-digit number, and on an engineering resume those are overwhelmingly
    # achievement metrics: "50000 concurrent connections", "12000 ms", "10000
    # records nightly". It is context-gated below (ADR-0017).
)

# A US ZIP looks like any other five-digit number, so position in an address is
# the only thing that distinguishes it. Same technique as GRAD_YEAR: the
# question "is this a postal code" is answered by looking at what sits beside
# it, not by the shape of the digits alone.
POSTAL_US = Pattern("POSTAL_US", re.compile(r"(?<!\d)\d{5}(?:-\d{4})?(?!\d)"))

POSTAL_CONTEXT: Final[re.Pattern[str]] = re.compile(
    r"\bzip\b|\bpostal\b|\bpin\s?code\b|\baddress\b|"
    # "Springfield, IL 62704" — a state abbreviation immediately before.
    r",\s*(?:A[KLRZ]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|"
    r"N[CDEHJMVY]|O[HKR]|P[AR]|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])\s*$",
    re.I,
)
POSTAL_WINDOW = 40

# Attributes that are unlawful or unwise to consider in hiring. Removed before
# the model sees anything, so it cannot reason about them even accidentally.
# This is not a complete list and does not make the system compliant — it
# reduces obvious exposure. See the limitations section of the README.
PROTECTED_TERMS: Final[dict[str, tuple[str, ...]]] = {
    "GENDER": (
        "male",
        "female",
        "woman",
        "non-binary",
        "nonbinary",
        "he/him",
        "she/her",
        "they/them",
        # "mr.", "mrs." and "ms." used to sit here and could never match; they
        # are shaped patterns below now. Leaving the dead entries in place would
        # keep implying coverage this table does not have.
    ),
    "MARITAL": ("married", "unmarried", "divorced", "widowed", "spouse"),
    "RELIGION": (
        "hindu",
        "muslim",
        "christian",
        "sikh",
        "jewish",
        "buddhist",
        "catholic",
        "atheist",
    ),
    "NATIONALITY": (
        "indian national",
        "us citizen",
        "citizenship",
        "visa status",
        "green card",
        "work permit",
        "nationality",
    ),
    "DISABILITY": ("disability", "wheelchair", "neurodivergent", "adhd", "autistic"),
    # "age:" used to live here and could never match. Every term in this table
    # is wrapped in `\b...\b`, and a `\b` after a colon requires a word
    # character next — so it fired on "Age:34" and never on "Age: 34", which is
    # how a resume actually writes it. A control that cannot fire is worse than
    # no control, because the table implies coverage it does not have. It is a
    # real pattern below instead (ADR-0017).
    "AGE": ("years old", "d.o.b", "date of birth"),
    "PHOTO": ("photograph attached", "photo attached", "passport photo"),
}

# Protected attributes whose shape needs more than a word list.
SHAPED_PROTECTED_PATTERNS: Final[tuple[Pattern, ...]] = (
    Pattern("AGE", re.compile(r"\bage\s*[:\-]\s*\d{1,3}\b", re.I)),
    # "mr.", "mrs.", "ms." and "miss" were in the word list above and three of
    # them could never match, for the same reason "age:" could not: the list is
    # wrapped in `\b...\b`, and a `\b` after a period needs a word character
    # next. "Ms. Alex Placeholder" has a space there, so the honorific — a
    # direct gender signal, sitting in the header of the document — went
    # through untouched. Matched here by shape instead: an honorific is the
    # thing in front of a capitalised name (ADR-0017).
    # The negative lookbehind is milliseconds: "Reduced p99 from 400 ms.
    # Deployed weekly." matched the honorific rule and ate the unit.
    # `(?-i:[A-Z])` turns IGNORECASE off for the lookahead alone. With plain
    # `[A-Z]` under re.I the class also matches lowercase, so "cache miss rate"
    # satisfied "honorific followed by a capitalised name" and the unit was
    # deleted. A case-insensitive character class is a quiet way to widen a
    # pattern far past what it looks like it says.
    Pattern(
        "GENDER",
        re.compile(r"(?<!\d )\b(?:mr|mrs|ms|mx|miss)\.?(?=\s+(?-i:[A-Z]))", re.I),
    ),
    # The reason someone stepped away from work is a protected-attribute proxy:
    # parental leave and caregiving track sex, age and disability. The gap
    # itself is legitimate information and stays — including its dates, which
    # the experience arithmetic reads — so only the parenthetical reason is
    # taken, via the `redact` group.
    #
    # It is taken for EVERY reason, "sabbatical" included. Redacting only the
    # protected ones would leave their absence as the signal, which is the same
    # trap as redacting a name but leaving the length of the redaction.
    Pattern(
        "BREAK_REASON",
        re.compile(
            r"\b(?:career\s+(?:break|gap)|employment\s+gap|break\s+in\s+employment|"
            r"sabbatical|hiatus)\b[^\n(]{0,40}(?P<redact>\([^)\n]{1,60}\))",
            re.I,
        ),
    ),
    Pattern(
        "BREAK_REASON",
        re.compile(
            r"\b(?:maternity|paternity|parental|adoption|caregiving|carer|"
            r"compassionate|medical|bereavement)\s+leave\b",
            re.I,
        ),
    ),
    # A whole labelled field, label included. Matching only the value left
    # "Marital status:" sitting in the text with its answer removed, which is
    # still a disclosure — the reader learns the candidate filled that field in.
    Pattern(
        "PERSONAL_FIELD",
        re.compile(
            r"\b(?:marital\s+status|marital|gender|sex|pronouns?|nationality|"
            r"citizenship|visa\s+status|religion|caste|ethnicity|race|"
            r"disability|date\s+of\s+birth|d\.?o\.?b\.?|age|"
            # An accommodation request is a disability disclosure, and it was
            # the last thing still reaching the model after the rest of the
            # personal-details block had been removed around it. The colon is
            # required so "built the accommodations booking service" is safe.
            r"(?:(?:requires?|needs?|reasonable)\s+)?"
            r"(?:accommodations?|adjustments?|accessibility\s+needs?)"
            r")\s*[:\-][^\n|;]*",
            re.I,
        ),
    ),
)

# Educational institutions, matched by shape rather than left to NER.
#
# ADR-0017 recorded institution redaction as inconsistent and did not solve it.
# Measured across ten institutions on an otherwise identical line, NER redacted
# eight and produced FIVE different shapes:
#
#   Stanford University              -> ORG_1
#   Imaginary Institute of Technology -> Imaginary ORG_1     (partial)
#   Nowhere Polytechnic              -> Nowhere Polytechnic  (untouched)
#   Example College                  -> ORG_1, and the DEGREE went with it
#
# An institution is a proxy for background in the same way a graduation year is
# a proxy for age, so which candidates get one removed cannot depend on whether
# a statistical model happened to recognise the name.
#
# Emitted as ORG, deliberately sharing the employer numbering: if this layer
# emitted a distinct entity, the redacted text would differ depending on WHICH
# layer caught the institution, which is the same invariance failure ADR-0017
# is about.
#
# The `redact` group excludes a trailing "of <Subject>" from the match when the
# subject is a field of study — "School of Engineering" is the institution,
# but "Institute of Technology" should not swallow a following degree subject.
INSTITUTION: Final[Pattern] = Pattern(
    "ORG",
    re.compile(
        r"\b(?:[A-Z][\w&.'\u2019-]*\s+(?:of\s+|and\s+|the\s+)?){1,4}"
        r"(?:University|College|Institute|Institution|School|Academy|Polytechnic|Seminary)"
        r"(?:\s+of\s+(?:[A-Z][\w&.'\u2019-]*(?:\s+and)?\s*){1,3})?"
        r"|\b(?:University|College|Institute|School|Academy|Polytechnic)"
        r"\s+of\s+(?:[A-Z][\w&.'\u2019-]*(?:\s+and)?\s*){1,3}"
    ),
)


# Entities that are DELETED rather than tokenised.
#
# Pseudonymisation is the right default: a recruiter re-hydrates PERSON_1 to
# see who they are looking at. No one ever needs to re-hydrate a candidate's
# religion, so the token buys nothing — and it costs something real. A resume
# that declared pronouns produced `EMAIL_1 | PHONE_1 | GENDER_1` where one that
# did not produced `EMAIL_1 | PHONE_1`. The value was hidden and the *act of
# disclosing* was not, which is its own protected signal: who volunteers a
# pronoun line correlates with exactly the attributes this layer exists to
# remove. Deleting closes that channel; there is no rehydration to lose
# (ADR-0017).
DELETE_NOT_TOKENISE: Final[frozenset[str]] = frozenset(
    {
        "GENDER",
        "MARITAL",
        "RELIGION",
        "NATIONALITY",
        "DISABILITY",
        "AGE",
        "PHOTO",
        "GRAD_YEAR",
        "BREAK_REASON",
        "PERSONAL_FIELD",
    }
)

# Terms that are protected attributes on a personal-details form and ordinary
# engineering vocabulary everywhere else. Matched bare, every one of these
# destroyed real signal (ADR-0017):
#
#   "man"      -> man-in-the-middle, man-in-the-browser, man page
#   "miss"     -> cache miss
#   "single"   -> Single Sign-On, single point of failure, single-tenant
#   "disabled" -> Disabled TLS 1.0, disabled the legacy endpoint
#
# In an application-security screener those are exactly the phrases that ought
# to score. They are only redacted when a demographic cue sits nearby.
AMBIGUOUS_PROTECTED_TERMS: Final[dict[str, tuple[str, ...]]] = {
    "GENDER": ("man", "miss"),
    "MARITAL": ("single",),
    "DISABILITY": ("disabled",),
}

# The vocabulary of a personal-details block. A resume that volunteers a
# protected attribute nearly always does it as a labelled field.
PROTECTED_CONTEXT: Final[re.Pattern[str]] = re.compile(
    r"\bmarital\b|\bmarriage\b|\bgender\b|\bsex\b|\bpronouns?\b|\bdependants?\b|"
    r"\bdependents?\b|\bchildren\b|\bdisabilit(?:y|ies)\b|\baccommodations?\b|"
    r"\bimpairment\b|\bpersonal\s+details\b|\bpersonal\s+information\b|"
    r"\bdate\s+of\s+birth\b|\bd\.?o\.?b\.?\b|\bstatus\s*[:\-]",
    re.I,
)
PROTECTED_WINDOW = 40

PROTECTED_PATTERNS: Final[tuple[Pattern, ...]] = (
    *(
        Pattern(
            entity,
            re.compile(r"\b(?:" + "|".join(re.escape(t) for t in terms) + r")\b", re.I),
        )
        for entity, terms in PROTECTED_TERMS.items()
    ),
    *SHAPED_PROTECTED_PATTERNS,
)

AMBIGUOUS_PROTECTED_PATTERNS: Final[tuple[Pattern, ...]] = tuple(
    Pattern(
        entity,
        re.compile(r"\b(?:" + "|".join(re.escape(t) for t in terms) + r")\b", re.I),
    )
    for entity, terms in AMBIGUOUS_PROTECTED_TERMS.items()
)

# Graduation years are a well-known age proxy.
# Graduation years are a well-known age proxy. Python's `re` forbids
# variable-width lookbehind, so the "is this year near an education keyword"
# question is answered in code (see redact._protected_spans) rather than by an
# unreadable single pattern.
YEAR = Pattern("GRAD_YEAR", re.compile(r"(?<!\d)(?:19[5-9]\d|20[0-4]\d)(?!\d)"))

EDUCATION_CONTEXT: Final[re.Pattern[str]] = re.compile(
    r"b\.?tech|b\.?e\b|b\.?sc|m\.?tech|m\.?sc|mba|ph\.?d|bachelor|master|degree|"
    r"university|college|institute|school|graduated|class of",
    re.I,
)

EDUCATION_WINDOW = 60

# A phone number carries at least this many digits. Without the floor, the
# pattern matched "(2021-2026)" — an employment date range — and redacted it as
# a phone number, destroying the years-of-experience input the deterministic
# scorer depends on. Over-redaction is not the safe direction; it is a different
# failure.
PHONE_MIN_DIGITS = 9

# Technology names are the primary scoring signal. NER routinely classifies them
# as organisations ("Redis", "Oracle", "Apache", "Databricks" are all companies
# too), so they are protected from redaction explicitly. Redacting a skill
# silently degrades every score that depends on it.
NEVER_REDACT: Final[frozenset[str]] = frozenset(
    t.lower()
    for t in (
        # languages
        "Python",
        "Java",
        "JavaScript",
        "TypeScript",
        "Go",
        "Golang",
        "Rust",
        "Ruby",
        "PHP",
        "Swift",
        "Kotlin",
        "Scala",
        "Perl",
        "Haskell",
        "Elixir",
        "Clojure",
        "C",
        "C++",
        "C#",
        "R",
        "MATLAB",
        "SQL",
        "Bash",
        "Shell",
        # data stores
        "PostgreSQL",
        "Postgres",
        "MySQL",
        "MariaDB",
        "SQLite",
        "Redis",
        "MongoDB",
        "Cassandra",
        "DynamoDB",
        "Elasticsearch",
        "OpenSearch",
        "Neo4j",
        "ClickHouse",
        "Snowflake",
        "BigQuery",
        "Redshift",
        "Oracle",
        "SQL Server",
        "pgvector",
        # platforms and infra
        "AWS",
        "Azure",
        "GCP",
        "Google Cloud",
        "Kubernetes",
        "Docker",
        "Terraform",
        "Ansible",
        "Jenkins",
        "GitHub",
        "GitLab",
        "Git",
        "Linux",
        "Nginx",
        "Apache",
        "Kafka",
        "RabbitMQ",
        "Celery",
        "Airflow",
        "Spark",
        "Hadoop",
        "Flink",
        "Prometheus",
        "Grafana",
        "Datadog",
        "Splunk",
        "Vault",
        "Consul",
        # frameworks
        "Django",
        "Flask",
        "FastAPI",
        "Spring",
        "Rails",
        "Express",
        "React",
        "Angular",
        "Vue",
        "Svelte",
        "Next.js",
        "Node.js",
        "Deno",
        ".NET",
        "Laravel",
        "Symfony",
        "PyTorch",
        "TensorFlow",
        "Keras",
        "scikit-learn",
        "pandas",
        "NumPy",
        "Jupyter",
        "Pytest",
        "JUnit",
        "Selenium",
        "Playwright",
        "Cypress",
        # practices
        "REST",
        "GraphQL",
        "gRPC",
        "OAuth",
        "SAML",
        "CI/CD",
        "Agile",
        "Scrum",
        "Kanban",
        "TDD",
        "DDD",
        "Microservices",
    )
)

# Degrees and fields of study. NER classifies "B.Tech Computer Science" and
# "MSc Artificial Intelligence" as ORGANIZATION, so the qualification was
# redacted while the institution beside it survived — precisely inverted, since
# the degree is the signal and the institution is the proxy (ADR-0017).
DEGREE_TERMS: Final[frozenset[str]] = frozenset(
    t.lower()
    for t in (
        # award names
        "B.Tech",
        "BTech",
        "B.E",
        "BE",
        "B.Sc",
        "BSc",
        "B.A",
        "BA",
        "B.Com",
        "BBA",
        "BCA",
        "M.Tech",
        "MTech",
        "M.E",
        "M.Sc",
        "MSc",
        "M.A",
        "MA",
        "MBA",
        "MCA",
        "M.Phil",
        "MPhil",
        "Ph.D",
        "PhD",
        "Doctorate",
        "Bachelor",
        "Bachelors",
        "Master",
        "Masters",
        "Diploma",
        "Associate",
        "Honours",
        "Honors",
        # connective words that appear inside a degree name
        "of",
        "in",
        "science",
        "sciences",
        "arts",
        "engineering",
        "technology",
        "computer",
        "information",
        "software",
        "electrical",
        "electronics",
        "mechanical",
        "civil",
        "chemical",
        "mathematics",
        "statistics",
        "physics",
        "data",
        "artificial",
        "intelligence",
        "business",
        "administration",
        "management",
        "cybersecurity",
        "security",
        # Abbreviated fields. Without these "B.Tech CS" failed the
        # all-tokens test and the NER span over it was redacted as a
        # PERSON — the degree destroyed again, by a different route than
        # the one ADR-0017 found.
        "cs",
        "cse",
        "ece",
        "eee",
        "ee",
        "it",
        "ai",
        "ml",
        "ds",
        "btech",
        "mtech",
        "bsc",
        "msc",
    )
)


def is_degree_phrase(value: str) -> bool:
    """True when every meaningful token is degree or field-of-study vocabulary.

    Deliberately strict: "Bachelor of Science in Computer Engineering" passes,
    "Bachelor Institute of Technology" does not, because "institute" is absent
    from the table. A false negative here costs a redacted degree; a false
    positive would leak an institution name.
    """
    parts = [p.strip(" .,;:()[]") for p in re.split(r"[\s,;/|&]+", value)]
    meaningful = [p for p in parts if p]
    return bool(meaningful) and all(p.lower() in DEGREE_TERMS for p in meaningful)
