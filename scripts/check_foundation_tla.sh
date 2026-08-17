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
from pal.foundation.fd_lease_formal import render_fd_lease_implementation_topology

path = Path("spec/foundation/FdLeaseImplementationTopology.tla")
if path.read_text(encoding="utf-8") != render_fd_lease_implementation_topology():
    raise SystemExit("generated fd lease topology is stale")
PY

for model in FdLeaseLifecycle; do
    echo "==> TLC ${model}"
    java -XX:+UseParallelGC -jar "${tla_jar}" \
        -workers "${workers}" -cleanup \
        -config "spec/foundation/${model}.cfg" \
        "spec/foundation/${model}.tla"
done

for unsafe_case in \
    "FdLeaseLifecycleUnsafe.cfg:NoStaleUse" \
    "FdLeaseLifecycleUnsafePublish.cfg:PublishedOnlyAfterDetach"
do
    unsafe_cfg="${unsafe_case%%:*}"
    unsafe_invariant="${unsafe_case#*:}"
    echo "==> TLC ${unsafe_cfg} (expected ${unsafe_invariant} counterexample)"
    set +e
    unsafe_output="$({
        java -XX:+UseParallelGC -jar "${tla_jar}" \
            -workers "${workers}" -cleanup \
            -config "spec/foundation/${unsafe_cfg}" \
            spec/foundation/FdLeaseLifecycle.tla
    } 2>&1)"
    unsafe_status=$?
    set -e
    if [[ ${unsafe_status} -eq 0 ]] || [[ "${unsafe_output}" != *"Invariant ${unsafe_invariant} is violated"* ]]; then
        echo "${unsafe_cfg} did not reproduce ${unsafe_invariant}" >&2
        echo "${unsafe_output}" >&2
        exit 1
    fi
done
