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
    ImportedPlanLifecycle
    GraphGenerationLifecycle
    GraphExecutionLifecycle
    DependencyRepairCohort
    DependencyRepairSetupOwnership
    DependencyRepairPartialOverlap
    DependencyRepairObligationCarry
    DependencyRepairLaterCohort
    DependencyRepairControlOverlay
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
    OperatorTriageRecovery
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

# Additional bounded peer-report classifications and the two-scope overlap.
for entry in \
    DependencyRepairCohort:DependencyRepairCohortPass \
    DependencyRepairCohort:DependencyRepairCohortInvalidScope \
    DependencyRepairCohort:DependencyRepairCohortContractOnly \
    DependencyRepairPartialOverlap:DependencyRepairPartialOverlapTwo \
    DependencyRepairControlOverlay:DependencyRepairControlOverlayPass \
    DependencyRepairControlOverlay:DependencyRepairControlOverlayLocal \
    DependencyRepairControlOverlay:DependencyRepairControlOverlayCorrection \
    DependencyRepairControlOverlay:DependencyRepairControlOverlayTerminalSource \
    DependencyRepairControlOverlay:DependencyRepairControlOverlayTerminalPeer \
    DependencyRepairControlOverlay:DependencyRepairControlOverlayChangedCandidate \
    DependencyRepairControlOverlay:DependencyRepairControlOverlayChangedFinding; do
    model="${entry%%:*}"
    config="${entry#*:}"
    echo "==> TLC ${config}"
    java -XX:+UseParallelGC -jar "${tla_jar}" \
        -workers "${workers}" \
        -cleanup \
        -config "spec/bunshin_v2/${config}.cfg" \
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

# UNKNOWN recovery must not silently restore an equivalent older receipt.
echo "==> TLC OperatorTriageRecoveryUnsafe (expected counterexample)"
set +e
unknown_unsafe_output="$({
    java -XX:+UseParallelGC -jar "${tla_jar}" \
        -workers "${workers}" -cleanup \
        -config spec/bunshin_v2/OperatorTriageRecoveryUnsafe.cfg \
        spec/bunshin_v2/OperatorTriageRecovery.tla
} 2>&1)"
unknown_unsafe_status=$?
set -e
if [[ ${unknown_unsafe_status} -eq 0 ]] || [[ "${unknown_unsafe_output}" != *"Invariant NoOldUnknownReplay is violated"* ]]; then
    echo "OperatorTriageRecoveryUnsafe did not reproduce NoOldUnknownReplay" >&2
    echo "${unknown_unsafe_output}" >&2
    exit 1
fi
