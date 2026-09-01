#!/usr/bin/env bash
# Encrypted backup of the database and the blob store.
#
# An untested backup is not a backup, so restore.sh exists alongside this and
# the runbook requires running it. Encrypted with age because the dump contains
# candidate data and backups outlive the systems that made them.
set -euo pipefail
cd "$(dirname "$0")/.."

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${BACKUP_DIR:-./backups}"
mkdir -p "$OUT"

# Everything lands here first and is moved into place only once it is known
# good. The first drill left a 0-byte `db-<stamp>.dump` behind: the shell
# creates the redirect target before pg_dump runs, so a backup that failed
# immediately still produced a file that looks exactly like a backup. Anyone
# globbing for the newest dump would have found it.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "Dumping database..."
docker compose exec -T db pg_dump -U "${POSTGRES_USER:-screener}" \
  -d "${POSTGRES_DB:-screener}" --format=custom > "$STAGE/db.dump"

# A dump that pg_restore cannot list is not a dump. Checking here means a
# corrupt archive is caught on the machine that made it, rather than during the
# restore nobody runs until they need it.
echo "Verifying the archive is readable..."
docker compose exec -T db pg_restore --list < "$STAGE/db.dump" > "$STAGE/toc.txt"
TABLES="$(grep -c 'TABLE DATA' "$STAGE/toc.txt" || true)"
if [ "${TABLES:-0}" -lt 1 ]; then
  echo "ERROR: the dump contains no table data. Refusing to publish it." >&2
  exit 1
fi
echo "  $TABLES tables in the archive."

echo "Archiving blob store..."
docker compose run --rm --no-deps -T --entrypoint sh worker-parse \
  -c 'tar -cf - -C /data files' > "$STAGE/blobs.tar" 2>/dev/null \
  || docker run --rm -v resume-screener_blobs:/data:ro alpine \
       tar -cf - -C /data . > "$STAGE/blobs.tar"

if command -v age >/dev/null && [ -n "${AGE_RECIPIENT:-}" ]; then
  echo "Encrypting with age..."
  age -r "$AGE_RECIPIENT" -o "$OUT/db-$STAMP.dump.age" "$STAGE/db.dump"
  age -r "$AGE_RECIPIENT" -o "$OUT/blobs-$STAMP.tar.age" "$STAGE/blobs.tar"
else
  # Refuse to pretend. An unencrypted backup of candidate data is a liability,
  # and silently producing one while the runbook claims encryption is worse.
  echo "WARNING: age not configured (set AGE_RECIPIENT). Backup is UNENCRYPTED." >&2
  mv "$STAGE/db.dump" "$OUT/db-$STAMP.dump"
  mv "$STAGE/blobs.tar" "$OUT/blobs-$STAMP.tar"
fi

echo "Backup complete: $OUT (stamp $STAMP)"
ls -la "$OUT" | tail -4
