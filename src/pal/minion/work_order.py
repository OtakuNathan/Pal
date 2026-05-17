from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


def new_work_id(prefix: str) -> str:
    safe_prefix = "".join(ch for ch in str(prefix or "").strip() if ch.isalnum() or ch == "_")
    return f"{safe_prefix or 'id'}_{uuid4().hex[:16]}"


@dataclass(frozen=True)
class MilestoneSpec:
    milestone_id: str
    title: str = ""
    task: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    skill_refs: list[str] = field(default_factory=list)
    test_plan: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "milestone_id": self.milestone_id,
            "title": self.title,
            "task": self.task,
            "acceptance_criteria": list(self.acceptance_criteria),
            "skill_refs": list(self.skill_refs),
            "test_plan": dict(self.test_plan),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | str, *, index: int = 0) -> "MilestoneSpec":
        if isinstance(payload, str):
            title = str(payload or "").strip() or f"Milestone {index + 1}"
            return cls(milestone_id=f"m{index + 1}", title=title, task=title)
        data = dict(payload or {})
        title = str(data.get("title") or data.get("name") or f"Milestone {index + 1}").strip()
        task = str(data.get("task") or data.get("summary") or data.get("goal") or title).strip()
        return cls(
            milestone_id=str(data.get("milestone_id") or data.get("id") or f"m{index + 1}").strip(),
            title=title,
            task=task,
            acceptance_criteria=_string_list(data.get("acceptance_criteria") or data.get("acceptance")),
            skill_refs=_skill_refs_from(data),
            test_plan=_dict(data.get("test_plan")),
            metadata=_dict(data.get("metadata")),
        )


@dataclass(frozen=True)
class ModulePlan:
    module_id: str
    owned_area: list[str] = field(default_factory=list)
    responsibility: str = ""
    provided_interfaces: list[dict[str, Any]] = field(default_factory=list)
    consumed_interfaces: list[dict[str, Any]] = field(default_factory=list)
    internal_milestones: list[MilestoneSpec] = field(default_factory=list)
    test_plan: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "module_id": self.module_id,
            "owned_area": list(self.owned_area),
            "responsibility": self.responsibility,
            "provided_interfaces": [dict(item) for item in self.provided_interfaces],
            "consumed_interfaces": [dict(item) for item in self.consumed_interfaces],
            "internal_milestones": [item.to_dict() for item in self.internal_milestones],
            "test_plan": dict(self.test_plan),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, index: int = 0) -> "ModulePlan":
        data = dict(payload or {})
        module_id = str(data.get("module_id") or data.get("id") or f"module_{index + 1}").strip()
        milestones = data.get("internal_milestones")
        if milestones is None:
            milestones = data.get("milestones")
        return cls(
            module_id=module_id,
            owned_area=_string_list(data.get("owned_area") or data.get("owned_paths")),
            responsibility=str(data.get("responsibility") or data.get("purpose") or "").strip(),
            provided_interfaces=_dict_list(data.get("provided_interfaces")),
            consumed_interfaces=_dict_list(data.get("consumed_interfaces")),
            internal_milestones=[
                MilestoneSpec.from_dict(item, index=item_index)
                for item_index, item in enumerate(list(milestones or []))
                if isinstance(item, (dict, str))
            ],
            test_plan=_dict(data.get("test_plan")),
            metadata=_dict(data.get("metadata")),
        )


