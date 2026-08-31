#!/usr/bin/env bash
# Anti-loophole rule D-12: a prompt's content must never change under a version
# that has already produced stored scores. Edit v2.md; never edit v1.md.
set -euo pipefail
fail=0
for f in "$@"; do
  # A prompt file that is modified (not added) is only allowed if it is the
  # highest version in its directory AND has never been committed before.
  if git rev-parse --verify "HEAD:$f" >/dev/null 2>&1; then
    echo "ERROR: $f is already committed. Prompts are immutable once used."
    echo "       Create the next version instead: $(dirname "$f")/v<N+1>.md"
    fail=1
  fi
done
exit $fail
