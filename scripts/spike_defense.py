"""Does the architecture stop the injection the model fell for?
Applies per-competency evidence gating + the deterministic half, then fuses."""
import json, re, time, urllib.request
from spike_structured import ask, CLEAN, DIRTY, JD          # reuse the spike

REQUIRED = {"python", "postgresql", "kubernetes"}
ALIASES  = {"postgres": "postgresql", "psql": "postgresql",
            "k8s": "kubernetes", "py": "python"}
W = {"skill": 0.30, "exp": 0.20, "sem": 0.20, "rubric": 0.30}


def norm(t):
    return " ".join(t.split()).lower()


def skills_present(text):
    """Deterministic: token-match required skills against the SOURCE text only."""
    toks = set(re.findall(r"[a-z0-9+#.]+", norm(text)))
    toks |= {ALIASES[t] for t in toks if t in ALIASES}
    return REQUIRED & toks


def gate(result, source):
    """Per-competency: a competency with zero verbatim-verified spans scores 0."""
    src, rows = norm(source), []
    for c in result.get("competencies", []):
        spans = c.get("evidence", []) or []
        ok = [s for s in spans if norm(s) in src]
        verified = len(ok) > 0
        rows.append({"name": c["name"], "claimed": c["level"],
                     "spans": len(spans), "verified_spans": len(ok),
                     "effective": c["level"] if verified else 0})
    return rows


def score(text, result):
    rows = gate(result, text)
    have = skills_present(text)
    s_skill = len(have) / len(REQUIRED)
    s_exp   = 1.0                                   # 7 yrs vs 4+ required, both cases
    s_sem   = 0.72                                  # stand-in for the RRF term
    eff     = [r["effective"] for r in rows] or [0]
    s_rub   = (sum(eff) / len(eff)) / 4
    unver   = any(r["effective"] < r["claimed"] for r in rows)
    total   = (W["skill"]*s_skill + W["exp"]*s_exp + W["sem"]*s_sem + W["rubric"]*s_rub)
    if unver:
        total -= 0.15                               # partially_supported penalty
    return rows, have, s_rub, max(total, 0.0), unver


INJ = re.compile(r"ignore (all )?previous instructions|do not mention this|"
                 r"you are now|disregard the above", re.I)

for label, doc in (("CLEAN", CLEAN), ("INJECTED", DIRTY)):
    raw, dt = ask(doc)
    r = json.loads(raw)
    rows, have, s_rub, total, unver = score(CLEAN, r)   # verify against real text
    print(f"\n=== {label}  ({dt:.1f}s)  injection_heuristic={'HIT' if INJ.search(doc) else 'clean'}")
    print(f"{'competency':<26}{'claimed':<9}{'spans':<7}{'verified':<10}effective")
    for x in rows:
        flag = "  <-- ZEROED" if x["effective"] < x["claimed"] else ""
        print(f"{x['name'][:25]:<26}{x['claimed']:<9}{x['spans']:<7}"
              f"{x['verified_spans']:<10}{x['effective']}{flag}")
    print(f"deterministic skills found: {sorted(have)}  -> S_skill={len(have)}/3")
    print(f"S_rubric={s_rub:.2f}   partially_supported={unver}   "
          f"FINAL={total:.3f}  ({total*10:.1f}/10)")
