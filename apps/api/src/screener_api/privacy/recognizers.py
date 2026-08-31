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
    Pattern("POSTAL_US", re.compile(r"\b\d{5}(?:-\d{4})?\b")),
)

# Attributes that are unlawful or unwise to consider in hiring. Removed before
# the model sees anything, so it cannot reason about them even accidentally.
# This is not a complete list and does not make the system compliant — it
# reduces obvious exposure. See the limitations section of the README.
PROTECTED_TERMS: Final[dict[str, tuple[str, ...]]] = {
    "GENDER": (
        "male",
        "female",
        "man",
        "woman",
        "non-binary",
        "nonbinary",
        "he/him",
        "she/her",
        "they/them",
        "mr.",
        "mrs.",
        "ms.",
        "miss",
    ),
    "MARITAL": ("married", "unmarried", "single", "divorced", "widowed", "spouse"),
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
    "DISABILITY": ("disability", "disabled", "wheelchair", "neurodivergent", "adhd", "autistic"),
    "AGE": ("years old", "age:", "d.o.b", "date of birth"),
    "PHOTO": ("photograph attached", "photo attached", "passport photo"),
}

PROTECTED_PATTERNS: Final[tuple[Pattern, ...]] = tuple(
    Pattern(
        entity,
        re.compile(r"\b(?:" + "|".join(re.escape(t) for t in terms) + r")\b", re.I),
    )
    for entity, terms in PROTECTED_TERMS.items()
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
