"""Generate the golden corpus: synthetic resumes with construction-derived labels.

Every document is invented. Names come from a fictional list, employers are
made up, and each file carries SYNTHETIC-DATA-DO-NOT-USE. No real person's
resume is used, ever — see the policy in `evals/README.md`.

Determinism matters: the corpus is regenerated from a fixed seed, so a metric
change always means a code change and never a data change.
"""

from __future__ import annotations

import json
import pathlib
import random
from dataclasses import asdict, dataclass, field

SEED = 20260831
MARKER = "SYNTHETIC-DATA-DO-NOT-USE"
OUT = pathlib.Path(__file__).resolve().parents[1] / "evals" / "golden"

# --------------------------------------------------------------------------- #
#  Job descriptions
# --------------------------------------------------------------------------- #

JOBS: list[dict[str, object]] = [
    {"id": "jd_backend_senior", "title": "Senior Backend Engineer",
     "required": ["Python", "PostgreSQL", "Kubernetes"], "nice": ["Redis", "Docker"],
     "min_years": 5,
     "description": "Senior backend engineer to build and operate payment services. "
                    "Python on PostgreSQL, deployed on Kubernetes, owning reliability."},
    {"id": "jd_backend_junior", "title": "Backend Engineer",
     "required": ["Python", "PostgreSQL"], "nice": ["Docker"], "min_years": 2,
     "description": "Backend engineer to build APIs in Python against PostgreSQL. "
                    "You will work on internal services and reporting tools."},
    {"id": "jd_data_engineer", "title": "Data Engineer",
     "required": ["Python", "Spark", "Airflow"], "nice": ["Snowflake"], "min_years": 4,
     "description": "Data engineer to build and own ETL pipelines with Airflow and "
                    "Spark, loading a warehouse used across the business."},
    {"id": "jd_ml_engineer", "title": "Machine Learning Engineer",
     "required": ["Python", "PyTorch", "Machine Learning"], "nice": ["Kubernetes"],
     "min_years": 4,
     "description": "Machine learning engineer to train and serve ranking models in "
                    "PyTorch, working on retrieval and embeddings."},
    {"id": "jd_frontend", "title": "Frontend Engineer",
     "required": ["React", "TypeScript"], "nice": ["Figma"], "min_years": 3,
     "description": "Frontend engineer to build accessible interfaces in React and "
                    "TypeScript, owning a component library."},
    {"id": "jd_platform", "title": "Platform Engineer",
     "required": ["Kubernetes", "Terraform", "Docker"], "nice": ["Prometheus"],
     "min_years": 4,
     "description": "Platform engineer to run Kubernetes, manage infrastructure as "
                    "code in Terraform, and own CI/CD."},
    {"id": "jd_sre", "title": "Site Reliability Engineer",
     "required": ["Kubernetes", "Prometheus", "Linux"], "nice": ["Terraform"],
     "min_years": 5,
     "description": "SRE to own observability with Prometheus and Grafana, run "
                    "incident response, and manage error budgets on Kubernetes."},
    {"id": "jd_security", "title": "Application Security Engineer",
     "required": ["Threat Modelling", "SAST", "Python"], "nice": ["Kubernetes"],
     "min_years": 4,
     "description": "Application security engineer to run threat modelling, own SAST "
                    "and DAST pipelines, and review services against OWASP ASVS."},
]

# --------------------------------------------------------------------------- #
#  Candidate archetypes
# --------------------------------------------------------------------------- #

FIRST = ["Priya", "Zoe", "James", "Wei", "Aisha", "Diego", "Lars", "Fatima", "Kenji",
         "Olga", "Sam", "Noor", "Mateo", "Ingrid", "Tariq", "Yuki", "Elena", "Kwame"]
LAST = ["Placeholder", "Fictional", "Invented", "Madeup", "Example", "Notreal",
        "Synthetic", "Imaginary", "Fabricated", "Sample"]