@dataclass(frozen=True)
class PlanArtifact:
    plan_id: str
    task_id: str
    summary: str = ""
    modules: list[ModulePlan] = field(default_factory=list)
    cross_module_contracts: list[dict[str, Any]] = field(default_factory=list)
    orchestration: dict[str, Any] = field(default_factory=dict)
    system_test_plan: list[dict[str, Any]] = field(default_factory=list)
    risks: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "task_id": self.task_id,
            "summary": self.summary,
            "modules": [item.to_dict() for item in self.modules],
            "cross_module_contracts": [dict(item) for item in self.cross_module_contracts],
            "orchestration": dict(self.orchestration),
            "system_test_plan": [dict(item) for item in self.system_test_plan],
            "risks": [dict(item) for item in self.risks],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PlanArtifact":
        data = dict(payload or {})
        return cls(
            plan_id=str(data.get("plan_id") or data.get("id") or new_work_id("plan")).strip(),
            task_id=str(data.get("task_id") or "").strip(),
            summary=str(data.get("summary") or "").strip(),
            modules=[
                ModulePlan.from_dict(item, index=index)
                for index, item in enumerate(list(data.get("modules") or []))
                if isinstance(item, dict)
            ],
            cross_module_contracts=_dict_list(data.get("cross_module_contracts")),
            orchestration=_dict(data.get("orchestration")),
            system_test_plan=_dict_list(data.get("system_test_plan")),
            risks=_dict_list(data.get("risks")),
            metadata=_dict(data.get("metadata")),
        )


@dataclass(frozen=True)
class CoderWorkOrder:
    work_order_id: str
    task_id: str
    module_id: str
    milestone_id: str
    attempt: int = 1
    role: str = "coder"
    owned_area: list[str] = field(default_factory=list)
    responsibility: str = ""
    current_milestone: MilestoneSpec | None = None
    relevant_contracts: list[dict[str, Any]] = field(default_factory=list)
    skill_refs: list[str] = field(default_factory=list)
    allowed_capabilities: list[str] = field(default_factory=list)
    test_plan: dict[str, Any] = field(default_factory=dict)
    output_contract: dict[str, Any] = field(default_factory=dict)
    source_artifact_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_order_id": self.work_order_id,
            "task_id": self.task_id,
            "role": self.role,
            "attempt": int(self.attempt),
            "module_id": self.module_id,
            "milestone_id": self.milestone_id,
            "owned_area": list(self.owned_area),
            "responsibility": self.responsibility,
            "current_milestone": self.current_milestone.to_dict() if self.current_milestone is not None else {},
            "relevant_contracts": [dict(item) for item in self.relevant_contracts],
            "skill_refs": list(self.skill_refs),
            "allowed_capabilities": list(self.allowed_capabilities),
            "test_plan": dict(self.test_plan),
            "output_contract": dict(self.output_contract),
            "source_artifact_ids": list(self.source_artifact_ids),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CoderWorkOrder":
        data = dict(payload or {})
        milestone = data.get("current_milestone")
        return cls(
            work_order_id=str(data.get("work_order_id") or new_work_id("wo")).strip(),
            task_id=str(data.get("task_id") or "").strip(),
            role=str(data.get("role") or "coder").strip() or "coder",
            attempt=_coerce_int(data.get("attempt"), default=1),
            module_id=str(data.get("module_id") or "").strip(),
            milestone_id=str(data.get("milestone_id") or "").strip(),
            owned_area=_string_list(data.get("owned_area")),
            responsibility=str(data.get("responsibility") or "").strip(),
            current_milestone=MilestoneSpec.from_dict(milestone) if isinstance(milestone, dict) else None,
            relevant_contracts=_dict_list(data.get("relevant_contracts")),
            skill_refs=_skill_refs_from(data),
            allowed_capabilities=_string_list(data.get("allowed_capabilities")),
            test_plan=_dict(data.get("test_plan")),
            output_contract=_dict(data.get("output_contract")),
            source_artifact_ids=_string_list(data.get("source_artifact_ids")),
            metadata=_dict(data.get("metadata")),
        )


