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
    ProduceCheckCycle
    GraphGenerationLifecycle
    GraphExecutionLifecycle
    ProcessCapacityLifecycle
    DagLifecycle
    ArchitectureLifecycle
    StandaloneReviewLifecycle
    OrchestrationLifecycle
    DurableEffects
    RoleAssignmentRecovery
    WorkerProcessLifecycle
    ContinuationLifecycle
    StartupRecoveryLifecycle
    ReplanReuseLifecycle
    ImplementationTopology
    ContractWorkItemLifecycle
    HarnessGenerationLifecycle
    BunshinRuntimeAuthority
    LogicalCoroutineSnapshotLifecycle
    ResidentMailboxLifecycle
    TaskDeliveryLifecycle
    ActiveLineageTriage
)

cd "${repo_root}"
for model in "${models[@]}"; do
    echo "==> TLC ${model}"
    java -XX:+UseParallelGC -jar "${tla_jar}" \
        -workers "${workers}" \
        -cleanup \
        -config "spec/bunshin_v2/${model}.cfg" \
        "spec/bunshin_v2/${model}.tla"
done

# A passing positive model alone must not make the no-silent-reset assertion
# vacuous: deliberately allow the retired fallback and require its counterexample.
echo "==> TLC StartupRecoveryLifecycleUnsafe (expected counterexample)"
set +e
unsafe_output="$({
    java -XX:+UseParallelGC -jar "${tla_jar}" \
        -workers "${workers}" -cleanup \
        -config spec/bunshin_v2/StartupRecoveryLifecycleUnsafe.cfg \
        spec/bunshin_v2/StartupRecoveryLifecycle.tla
} 2>&1)"
unsafe_status=$?
set -e
if [[ ${unsafe_status} -eq 0 ]] || [[ "${unsafe_output}" != *"Invariant FreshStartNeverForgetsInitialization is violated"* ]]; then
    echo "StartupRecoveryLifecycleUnsafe did not reproduce FreshStartNeverForgetsInitialization" >&2
    echo "${unsafe_output}" >&2
    exit 1
fi