COMPANIES = ["Invented Systems", "Nowhere Ltd", "Fictional Labs", "Example Works",
             "Imaginary Group", "Placeholder Inc", "Notreal Technologies"]

SKILL_DETAIL: dict[str, str] = {
    "Python": "Built and maintained services in Python, including async APIs and workers.",
    "PostgreSQL": "Designed PostgreSQL schemas and tuned queries under production load.",
    "Kubernetes": "Ran workloads on Kubernetes, owning rollouts, probes and autoscaling.",
    "Redis": "Used Redis for caching and rate limiting in high-traffic paths.",
    "Docker": "Containerised services with Docker and maintained base images.",
    "Spark": "Wrote Spark jobs processing several terabytes per day.",
    "Airflow": "Authored and operated Airflow DAGs for nightly pipelines.",
    "Snowflake": "Modelled warehouse tables in Snowflake for analytics consumers.",
    "PyTorch": "Trained ranking models in PyTorch and shipped them to production.",
    "Machine Learning": "Owned feature engineering, training and offline evaluation.",
    "React": "Built accessible React components used across several products.",
    "TypeScript": "Migrated a large codebase to TypeScript and enforced strict mode.",
    "Figma": "Worked from Figma designs and maintained shared design tokens.",
    "Terraform": "Managed infrastructure as code in Terraform across environments.",
    "Prometheus": "Instrumented services with Prometheus metrics and wrote alerts.",
    "Linux": "Debugged Linux performance issues down to syscall level.",
    "Threat Modelling": "Ran threat modelling sessions using a STRIDE-style method.",
    "SAST": "Owned SAST and dependency scanning in CI, triaging findings.",
}


@dataclass
class Profile:
    resume_id: str
    name: str
    skills: list[str]
    years: int
    seniority: str
    archetype: str
    # Where the evidence sits. The career-changer case puts real experience in a
    # projects section, which is exactly what a naive section-weighted scorer
    # gets wrong.
    skills_in_projects: bool = False
    stuffed: list[str] = field(default_factory=list)


def build_profiles(rng: random.Random) -> list[Profile]:
    profiles: list[Profile] = []
    index = 0

    def add(**kwargs: object) -> None:
        nonlocal index
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        profiles.append(Profile(resume_id=f"syn_{index:04d}", name=name, **kwargs))  # type: ignore[arg-type]
        index += 1

    # Strong fits: one or two per job.
    for job in JOBS:
        for _ in range(2):
            add(skills=list(job["required"]) + list(job["nice"])[:1],  # type: ignore[arg-type]
                years=int(job["min_years"]) + rng.randint(1, 4),  # type: ignore[arg-type]
                seniority="senior", archetype="strong_fit")

    # Near misses: right skills, not enough years. The case a keyword-only
    # system cannot distinguish from a strong fit.
    for job in JOBS[:6]:
        add(skills=list(job["required"]),  # type: ignore[arg-type]
            years=max(1, int(job["min_years"]) - 3),  # type: ignore[arg-type]
            seniority="junior", archetype="near_miss_experience")

    # Partial skills: most but not all requirements.
    for job in JOBS[:6]:
        required = list(job["required"])  # type: ignore[arg-type]
        add(skills=required[:-1], years=int(job["min_years"]) + 1,  # type: ignore[arg-type]
            seniority="mid", archetype="partial_skills")

    # Career changers: the right skills, but only in a projects section.
    for job in JOBS[:5]:
        add(skills=list(job["required"]), years=2,  # type: ignore[arg-type]
            seniority="junior", archetype="career_changer", skills_in_projects=True)

    # Keyword-stuffed decoys: they NAME the skills without any supporting detail.
    for job in JOBS[:5]:
        add(skills=[], years=1, seniority="junior", archetype="keyword_stuffed",
            stuffed=list(job["required"]))  # type: ignore[arg-type]

    # Overqualified: far more experience than asked for. The experience term is
    # meant to flatten at the requirement, not keep rewarding — twice the
    # required years is not twice as good.
    for job in JOBS[:3]:
        add(skills=list(job["required"]) + list(job["nice"]),  # type: ignore[arg-type]
            years=int(job["min_years"]) + 12,  # type: ignore[arg-type]
            seniority="principal", archetype="overqualified")

    # Unrelated professionals, to give retrieval something to reject.
    others = [["React", "TypeScript", "Figma"], ["Spark", "Airflow", "Snowflake"],
              ["Threat Modelling", "SAST"], ["PyTorch", "Machine Learning"],
              ["Terraform", "Prometheus", "Linux"], ["Python", "Docker"],
              ["React", "Figma"], ["Snowflake", "Airflow"], ["Linux", "Docker"]]
    for skills in others:
        add(skills=skills, years=rng.randint(2, 8), seniority="mid",
            archetype="unrelated")

    return profiles


