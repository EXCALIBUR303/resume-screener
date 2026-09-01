"""Prometheus metrics.

Chosen to answer the questions an operator actually has at 3am: is work piling
up, is anything dead-lettered, is redaction still firing, and is the model
responding. Vanity counters are omitted.

**Cardinality discipline.** No label ever carries a resume id, candidate id, or
organisation id. High-cardinality labels are how a metrics backend falls over,
and an org id in a label is also a slow information leak about who the tenants
are.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry()

# ---- ingestion ---------------------------------------------------------------

uploads_total = Counter(
    "screener_uploads_total",
    "Resume uploads by outcome.",
    ["outcome"],  # accepted | rejected | duplicate
    registry=REGISTRY,
)
upload_rejections_total = Counter(
    "screener_upload_rejections_total",
    "Rejected uploads by reason. A spike in one reason is usually an attack or a "
    "broken client, and the two look different.",
    ["reason"],
    registry=REGISTRY,
)

# ---- pipeline ----------------------------------------------------------------

parse_duration_seconds = Histogram(
    "screener_parse_duration_seconds",
    "Time to extract text from one document.",
    ["extractor"],  # pypdf | python-docx | tesseract
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60),
    registry=REGISTRY,
)
redaction_entities_total = Counter(
    "screener_redaction_entities_total",
    "PII entities removed, by type. A sudden drop to zero means redaction has "
    "silently stopped working — the most dangerous failure this system has.",
    ["entity"],
    registry=REGISTRY,
)
embed_duration_seconds = Histogram(
    "screener_embed_duration_seconds",
    "Time to embed one document's chunks.",
    buckets=(0.05, 0.1, 0.5, 1, 2, 5, 15),
    registry=REGISTRY,
)

# ---- model -------------------------------------------------------------------

llm_requests_total = Counter(
    "screener_llm_requests_total",
    "Model calls by provider and outcome.",
    ["provider", "outcome"],  # ok | schema_invalid | timeout | unavailable
    registry=REGISTRY,
)
llm_duration_seconds = Histogram(
    "screener_llm_duration_seconds",
    "Model latency.",
    ["provider"],
    buckets=(0.5, 1, 2, 5, 10, 20, 40, 60, 120),
    registry=REGISTRY,
)
llm_tokens_total = Counter(
    "screener_llm_tokens_total",
    "Tokens consumed. The number that becomes a bill the moment a paid provider "
    "is configured, which is why it is tracked even when the model is free.",
    ["provider", "kind"],  # prompt | completion
    registry=REGISTRY,
)

# ---- scoring integrity -------------------------------------------------------

scores_total = Counter(
    "screener_scores_total",
    "Completed scores by integrity flag.",
    ["flag"],  # clean | partially_supported | injection_suspected | degraded | stuffed
    registry=REGISTRY,
)
evidence_zeroed_total = Counter(
    "screener_evidence_zeroed_total",
    "Competencies zeroed because their cited evidence could not be verified. "
    "A rise means the model is fabricating more, or a document is attacking us.",
    registry=REGISTRY,
)

# ---- queue (sampled at scrape time; see collect_queue_depths) -----------------

queue_depth = Gauge(
    "screener_queue_depth",
    "Jobs waiting, by type and status.",
    ["job_type", "status"],
    registry=REGISTRY,
)
dead_letter_depth = Gauge(
    "screener_dead_letter_depth",
    "Dead-lettered jobs. Alert on > 0: it means work was abandoned.",
    registry=REGISTRY,
)
oldest_pending_seconds = Gauge(
    "screener_oldest_pending_job_seconds",
    "Age of the oldest pending job. Rising while depth is flat means workers are "
    "stuck rather than merely busy — a different problem with a different fix.",
    registry=REGISTRY,
)