@dataclass(frozen=True)
class ReviewerWorkOrder:
    work_order_id: str
    task_id: str
    review_target: dict[str, Any]
    attempt: int = 1
    role: str = "reviewer"
    relevant_contracts: list[dict[str, Any]] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    test_evidence: list[dict[str, Any]] = field(default_factory=list)
    skill_refs: list[str] = field(default_factory=list)
    allowed_capabilities: list[str] = field(default_factory=list)
    output_contract: dict[str, Any] = field(default_factory=dict)
    source_artifact_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_order_id": self.work_order_id,
            "task_id": self.task_id,
            "role": self.role,
            "attempt": int(self.attempt),
            "review_target": dict(self.review_target),
            "relevant_contracts": [dict(item) for item in self.relevant_contracts],
            "acceptance_criteria": list(self.acceptance_criteria),
            "test_evidence": [dict(item) for item in self.test_evidence],
            "skill_refs": list(self.skill_refs),
            "allowed_capabilities": list(self.allowed_capabilities),
            "output_contract": dict(self.output_contract),
            "source_artifact_ids": list(self.source_artifact_ids),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReviewerWorkOrder":
        data = dict(payload or {})
        return cls(
            work_order_id=str(data.get("work_order_id") or new_work_id("wo")).strip(),
            task_id=str(data.get("task_id") or "").strip(),
            role=str(data.get("role") or "reviewer").strip() or "reviewer",
            attempt=_coerce_int(data.get("attempt"), default=1),
            review_target=_dict(data.get("review_target")),
            relevant_contracts=_dict_list(data.get("relevant_contracts")),
            acceptance_criteria=_string_list(data.get("acceptance_criteria")),
            test_evidence=_dict_list(data.get("test_evidence")),
            skill_refs=_skill_refs_from(data),
            allowed_capabilities=_string_list(data.get("allowed_capabilities")),
            output_contract=_dict(data.get("output_contract")),
            source_artifact_ids=_string_list(data.get("source_artifact_ids")),
            metadata=_dict(data.get("metadata")),
        )


@dataclass(frozen=True)
class PromptView:
    role: str
    task_id: str
    work_order_id: str
    module: dict[str, Any] = field(default_factory=dict)
    milestone: dict[str, Any] = field(default_factory=dict)
    relevant_contracts: list[dict[str, Any]] = field(default_factory=list)
    skill_refs: list[str] = field(default_factory=list)
    allowed_capabilities: list[str] = field(default_factory=list)
    test_plan: dict[str, Any] = field(default_factory=dict)
    output_contract: dict[str, Any] = field(default_factory=dict)
    workspace: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "task_id": self.task_id,
            "work_order_id": self.work_order_id,
            "module": dict(self.module),
            "milestone": dict(self.milestone),
            "relevant_contracts": [dict(item) for item in self.relevant_contracts],
            "skill_refs": list(self.skill_refs),
            "allowed_capabilities": list(self.allowed_capabilities),
            "test_plan": dict(self.test_plan),
            "output_contract": dict(self.output_contract),
            "workspace": dict(self.workspace),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PromptView":
        data = dict(payload or {})
        return cls(
            role=str(data.get("role") or "").strip(),
            task_id=str(data.get("task_id") or "").strip(),
            work_order_id=str(data.get("work_order_id") or "").strip(),
            module=_dict(data.get("module")),
            milestone=_dict(data.get("milestone")),
            relevant_contracts=_dict_list(data.get("relevant_contracts")),
            skill_refs=_skill_refs_from(data),
            allowed_capabilities=_string_list(data.get("allowed_capabilities")),
            test_plan=_dict(data.get("test_plan")),
            output_contract=_dict(data.get("output_contract")),
            workspace=_dict(data.get("workspace")),
        )


@dataclass(frozen=True)
class AskUserQuestion:
    task_id: str
    work_order_id: str
    questions: list[dict[str, Any]]
    turn_index: int = 0
    plan_revision: int = 0
    plan_draft_id: str = ""
    output_type: str = "ask_user_question"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.output_type,
            "task_id": self.task_id,
            "work_order_id": self.work_order_id,
            "turn_index": int(self.turn_index),
            "plan_revision": int(self.plan_revision),
            "plan_draft_id": self.plan_draft_id,
            "questions": [dict(item) for item in self.questions],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AskUserQuestion":
        data = dict(payload or {})
        return cls(
            task_id=str(data.get("task_id") or "").strip(),
            work_order_id=str(data.get("work_order_id") or "").strip(),
            turn_index=_coerce_int(data.get("turn_index"), default=0),
            plan_revision=_coerce_int(data.get("plan_revision"), default=0),
            plan_draft_id=str(data.get("plan_draft_id") or "").strip(),
            questions=_dict_list(data.get("questions")),
        )


