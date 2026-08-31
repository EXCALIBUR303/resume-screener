"""Render a ranked candidate list from /jobs/{id}/matches JSON on stdin."""
import json
import sys

rows = json.load(sys.stdin)
print("\n  RANKED CANDIDATES")
print("  " + "=" * 76)
for i, m in enumerate(rows, 1):
    flags = []
    if m["injection_suspected"]:
        flags.append("INJECTION SUSPECTED")
    if m["partially_supported"]:
        flags.append("partially supported")
    if m["degraded"]:
        flags.append("degraded")
    banner = f"   [{', '.join(flags)}]" if flags else ""
    print(f"\n  {i}. {m['pseudonym']}   {m['score_out_of_ten']}/10{banner}")
    for c in m["contributions"]:
        print(f"       {c['term']:<11} {c['weight']:.2f} x {c['value']:.2f} "
              f"= {c['points']:+.3f}  [{c['computed_by']}]")
    for k, v in (m["penalties"] or {}).items():
        print(f"       penalty {k:<24} {-v:+.3f}")
    print(f"       matched: {', '.join(m['matched_skills']) or '-'}")
    print(f"       missing: {', '.join(m['missing_skills']) or '-'}")
    for comp in m["competencies"]:
        z = "  <-- ZEROED" if comp["zeroed"] else ""
        print(f"       {comp['name'][:20]:<22} claimed={comp['claimed_level']} "
              f"verified={comp['quotes_verified']}/{comp['quotes_cited']} "
              f"effective={comp['effective_level']}{z}")
