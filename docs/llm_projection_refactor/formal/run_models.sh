#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
: "${TLA2TOOLS_JAR:?Set TLA2TOOLS_JAR to a trusted local tla2tools.jar}"
if [[ ! -f "$TLA2TOOLS_JAR" ]]; then
  printf 'TLA2TOOLS_JAR not found: %s\n' "$TLA2TOOLS_JAR" >&2
  exit 2
fi
JAR=$(cd "$(dirname "$TLA2TOOLS_JAR")" && pwd)/$(basename "$TLA2TOOLS_JAR")
LOGS=${MODEL_LOG_DIR:-../evidence/tlc}
mkdir -p "$LOGS"
java -version 2>"$LOGS/java-version.txt"
python - "$JAR" >"$LOGS/jar-sha256.txt" <<'PY'
import hashlib,sys
with open(sys.argv[1], 'rb') as f:
    print(hashlib.file_digest(f, 'sha256').hexdigest())
PY
java -cp "$JAR" tla2sany.SANY EndpointProjection.tla >"$LOGS/sany.log" 2>&1
for config in single isolation; do
  metadata=$(mktemp -d)
  java -XX:+UseParallelGC -Xmx2g -cp "$JAR" tlc2.TLC \
    -workers 1 -metadir "$metadata" -config "$config.cfg" EndpointProjection.tla \
    >"$LOGS/$config.log" 2>&1
  grep -q 'Model checking completed. No error has been found.' "$LOGS/$config.log"
  rm -rf "$metadata"
done
metadata=$(mktemp -d)
set +e
java -XX:+UseParallelGC -Xmx2g -cp "$JAR" tlc2.TLC \
  -workers 1 -metadir "$metadata" -config stale_replay_mutant.cfg EndpointProjection.tla \
  >"$LOGS/stale_replay_mutant.log" 2>&1
status=$?
set -e
rm -rf "$metadata"
if [[ $status -eq 0 ]] || ! grep -q 'Invariant DraftAligned is violated' "$LOGS/stale_replay_mutant.log"; then
  echo 'Mutant did not produce the expected DraftAligned failure. Inspect logs.' >&2
  exit 1
fi
echo "TLC positive configs passed; stale-replay mutant rejected. Logs: $LOGS"
