#!/usr/bin/env bash
# AC-15: `docker compose up` -> all services healthy in under 180 seconds.
set -euo pipefail
LIMIT=180
cd "$(dirname "$0")/.."

echo "Tearing down for a genuine cold start..."
docker compose down -v >/dev/null 2>&1 || true

START=$(date +%s)
docker compose up -d --build >/dev/null

while :; do
  ELAPSED=$(( $(date +%s) - START ))
  if [ "$ELAPSED" -gt "$LIMIT" ]; then
    echo "FAIL: not healthy within ${LIMIT}s"
    docker compose ps
    exit 1
  fi
  DB=$(docker compose ps --format '{{.Health}}' db 2>/dev/null || echo "")
  API=$(docker compose ps --format '{{.Health}}' api 2>/dev/null || echo "")
  if [ "$DB" = "healthy" ] && [ "$API" = "healthy" ]; then
    echo "PASS: all services healthy in ${ELAPSED}s (budget ${LIMIT}s)"
    break
  fi
  sleep 2
done

echo "Applying migrations..."
docker compose exec -T api alembic upgrade head

# Assert on the VALUE, not the presence of the key. Grepping for '"pgvector"'
# passed while the extension was actually missing — the exact weak-assertion
# trap rule D exists to prevent.
echo "Checking readiness..."
READY=$(curl -sf http://localhost:8000/readyz)
echo "$READY"
python3 - "$READY" <<'PYCHECK'
import json, sys
r = json.loads(sys.argv[1])
problems = []
if r.get("status") != "ok":
    problems.append(f"status is {r.get('status')!r}, expected 'ok'")
if r.get("database") != "ok":
    problems.append(f"database is {r.get('database')!r}")
pgv = r.get("pgvector", "missing")
if pgv in ("missing", None):
    problems.append("pgvector extension is not installed")
if problems:
    print("FAIL: " + "; ".join(problems))
    sys.exit(1)
print(f"pgvector {pgv} confirmed")
PYCHECK

echo "AC-15 PASS"