@dataclass(frozen=True)
class MilestoneReport:
    report_id: str
    task_id: str
    work_order_id: str
    module_id: str
    milestone_id: str
    status: str
    summary: str = ""
    changed_files: list[str] = field(default_factory=list)
    implemented_interfaces: list[dict[str, Any]] = field(default_factory=list)
    test_evidence: list[dict[str, Any]] = field(default_factory=list)
    commit_sha: str = ""
    artifact_ids: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    memory_candidates: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "task_id": self.task_id,
            "work_order_id": self.work_order_id,
            "module_id": self.module_id,
            "milestone_id": self.milestone_id,
            "status": self.status,
            "summary": self.summary,
            "changed_files": list(self.changed_files),
            "implemented_interfaces": [dict(item) for item in self.implemented_interfaces],
            "test_evidence": [dict(item) for item in self.test_evidence],
            "commit_sha": self.commit_sha,
            "artifact_ids": list(self.artifact_ids),
            "blockers": list(self.blockers),
            "memory_candidates": [dict(item) for item in self.memory_candidates],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MilestoneReport":
        data = dict(payload or {})
        return cls(
            report_id=str(data.get("report_id") or new_work_id("mr")).strip(),
            task_id=str(data.get("task_id") or "").strip(),
            work_order_id=str(data.get("work_order_id") or "").strip(),
            module_id=str(data.get("module_id") or "").strip(),
            milestone_id=str(data.get("milestone_id") or "").strip(),
            status=str(data.get("status") or "blocked").strip(),
            summary=str(data.get("summary") or "").strip(),
            changed_files=_string_list(data.get("changed_files")),
            implemented_interfaces=_dict_list(data.get("implemented_interfaces")),
            test_evidence=_dict_list(data.get("test_evidence")),
            commit_sha=str(data.get("commit_sha") or "").strip(),
            artifact_ids=_string_list(data.get("artifact_ids")),
            blockers=_string_list(data.get("blockers")),
            memory_candidates=_dict_list(data.get("memory_candidates")),
        )


@dataclass(frozen=True)
class ReviewReport:
    report_id: str
    task_id: str
    work_order_id: str
    status: str
    summary: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    test_gaps: list[str] = field(default_factory=list)
    residual_risk: str = ""
    blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "task_id": self.task_id,
            "work_order_id": self.work_order_id,
            "status": self.status,
            "summary": self.summary,
            "findings": [dict(item) for item in self.findings],
            "test_gaps": list(self.test_gaps),
            "residual_risk": self.residual_risk,
            "blockers": list(self.blockers),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReviewReport":
        data = dict(payload or {})
        return cls(
            report_id=str(data.get("report_id") or new_work_id("rr")).strip(),
            task_id=str(data.get("task_id") or "").strip(),
            work_order_id=str(data.get("work_order_id") or "").strip(),
            status=str(data.get("status") or "blocked").strip(),
            summary=str(data.get("summary") or "").strip(),
            findings=_dict_list(data.get("findings")),
            test_gaps=_string_list(data.get("test_gaps")),
            residual_risk=str(data.get("residual_risk") or "").strip(),
            blockers=_string_list(data.get("blockers")),
        )


def build_planner_work_order(
    *,
    goal: str,
    task_id: str = "",
    work_order_id: str = "",
    turn_index: int = 0,
    plan_revision: int = 0,
    clarifications: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "work_order_id": work_order_id or new_work_id("wo"),
        "task_id": task_id or new_work_id("task"),
        "role": "planner",
        "turn_index": int(turn_index),
        "plan_revision": int(plan_revision),
        "goal": str(goal or "").strip(),
        "planning_requirements": planner_requirements(),
        "clarifications": [dict(item) for item in list(clarifications or []) if isinstance(item, dict)],
        "output_contract": {
            "allowed_outputs": ["plan_draft", "ask_user_question", "final_plan_artifact"],
            "final_artifact": "PlanArtifact",
        },
    }


