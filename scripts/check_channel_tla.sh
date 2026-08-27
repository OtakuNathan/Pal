#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tla_jar="${1:-${TLA2TOOLS_JAR:-}}"
workers="${TLC_WORKERS:-1}"

if [[ -z "${tla_jar}" || ! -f "${tla_jar}" ]]; then
    echo "usage: $0 /path/to/tla2tools.jar" >&2
    echo "or set TLA2TOOLS_JAR to a pinned TLC jar" >&2
    exit 2
fi

cd "${repo_root}"
python - <<'PY'
from pathlib import Path
from pal.channel.formal import render_endpoint_hub_implementation_relation

path = Path("spec/channel/EndpointHubImplementationReducer.tla")
if path.read_text(encoding="utf-8") != render_endpoint_hub_implementation_relation():
    raise SystemExit("generated endpoint hub reducer relation is stale")
PY

echo "==> TLC EndpointHubLifecycle"
java -XX:+UseParallelGC -jar "${tla_jar}" \
    -workers "${workers}" \
    -cleanup \
    -config spec/channel/EndpointHubLifecycle.cfg \
    spec/channel/EndpointHubLifecycle.tla