def render(profile: Profile) -> str:
    lines = [
        profile.name,
        f"{profile.name.split()[0].lower()}@example.com | +91 90000 00000",
        "",
        "SUMMARY",
        f"{profile.seniority.title()} engineer with {profile.years} years of experience.",
        "",
    ]

    detail = [SKILL_DETAIL[s] for s in profile.skills if s in SKILL_DETAIL]
    company = COMPANIES[hash(profile.resume_id) % len(COMPANIES)]
    start = 2026 - profile.years

    if profile.skills_in_projects:
        lines += ["WORK EXPERIENCE",
                  f"Support Analyst, {company} ({start}-2026)",
                  "Handled tickets and reporting for internal teams.", "",
                  "PROJECTS"] + detail + [""]
    else:
        lines += ["WORK EXPERIENCE",
                  f"{profile.seniority.title()} Engineer, {company} ({start}-2026)"] \
                 + detail + [""]

    lines += ["EDUCATION", "B.Tech Computer Science, Imaginary Institute, 2018", ""]

    named = profile.skills or profile.stuffed
    lines += ["TECHNICAL SKILLS", ", ".join(named) if named else "General IT", "", MARKER]
    return "\n".join(lines)


def grade(profile: Profile, job: dict[str, object]) -> int:
    """Relevance derived from construction, never from reading the text back."""
    required = set(job["required"])  # type: ignore[arg-type]
    # A stuffed resume NAMES skills it cannot evidence. It is not a match, and
    # the whole point of the corpus is that the system must agree.
    have = set(profile.skills)
    overlap = len(have & required)

    if overlap == 0:
        return 0
    if overlap < len(required):
        return 1 if overlap == 1 and len(required) > 2 else 1
    # All required skills present; years decides 3 vs 2.
    if profile.years >= int(job["min_years"]):  # type: ignore[arg-type]
        return 3
    return 2


def main() -> None:
    rng = random.Random(SEED)
    profiles = build_profiles(rng)

    corpus = {
        "version": "v1",
        "seed": SEED,
        "generated_by": "scripts/gen_synthetic.py",
        "labels_are": "construction-derived, not human judgments (see evals/README.md)",
        "jobs": JOBS,
        "resumes": [
            {**asdict(p), "text": render(p)} for p in profiles
        ],
    }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "corpus.json").write_text(json.dumps(corpus, indent=2) + "\n")

    with (OUT / "labels.jsonl").open("w") as handle:
        for job in JOBS:
            for profile in profiles:
                handle.write(json.dumps({
                    "job_id": job["id"], "resume_id": profile.resume_id,
                    "grade": grade(profile, job), "archetype": profile.archetype,
                }) + "\n")

    counts: dict[str, int] = {}
    for profile in profiles:
        counts[profile.archetype] = counts.get(profile.archetype, 0) + 1
    print(f"  {len(profiles)} resumes x {len(JOBS)} jobs = "
          f"{len(profiles) * len(JOBS)} labelled pairs")
    for archetype, n in sorted(counts.items()):
        print(f"    {archetype:<26} {n}")


if __name__ == "__main__":
    main()