def planner_requirements() -> dict[str, Any]:
    return {
        "split_by_module_first": True,
        "require_module_contracts": True,
        "require_internal_milestones": True,
        "require_orchestration": True,
        "test_plan_levels": ["unit", "module", "integration", "system"],
        "ask_user_policy": ask_user_policy(),
    }


def ask_user_policy() -> dict[str, Any]:
    return {
        "ask_only_user_answerable": True,
        "do_not_ask_if_repo_discoverable": True,
        "bundle_related_questions": True,
        "max_questions_per_turn": 3,
        "must_include_why_needed": True,
        "must_include_evidence_after_code_investigation": True,
        "ask_early_after_skeleton_plan": True,
        "valid_topics": [
            "requirements",
            "preference",
            "tradeoff",
            "edge_case",
            "priority",
            "destructive_operation",
            "permission_expansion",
            "data_migration",
            "network_or_credentials",
        ],
    }


def compile_coder_work_order(
    plan: PlanArtifact | dict[str, Any],
    *,
    module_id: str = "",
    milestone_id: str = "",
    work_order_id: str = "",
    attempt: int = 1,
    allowed_capabilities: list[str] | None = None,
    source_artifact_ids: list[str] | None = None,
    workspace: dict[str, Any] | None = None,
) -> CoderWorkOrder:
    artifact = plan if isinstance(plan, PlanArtifact) else PlanArtifact.from_dict(dict(plan or {}))
    module = _select_module(artifact.modules, module_id)
    milestone_index, milestone = _select_milestone_with_index(module.internal_milestones, milestone_id)
    relevant_contracts = [
        *module.provided_interfaces,
        *module.consumed_interfaces,
        *[
            dict(item)
            for item in artifact.cross_module_contracts
            if _contract_mentions_module(item, module.module_id)
        ],
    ]
    test_plan = dict(module.test_plan)
    if milestone.test_plan:
        test_plan.update(dict(milestone.test_plan))
    return CoderWorkOrder(
        work_order_id=work_order_id or new_work_id("wo"),
        task_id=artifact.task_id,
        attempt=max(1, int(attempt or 1)),
        module_id=module.module_id,
        milestone_id=milestone.milestone_id,
        owned_area=list(module.owned_area),
        responsibility=module.responsibility,
        current_milestone=milestone,
        relevant_contracts=relevant_contracts,
        skill_refs=_dedupe(
            [
                *milestone.skill_refs,
                *_string_list(module.metadata.get("skill_refs") or module.metadata.get("suggested_skills")),
            ]
        ),
        allowed_capabilities=list(allowed_capabilities or []),
        test_plan=test_plan,
        output_contract={
            "must_return": [
                "summary",
                "changed_files",
                "test_evidence",
                "milestone_report",
            ]
        },
        source_artifact_ids=list(source_artifact_ids or []),
        metadata={
            "plan_id": artifact.plan_id,
            "milestone_index": milestone_index,
            "workspace": _prompt_workspace(workspace or {}),
        },
    )


def prompt_view_for_coder(work_order: CoderWorkOrder | dict[str, Any]) -> PromptView:
    order = work_order if isinstance(work_order, CoderWorkOrder) else CoderWorkOrder.from_dict(dict(work_order or {}))
    milestone = order.current_milestone.to_dict() if order.current_milestone is not None else {}
    if "milestone_index" in order.metadata:
        milestone.setdefault("milestone_index", _coerce_int(order.metadata.get("milestone_index"), default=0))
    return PromptView(
        role=order.role,
        task_id=order.task_id,
        work_order_id=order.work_order_id,
        module={
            "module_id": order.module_id,
            "owned_area": list(order.owned_area),
            "responsibility": order.responsibility,
        },
        milestone=milestone,
        relevant_contracts=[dict(item) for item in order.relevant_contracts],
        skill_refs=list(order.skill_refs),
        allowed_capabilities=list(order.allowed_capabilities),
        test_plan=dict(order.test_plan),
        output_contract=dict(order.output_contract),
        workspace=_prompt_workspace(_dict(order.metadata.get("workspace"))),
    )


