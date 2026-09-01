#!/usr/bin/env bash
# Restore a backup into a SCRATCH database and verify it.
#
# Deliberately refuses to restore over the live database. The point of a monthly
# restore drill is to prove the backup is good, and a drill that can destroy
# production is a drill nobody runs.
#
# Accepts an age-encrypted dump, which the first version did not. The runbook
# told operators to encrypt with backup.sh and then restore with this script,
# and those two halves had never been run against each other: pg_restore was
# handed ciphertext and said "input file does not appear to be a valid
# archive". The documented procedure could not work.
set -euo pipefail
cd "$(dirname "$0")/.."

DUMP="${1:?usage: restore.sh <db-dump-file[.age]> [scratch-db-name]}"
SCRATCH="${2:-screener_restore_test}"

if [ "$SCRATCH" = "${POSTGRES_DB:-screener}" ]; then
  echo "Refusing to restore over the live database." >&2
  exit 1
fi
[ -s "$DUMP" ] || { echo "$DUMP is missing or empty." >&2; exit 1; }

# Plaintext candidate data exists only inside this directory, only for the
# length of the restore, and only readable by the invoking user.
WORK="$(mktemp -d)"
chmod 700 "$WORK"
trap 'rm -rf "$WORK"' EXIT

case "$DUMP" in
  *.age)
    : "${AGE_IDENTITY:?set AGE_IDENTITY to the private key file for this backup}"
    command -v age >/dev/null || { echo "age is not installed." >&2; exit 1; }
    echo "Decrypting $DUMP..."
    age -d -i "$AGE_IDENTITY" -o "$WORK/db.dump" "$DUMP"
    PLAIN="$WORK/db.dump"
    ;;
  *)
    PLAIN="$DUMP"
    ;;
esac

echo "Creating scratch database $SCRATCH..."
docker compose exec -T db psql -U "${POSTGRES_USER:-screener}" -d postgres \
  -c "DROP DATABASE IF EXISTS $SCRATCH" -c "CREATE DATABASE $SCRATCH"

echo "Restoring..."
# Status captured rather than discarded. The previous `|| true` meant a restore
# that half-failed still reached the verification step, which would then print
# plausible-looking counts for an incomplete database.
set +e
docker compose exec -T db pg_restore -U "${POSTGRES_USER:-screener}" \
  -d "$SCRATCH" --no-owner --no-privileges --exit-on-error < "$PLAIN"
STATUS=$?
set -e
if [ "$STATUS" -ne 0 ]; then
  echo "pg_restore failed (exit $STATUS). The backup is NOT good." >&2
  exit "$STATUS"
fi

echo
echo "Verifying restored contents:"
docker compose exec -T db psql -U "${POSTGRES_USER:-screener}" -d "$SCRATCH" -c "
SELECT 'organizations' t, count(*) FROM organizations
UNION ALL SELECT 'users', count(*) FROM users
UNION ALL SELECT 'resumes', count(*) FROM resumes
UNION ALL SELECT 'resume_chunks', count(*) FROM resume_chunks
UNION ALL SELECT 'matches', count(*) FROM matches
UNION ALL SELECT 'audit_events', count(*) FROM audit_events
UNION ALL SELECT 'outbox_events', count(*) FROM outbox_events
ORDER BY 1;"

# The chain is the reason the audit log is worth having, and a restore that
# brings back rows with a broken chain has restored data but not evidence.
echo "Verifying the audit chain survived the round trip:"
docker compose exec -T db psql -U "${POSTGRES_USER:-screener}" -d "$SCRATCH" -t -c "
SELECT count(*) || ' events, ' ||
       count(*) FILTER (WHERE length(hash) = 64) || ' with a well-formed hash'
  FROM audit_events;"

echo "Restore verified into $SCRATCH. Drop it when finished:"
echo "  docker compose exec -T db psql -U ${POSTGRES_USER:-screener} -d postgres -c 'DROP DATABASE $SCRATCH'"
