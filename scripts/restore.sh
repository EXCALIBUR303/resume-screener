#!/usr/bin/env bash
# Restore a backup into a SCRATCH database and verify it.
#
# Deliberately refuses to restore over the live database. The point of a monthly
# restore drill is to prove the backup is good, and a drill that can destroy
# production is a drill nobody runs.
set -euo pipefail
cd "$(dirname "$0")/.."

DUMP="${1:?usage: restore.sh <db-dump-file> [scratch-db-name]}"
SCRATCH="${2:-screener_restore_test}"

if [ "$SCRATCH" = "${POSTGRES_DB:-screener}" ]; then
  echo "Refusing to restore over the live database." >&2
  exit 1
fi

echo "Creating scratch database $SCRATCH..."
docker compose exec -T db psql -U "${POSTGRES_USER:-screener}" -d postgres \
  -c "DROP DATABASE IF EXISTS $SCRATCH" -c "CREATE DATABASE $SCRATCH"

echo "Restoring..."
docker compose exec -T db pg_restore -U "${POSTGRES_USER:-screener}" \
  -d "$SCRATCH" --no-owner --no-privileges < "$DUMP" 2>&1 | tail -5 || true

echo
echo "Verifying restored contents:"
docker compose exec -T db psql -U "${POSTGRES_USER:-screener}" -d "$SCRATCH" -c "
SELECT 'organizations' t, count(*) FROM organizations
UNION ALL SELECT 'users', count(*) FROM users
UNION ALL SELECT 'resumes', count(*) FROM resumes
UNION ALL SELECT 'resume_chunks', count(*) FROM resume_chunks
UNION ALL SELECT 'matches', count(*) FROM matches
UNION ALL SELECT 'audit_events', count(*) FROM audit_events
ORDER BY 1;"

echo "Restore verified into $SCRATCH. Drop it when finished:"
echo "  docker compose exec -T db psql -U ${POSTGRES_USER:-screener} -d postgres -c 'DROP DATABASE $SCRATCH'"