def prompt_view_for_reviewer(work_order: ReviewerWorkOrder | dict[str, Any]) -> dict[str, Any]:
    order = work_order if isinstance(work_order, ReviewerWorkOrder) else ReviewerWorkOrder.from_dict(dict(work_order or {}))
    return PromptView(
        role=order.role,
        task_id=order.task_id,
        work_order_id=order.work_order_id,
        module={},
        milestone={},
        relevant_contracts=[dict(item) for item in order.relevant_contracts],
        skill_refs=list(order.skill_refs),
        allowed_capabilities=list(order.allowed_capabilities),
        test_plan={"evidence": [dict(item) for item in order.test_evidence]},
        output_contract=dict(order.output_contract),
        workspace=_prompt_workspace(_dict(order.metadata.get("workspace"))),
    ).to_dict() | {
        "review_target": dict(order.review_target),
        "acceptance_criteria": list(order.acceptance_criteria),
    }


def prompt_view_from_metadata(metadata: dict[str, Any], *, workspace: dict[str, Any] | None = None) -> dict[str, Any]:
    data = dict(metadata or {})
    if isinstance(data.get("prompt_view"), dict):
        raw_prompt_view = dict(data.get("prompt_view") or {})
        prompt_view = PromptView.from_dict(raw_prompt_view).to_dict()
        for key in (
            "review_target",
            "acceptance_criteria",
            "planning_goal",
            "planning_requirements",
            "clarifications",
            "turn_index",
            "plan_revision",
        ):
            if key in raw_prompt_view:
                prompt_view[key] = raw_prompt_view[key]
        if workspace:
            prompt_view["workspace"] = _merge_prompt_workspace(prompt_view.get("workspace"), workspace)
        return prompt_view
    if isinstance(data.get("coder_work_order"), dict):
        prompt_view = prompt_view_for_coder(dict(data.get("coder_work_order") or {})).to_dict()
        if workspace:
            prompt_view["workspace"] = _prompt_workspace(workspace)
        return prompt_view
    if isinstance(data.get("reviewer_work_order"), dict):
        prompt_view = prompt_view_for_reviewer(dict(data.get("reviewer_work_order") or {}))
        if workspace:
            prompt_view["workspace"] = _prompt_workspace(workspace)
        return prompt_view
    if isinstance(data.get("planner_work_order"), dict):
        return _planner_prompt_view(dict(data.get("planner_work_order") or {}), workspace=workspace or {})
    return {}


def validate_final_plan_artifact(payload: PlanArtifact | dict[str, Any]) -> PlanArtifact:
    artifact = payload if isinstance(payload, PlanArtifact) else PlanArtifact.from_dict(dict(payload or {}))
    raw_type = ""
    if isinstance(payload, dict):
        raw_type = str(payload.get("type") or payload.get("output_type") or "").strip()
    if raw_type and raw_type not in {"FinalPlanArtifact", "final_plan_artifact"}:
        raise ValueError(f"expected FinalPlanArtifact, got {raw_type}")
    errors: list[str] = []
    if not artifact.task_id:
        errors.append("task_id is required")
    if not artifact.modules:
        errors.append("modules is required")
    for module_index, module in enumerate(artifact.modules):
        if not module.module_id:
            errors.append(f"modules[{module_index}].module_id is required")
        if not module.internal_milestones:
            errors.append(f"modules[{module_index}].internal_milestones is required")
        for milestone_index, milestone in enumerate(module.internal_milestones):
            if not milestone.milestone_id:
                errors.append(f"modules[{module_index}].internal_milestones[{milestone_index}].milestone_id is required")
            if not (milestone.task or milestone.title):
                errors.append(f"modules[{module_index}].internal_milestones[{milestone_index}].task is required")
    if errors:
        raise ValueError("invalid FinalPlanArtifact: " + "; ".join(errors))
    return artifact


