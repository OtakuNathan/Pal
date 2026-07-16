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

models=(
    ModuleLifecycle
    DagLifecycle
    ArchitectureLifecycle
    StandaloneReviewLifecycle
    OrchestrationLifecycle
    DurableEffects
    ImplementationTopology
)

cd "${repo_root}"
for model in "${models[@]}"; do
    echo "==> TLC ${model}"
    java -XX:+UseParallelGC -jar "${tla_jar}" \
        -workers "${workers}" \
        -cleanup \
        -config "spec/minion_v2/${model}.cfg" \
        "spec/minion_v2/${model}.tla"
done
