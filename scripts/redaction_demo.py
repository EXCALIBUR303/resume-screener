"""Print a resume beside what the model actually receives.

This is the README's headline image. Run: make redact-demo
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api", "src"))

from screener_api.privacy.redact import redact  # noqa: E402

RESUME = """Priya Ramanathan
priya.ramanathan@example.com | +91 98765 44321 | linkedin.com/in/priya-r
Bengaluru, India | Female, married | D.O.B: 12/04/1997 | PAN ABCDE1234F

SUMMARY
Backend engineer with seven years building payment and ledger systems.

WORK EXPERIENCE
Senior Backend Engineer, Invented Systems Ltd (2021-2026)
Priya designed payment services in Python on PostgreSQL at 12k requests/second.
Ramanathan led the migration from a monolith to six services using Docker and Redis.

EDUCATION
B.Tech Computer Science, Imaginary Institute of Technology, 2019

TECHNICAL SKILLS
Python, PostgreSQL, Redis, Docker, Kubernetes, REST APIs, pytest

SYNTHETIC-DATA-DO-NOT-USE
"""

result = redact(RESUME, header=RESUME.split("SUMMARY")[0])

print("=" * 78)
print("  UPLOADED BY THE RECRUITER")
print("=" * 78)
for line in RESUME.strip().split("\n"):
    print("  " + line)

print()
print("=" * 78)
print("  WHAT THE MODEL ACTUALLY RECEIVES")
print("=" * 78)
for line in result.text.strip().split("\n"):
    print("  " + line)

print()
print(f"  {result.entity_count} entities removed: {result.counts}")
print("  Skills, employment dates and achievements survive intact — a redactor")
print("  that eats the signal breaks the scoring silently (ADR-0009).")