def module_milestone_records(plan: PlanArtifact | dict[str, Any], *, module_id: str = "") -> list[dict[str, Any]]:
    artifact = plan if isinstance(plan, PlanArtifact) else PlanArtifact.from_dict(dict(plan or {}))
    module = _select_module(artifact.modules, module_id)
    if not module.internal_milestones:
        return [{"title": "Complete module milestone", "summary": "Complete module milestone", "acceptance": []}]
    return [
        {
            "title": milestone.title or milestone.milestone_id or f"Milestone {index + 1}",
            "summary": milestone.task or milestone.title,
            "acceptance": list(milestone.acceptance_criteria),
        }
        for index, milestone in enumerate(module.internal_milestones)
    ]


def plan_module_id_at(plan: PlanArtifact | dict[str, Any], *, module_id: str = "") -> str:
    artifact = plan if isinstance(plan, PlanArtifact) else PlanArtifact.from_dict(dict(plan or {}))
    return _select_module(artifact.modules, module_id).module_id


def plan_milestone_id_at(plan: PlanArtifact | dict[str, Any], *, module_id: str = "", milestone_index: int = 0) -> str:
    artifact = plan if isinstance(plan, PlanArtifact) else PlanArtifact.from_dict(dict(plan or {}))
    module = _select_module(artifact.modules, module_id)
    if not module.internal_milestones:
        return "m1"
    index = max(0, min(int(milestone_index or 0), len(module.internal_milestones) - 1))
    return module.internal_milestones[index].milestone_id


def _planner_prompt_view(work_order: dict[str, Any], *, workspace: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "planner",
        "task_id": str(work_order.get("task_id") or ""),
        "work_order_id": str(work_order.get("work_order_id") or ""),
        "planning_goal": str(work_order.get("goal") or ""),
        "turn_index": _coerce_int(work_order.get("turn_index"), default=0),
        "plan_revision": _coerce_int(work_order.get("plan_revision"), default=0),
        "planning_requirements": _dict(work_order.get("planning_requirements") or planner_requirements()),
        "clarifications": _dict_list(work_order.get("clarifications")),
        "output_contract": _dict(work_order.get("output_contract")),
        "workspace": _prompt_workspace(workspace),
    }


def _select_module(modules: list[ModulePlan], module_id: str) -> ModulePlan:
    if not modules:
        raise ValueError("PlanArtifact.modules is required")
    wanted = str(module_id or "").strip()
    if wanted:
        for item in modules:
            if item.module_id == wanted:
                return item
        raise KeyError(f"unknown module_id: {wanted}")
    return modules[0]


def _select_milestone(milestones: list[MilestoneSpec], milestone_id: str) -> MilestoneSpec:
    return _select_milestone_with_index(milestones, milestone_id)[1]


def _select_milestone_with_index(milestones: list[MilestoneSpec], milestone_id: str) -> tuple[int, MilestoneSpec]:
    if not milestones:
        return 0, MilestoneSpec(milestone_id="m1", title="Complete module milestone", task="Complete module milestone")
    wanted = str(milestone_id or "").strip()
    if wanted:
        for index, item in enumerate(milestones):
            if item.milestone_id == wanted:
                return index, item
        raise KeyError(f"unknown milestone_id: {wanted}")
    return 0, milestones[0]


def _contract_mentions_module(contract: dict[str, Any], module_id: str) -> bool:
    values = {
        str(contract.get(key) or "")
        for key in ("module_id", "module", "producer", "consumer", "from", "to", "owner")
    }
    return str(module_id or "") in values


def _prompt_workspace(workspace: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "repo_path",
        "source_repo",
        "artifact_dir",
        "work_order_branch",
        "merge_target",
        "base_sha",
        "task_repo_path",
        "target_repo_path",
    }
    return {key: str(value) for key, value in dict(workspace or {}).items() if key in allowed and str(value or "").strip()}


def _merge_prompt_workspace(existing: Any, workspace: dict[str, Any]) -> dict[str, str]:
    merged = _prompt_workspace(existing if isinstance(existing, dict) else {})
    merged.update(_prompt_workspace(workspace))
    return merged


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _skill_refs_from(data: dict[str, Any]) -> list[str]:
    return _string_list(data.get("skill_refs") or data.get("suggested_skills") or data.get("skills"))


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)
