from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.role_contracts import OrchestrationRole
from pal.bunshin.v2.role_contracts import RoleActivation, RoleMode
from pal.bunshin.v2.semantic_orchestration.role_inputs import _attach_bound_input_read_only_overlays
from pal.bunshin.v2.adapters import SOFTWARE_GIT_ADAPTER
from pal.bunshin.v2.task_ledger import TASK_LEDGER_ARTIFACT
from pal.bunshin.v2.role_protocol import canonical_role_profile_parts
from pal.bunshin.v2.semantic_orchestration.workflow_facts import WorkflowFacts
from pal.bunshin.v2.task_ledger import TaskLedgerService
from pal.bunshin.v2.semantic_orchestration.attempt_models import BoundRoleReferences, PreparedRoleWorkspace, RoleAttemptRequest


@dataclass
class ReferenceBinding:
    repository: BunshinV2Repository
    task_ledger: TaskLedgerService
    workflow_facts: WorkflowFacts

    async def execute(self, command: RoleAttemptRequest, stage_workspace_preparation: PreparedRoleWorkspace) -> BoundRoleReferences:
        activation = command.activation
        bound_input_entries = stage_workspace_preparation.bound_input_entries
        bound_reference_refs = stage_workspace_preparation.bound_reference_refs
        contract_authoring = stage_workspace_preparation.contract_authoring
        profile = command.profile
        snapshot = command.snapshot
        workspace = stage_workspace_preparation.workspace
        references: list[dict[str, Any]] = []
        reference_items = list(bound_reference_refs.items())
        if activation.mode == RoleMode.REPAIR:
            priority = {
                "repair_bill": 0,
                "module_work_view": 1,
                "unit_work_view": 1,
                "workspace_preparation": 2,
            }
            reference_items.sort(key=lambda item: (priority.get(item[0], 3), item[0]))
        for name, ref in reference_items:
            includes: list[str] = []
            if ref.artifact_type == "LocalPathReference":
                path = str(ref.media_type)
            elif ref.artifact_type == TASK_LEDGER_ARTIFACT:
                materialized = self.task_ledger.materialize(ref)
                path = str(materialized.root)
                includes = list(materialized.files)
            else:
                record = self.repository.artifacts.read_artifact_record(ref.sha256)
                if record is None:
                    raise ValueError(f"worker input artifact is unavailable: {name}")
                path = str(
                    self.task_ledger.materialize_artifact(
                        ref,
                        semantic_name=name,
                    )
                )
            references.append(
                {
                    "name": name,
                    "path": path,
                    "description": f"V2 immutable input {name}",
                    "truth_source": True,
                    "required": True,
                    "bound_input": False,
                    **({"include": includes} if includes else {}),
                }
            )
        workspace["reference_paths"] = [*references, *bound_input_entries]
        _attach_bound_input_read_only_overlays(workspace, bound_input_entries)
        profile_group, profile_name = canonical_role_profile_parts(profile)
        if contract_authoring and activation.role == OrchestrationRole.ARCHITECT:
            invocation_acceptance = [
                "Spend architecture work on the declaration-level skeleton; never compile, build, test, link, or execute product behavior. Any private product code incidentally authored must stay inside its final owning module's implementation_scopes, has no contract authority, and may be replaced by the Coder.",
                "Finish only when boundaries and responsibilities, unique state/resource owners, contracts, and closed lifecycle/joins are declared independently of any private product draft.",
                "Use update_checklist as the fixed durable phase cursor: settle requirements and the complete semantic module graph first; then write only declaration skeletons; only then fill the Manager-preseeded architect.yaml, reconcile both projections, complete the checklist, and call submit_contract with no arguments. Never work ahead of the current phase.",
            ]
        elif (
            contract_authoring or profile_group == "software_engineering"
        ) and activation == RoleActivation(
            OrchestrationRole.REVIEWER,
            RoleMode.ARCHITECTURE,
        ):
            invocation_acceptance = [
                "This is architecture review, not product verification. Judge whether a future Coder can implement the task from the declarations and semantic DAG; never inspect or execute private bodies merely to show that requested behavior is not implemented yet.",
                "Investigate a bound reference path only when its type or relevance is unknown. If supplied metadata or evidence already in context establishes the relevant file, read it directly without repeating discovery. Use bounded discovery for unclassified paths.",
                "Read the skeleton diff first. Ignore private product bodies inside their owning module's declared implementation_scopes: they are non-authoritative Coder drafts and cannot prove or invalidate the architecture. Reject tests, build machinery, cross-module or undeclared writes, and any contract whose feasibility depends on private draft behavior.",
                "This logical Reviewer persists across Candidates, but no verdict does. For every new Candidate, first regress all prior findings and touched accepted invariants; then inspect the current skeleton diff and affected semantic neighborhood for new defects. Reuse unchanged investigation instead of rereading it.",
                "Review the bound task.yaml ledger in order, code contracts, semantic dependencies, and scenarios; reconcile every exact Manager-recorded question and answer.",
                "Treat the Manager-derived tests/<module_name>/developer and tests/<module_name>/verifier corpora as implementation and verification infrastructure: they are intentionally absent from Architect-declared paths and scenarios, so their absence is not a defect.",
                "Compile only focused declaration/protocol consumers to confirm contracts compose; compilation is not product behavior proof and must not require implementation bodies.",
                "For every Requirement and observable scenario claim, trace declared interface semantics from a concrete entrypoint through data/state/error transitions to a legal terminal. Explicit composable semantics are required; current implementation availability and current end-to-end behavior are outside this verdict.",
                "For every module, verify responsibility, dependency handoffs and consumed outputs, input/output/error/invariant contracts, ownership, lifecycle, optional state machine, and agreement between architect.yaml and declaration comments.",
                "Before PASS, reject every public semantic ambiguity: audit absent/null/empty/zero-length inputs, partial output followed by failure and its commitment/consumption/post-error state, and all permitted copy/move/clone/share/reset/reuse operations on public stateful values. If two conforming implementations may make observably different choices that a consumer must know, add a finding; review_guarded private implementation freedom cannot close that gap.",
                "PASS only when key scenarios traverse the contract graph, failure paths terminate legally, every Requirement maps, no dependency is undeclared, and every observable edge case has one declared outcome or an explicitly declared set of outcomes safe for every consumer.",
                "Inspect the complete Manager-bound scope, record every material defect with add_finding, complete the checklist, and call submit_review once.",
            ]
        elif activation == RoleActivation(
            OrchestrationRole.REVIEWER,
            RoleMode.ARCHITECTURE,
        ):
            invocation_acceptance = [
                "Review the ordered task ledger and the complete immutable contract breadth-first; never stop at the first defect.",
                "Trace each requirement through module dependencies, ownership, lifecycle, errors, and scenarios to a legal success or failure endpoint. Schema validity is not semantic proof.",
                "Record every independent defect with add_finding, complete the checklist, and call submit_review exactly once. Do not repair or redesign private implementation.",
            ]
        elif activation.role == OrchestrationRole.ARCHITECT:
            invocation_acceptance = [
                "Read the ordered task ledger and perform one bounded consistency pass before authoring Contract fields.",
                "Immediately call update_checklist after that pass, then work only on its current phase. Externalize each settled phase to the durable Contract Draft before moving on; batch independent tool calls in one response and sequence only dependent definitions.",
                "Complete the fixed checklist, reconcile topology and end-to-end integration against the task, then call submit_contract with no arguments. The checklist is a cursor, never contract truth or review evidence.",
            ]
        elif activation.role == OrchestrationRole.VERIFIER:
            invocation_acceptance = [
                "Read and run both durable corpora; extend only tests/<module_name>/verifier for demonstrated coverage gaps, while tests/<module_name>/developer remains read-only. Reuse unchanged contract analysis and coverage mapping, but rerun required evidence on the current Candidate and validate affected checks after the final corpus edit. Keep sink end-to-end cases in the same verifier corpus.",
                "For this assignment, first record every required current/historical regression, then record a current-Candidate diff-risk check for newly introduced defects. A failing regression blocks PASS but never skips the diff-risk phase.",
                "Use the visible dedicated verification run tools for classified evidence and ordinary shell for read-only Git inspection. A successful classified execution can also supply its final-corpus receipt; do not rerun it solely to obtain an ordinary-shell receipt. Use read_verification_draft_status to resolve readiness or next-action uncertainty; follow ready_by_outcome, blockers_by_outcome, missing_historical_cases, and applicable next_actions rather than polling after every phase.",
                "Follow the Role Contract completion rule and Manager readiness, supplying UNKNOWN's environmental reason and follow-up plan at submission when needed. Call exactly one semantic verification outcome tool; do not construct a VerificationPlan or evidence JSON.",
            ]
        elif activation.role == OrchestrationRole.IMPLEMENTATION:
            if self.workflow_facts.execution_adapter(snapshot) == SOFTWARE_GIT_ADAPTER:
                invocation_acceptance = [
                    "Implement or repair only the bound module, write focused tests only in tests/<module_name>/developer, and keep tests/<module_name>/verifier read-only.",
                    "Treat private product code inherited from the Architect as a non-authoritative draft: inspect it as workspace evidence, then keep, modify, delete, or replace it as needed; you own and must validate the final implementation.",
                    "Maintain the compact durable checklist with update_checklist; it is a micro-plan, not evidence. Complete it, run the minimum sufficient self-check with ordinary shell or LSP tools, then call submit_candidate with no arguments.",
                ]
            else:
                invocation_acceptance = [
                    "Write the contracted product artifact in the bound workspace and run a focused validation.",
                    "Maintain a compact checklist with update_checklist, run one focused self-check with ordinary available tools, then call submit_candidate with no arguments; do not write producer_report.json.",
                ]
        elif activation.mode == RoleMode.STANDALONE:
            invocation_acceptance = [
                "Review only the bound immutable target and run reproducible read-only probes.",
                "Use the checklist as the audit cursor, record every independent defect with add_finding, then call submit_review with no arguments.",
            ]
        else:
            invocation_acceptance = ["Write the exact primary JSON artifact required by the profile output contract."]
        mandatory_inputs: list[str] = []
        evaluation_generation = 0
        if activation == RoleActivation(OrchestrationRole.REVIEWER, RoleMode.ARCHITECTURE):
            evaluation_generation = int(snapshot.payload.get("architecture_review_generation") or 0)
        elif activation == RoleActivation(OrchestrationRole.VERIFIER, RoleMode.MODULE):
            evaluation_generation = int(snapshot.payload.get("verifier_evaluation_generation") or 0)
        return BoundRoleReferences(
            evaluation_generation=evaluation_generation, invocation_acceptance=invocation_acceptance,
            profile_group=profile_group, profile_name=profile_name, references=references,
        )
