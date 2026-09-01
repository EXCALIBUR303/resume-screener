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

echo "Dumping database..."
docker compose exec -T db pg_dump -U "${POSTGRES_USER:-screener}" \
  -d "${POSTGRES_DB:-screener}" --format=custom > "$OUT/db-$STAMP.dump"

echo "Archiving blob store..."
docker compose run --rm --no-deps -T -v "$PWD/$OUT:/backup" \
  --entrypoint sh worker-parse -c 'tar -cf - -C /data files' > "$OUT/blobs-$STAMP.tar" \
  2>/dev/null || docker run --rm -v resume-screener_blobs:/data:ro -v "$PWD/$OUT:/backup" \
  alpine tar -cf "/backup/blobs-$STAMP.tar" -C /data .

if command -v age >/dev/null && [ -n "${AGE_RECIPIENT:-}" ]; then
  echo "Encrypting with age..."
  for f in "$OUT/db-$STAMP.dump" "$OUT/blobs-$STAMP.tar"; do
    age -r "$AGE_RECIPIENT" -o "$f.age" "$f" && rm "$f"
  done
else
  # Refuse to pretend. An unencrypted backup of candidate data is a liability,
  # and silently producing one while the runbook claims encryption is worse.
  echo "WARNING: age not configured (set AGE_RECIPIENT). Backup is UNENCRYPTED." >&2
fi

echo "Backup complete: $OUT (stamp $STAMP)"
ls -la "$OUT" | tail -4
