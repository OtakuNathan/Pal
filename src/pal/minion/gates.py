from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pal.foundation import utc_now
from pal.minion.checklist import build_acceptance_checklist, compact_checklist, repair_acceptance_refs
from pal.minion.utils import coerce_bool, coerce_int
from pal.shared import TaskContextPack


GATE_TRIGGER_TERMINAL = "terminal"
GATE_TRIGGER_BEFORE_PLAN = "before_plan"
GATE_TRIGGER_AFTER_EACH_MILESTONE = "after_each_milestone"

CHECKPOINT_QUALITY_GATE = "checkpoint_quality"
CHECKPOINT_ADMISSION_GATE = "checkpoint_admission"
MODULE_QUALITY_GATE = "module_quality"
PLAN_ACCEPTANCE_GATE = "plan_acceptance"
SOURCE_CONTRACT_GATE = "source_contract"
NONE_GATE = "none"

REVIEWER_GATE_STRATEGY = "reviewer"
NONE_GATE_STRATEGY = "none"

# Backward-compatible names for older callers/tests while gate definitions move
# strategy back to its execution meaning.
CHECKPOINT_QUALITY_STRATEGY = CHECKPOINT_QUALITY_GATE
PLAN_ARTIFACT_ACCEPTANCE_STRATEGY = PLAN_ACCEPTANCE_GATE


@dataclass(frozen=True)
class GateSpec:
    gate_id: str
    gate: str
    trigger: str
    strategy: str
    target_kind: str
    gate_kind: str = ""
    reviewer_profile_group: str = ""
    reviewer_profile_name: str = ""
    fallback_reviewer_profile_group: str = ""
    fallback_reviewer_profile_name: str = ""
    max_repair_attempts: int = 0
    max_revision_attempts: int = 0
    auto_revise: bool = False
    required_checks: tuple[str, ...] = ()
    blocking: tuple[str, ...] = ()
    policy: dict[str, Any] = field(default_factory=dict)

    @property
    def reviewer_profile(self) -> str:
        return _profile_ref(self.reviewer_profile_group, self.reviewer_profile_name)

    @property
    def fallback_reviewer_profile(self) -> str:
        return _profile_ref(self.fallback_reviewer_profile_group, self.fallback_reviewer_profile_name)

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty(
            {
                "id": self.gate_id,
                "gate": self.gate,
                "trigger": self.trigger,
                "strategy": self.strategy,
                "target_kind": self.target_kind,
                "gate_kind": self.gate_kind,
                "reviewer_profile_group": self.reviewer_profile_group,
                "reviewer_profile_name": self.reviewer_profile_name,
                "fallback_reviewer_profile_group": self.fallback_reviewer_profile_group,
                "fallback_reviewer_profile_name": self.fallback_reviewer_profile_name,
                "max_repair_attempts": self.max_repair_attempts,
                "max_revision_attempts": self.max_revision_attempts,
                "auto_revise": self.auto_revise,
                "required_checks": list(self.required_checks),
                "blocking": list(self.blocking),
                "policy": dict(self.policy),
            }
        )


@dataclass(frozen=True)
class GateOutcome:
    gate_ref: dict[str, Any]
    verdict: str
    todo: dict[str, Any] = field(default_factory=dict)


class GateStrategy(Protocol):
    name: str

    def project_todo(self, gate: dict[str, Any]) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class DeclarativeGateStrategy:
    name: str

    def project_todo(self, gate: dict[str, Any]) -> dict[str, Any]:
        return project_active_gate_todo(gate)


@dataclass
class GateStrategyRegistry:
    strategies: dict[str, GateStrategy] = field(default_factory=dict)

    def register(self, strategy: GateStrategy) -> None:
        name = str(getattr(strategy, "name", "") or "").strip()
        if not name:
            raise ValueError("gate strategy name is required")
        self.strategies[name] = strategy

    def get(self, name: str) -> GateStrategy | None:
        return self.strategies.get(str(name or "").strip())


class MinionGateStrategyProvider(Protocol):
    def declared_minion_gate_strategies(self) -> list[GateStrategy]:
        ...


@dataclass(frozen=True)
class GateChecklistEntry:
    entry_id: str
    text: str
    kind: str = "required_check"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty(
            {
                "id": self.entry_id,
                "kind": self.kind,
                "text": self.text,
                "metadata": dict(self.metadata),
            }
        )


@dataclass
class GateChecklistEntryRegistry:
    entries: dict[str, GateChecklistEntry] = field(default_factory=dict)

    def register(self, entry: GateChecklistEntry | dict[str, Any]) -> None:
        normalized = entry if isinstance(entry, GateChecklistEntry) else _checklist_entry_from_dict(dict(entry or {}))
        if not normalized.entry_id:
            raise ValueError("gate checklist entry id is required")
        self.entries[normalized.entry_id] = normalized

    def get(self, entry_id: str) -> GateChecklistEntry | None:
        return self.entries.get(str(entry_id or "").strip())

    def resolve_texts(self, refs: Any, *, fallback: Any = None) -> tuple[str, ...]:
        result: list[str] = []
        for ref in _string_list(refs):
            entry = self.get(ref)
            result.append(entry.text if entry is not None else ref)
        if not result:
            result = _string_list(fallback)
        return tuple(result)


@dataclass(frozen=True)
class GateDefinition:
    name: str
    target_kind: str
    gate_kind: str
    strategy: str = REVIEWER_GATE_STRATEGY
    trigger: str = GATE_TRIGGER_AFTER_EACH_MILESTONE
    reviewer_profile_group: str = ""
    reviewer_profile_name: str = ""
    fallback_reviewer_profile_group: str = ""
    fallback_reviewer_profile_name: str = ""
    max_repair_attempts: int = 0
    max_revision_attempts: int = 0
    auto_revise: bool = False
    required_check_refs: tuple[str, ...] = ()
    required_checks: tuple[str, ...] = ()
    blocking: tuple[str, ...] = ()
    policy: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty(
            {
                "gate": self.name,
                "trigger": self.trigger,
                "strategy": self.strategy,
                "target_kind": self.target_kind,
                "gate_kind": self.gate_kind,
                "reviewer_profile_group": self.reviewer_profile_group,
                "reviewer_profile_name": self.reviewer_profile_name,
                "fallback_reviewer_profile_group": self.fallback_reviewer_profile_group,
                "fallback_reviewer_profile_name": self.fallback_reviewer_profile_name,
                "max_repair_attempts": self.max_repair_attempts,
                "max_revision_attempts": self.max_revision_attempts,
                "auto_revise": self.auto_revise,
                "required_check_refs": list(self.required_check_refs),
                "required_checks": list(self.required_checks),
                "blocking": list(self.blocking),
                "policy": dict(self.policy),
            }
        )


@dataclass
class GateDefinitionRegistry:
    definitions: dict[str, GateDefinition] = field(default_factory=dict)
    checklist_entries: GateChecklistEntryRegistry = field(default_factory=GateChecklistEntryRegistry)

    def register(self, definition: GateDefinition | dict[str, Any]) -> None:
        normalized = definition if isinstance(definition, GateDefinition) else _gate_definition_from_dict(dict(definition or {}))
        name = str(normalized.name or "").strip()
        if not name:
            raise ValueError("gate definition name is required")
        self.definitions[name] = normalized

    def get(self, name: str) -> GateDefinition | None:
        return self.definitions.get(str(name or "").strip())


class MinionGateDefinitionProvider(Protocol):
    def declared_minion_gate_definitions(self) -> list[GateDefinition | dict[str, Any]]:
        ...


class MinionGateChecklistEntryProvider(Protocol):
    def declared_minion_gate_checklist_entries(self) -> list[GateChecklistEntry | dict[str, Any]]:
        ...


def default_gate_strategy_registry(providers: tuple[MinionGateStrategyProvider, ...] = ()) -> GateStrategyRegistry:
    registry = GateStrategyRegistry()
    registry.register(DeclarativeGateStrategy(REVIEWER_GATE_STRATEGY))
    for provider in providers:
        declare = getattr(provider, "declared_minion_gate_strategies", None)
        if not callable(declare):
            continue
        for strategy in list(declare() or []):
            registry.register(strategy)
    return registry


def default_gate_definition_registry(
    providers: tuple[MinionGateDefinitionProvider | MinionGateChecklistEntryProvider, ...] = (),
) -> GateDefinitionRegistry:
    registry = GateDefinitionRegistry()
    for entry in _builtin_gate_checklist_entries():
        registry.checklist_entries.register(entry)
    for definition in _builtin_gate_definitions():
        registry.register(definition)
    for provider in providers:
        declare_entries = getattr(provider, "declared_minion_gate_checklist_entries", None)
        if callable(declare_entries):
            for entry in list(declare_entries() or []):
                registry.checklist_entries.register(entry)
        declare_definitions = getattr(provider, "declared_minion_gate_definitions", None)
        if callable(declare_definitions):
            for definition in list(declare_definitions() or []):
                registry.register(definition)
    return registry


def gate_specs_from_pack(
    pack: TaskContextPack,
    *,
    trigger: str = "",
    strategy: str = "",
    target_kind: str = "",
    gate: str = "",
    gate_registry: GateDefinitionRegistry | None = None,
) -> list[GateSpec]:
    profile = dict(pack.resolved_profile or {})
    policy = dict(profile.get("effective_gate_policy") or profile.get("gate_policy") or {})
    if not policy and isinstance(pack.workspace.get("gate_policy"), dict):
        policy = dict(pack.workspace.get("gate_policy") or {})
    specs = normalize_gate_policy(policy, gate_registry=gate_registry)
    if trigger:
        normalized_trigger = _normalize_trigger(trigger)
        specs = [item for item in specs if item.trigger == normalized_trigger]
    if strategy:
        normalized_strategy = str(strategy or "").strip()
        specs = [item for item in specs if item.strategy == normalized_strategy]
    if target_kind:
        normalized_target = str(target_kind or "").strip()
        specs = [item for item in specs if item.target_kind == normalized_target]
    if gate:
        normalized_gate = str(gate or "").strip()
        specs = [item for item in specs if item.gate == normalized_gate]
    return specs


def normalize_gate_policy(
    policy: dict[str, Any],
    *,
    gate_registry: GateDefinitionRegistry | None = None,
) -> list[GateSpec]:
    data = dict(policy or {})
    registry = gate_registry or default_gate_definition_registry()
    specs: list[GateSpec] = []
    raw_gates = data.get("gates")
    if isinstance(raw_gates, list):
        for index, raw in enumerate(raw_gates):
            if str(raw or "").strip().lower() == NONE_GATE:
                return []
            if isinstance(raw, str):
                spec = _gate_spec_from_name(raw, data, index=index, registry=registry)
                if spec is not None:
                    specs.append(spec)
                continue
            if isinstance(raw, dict):
                if _gate_entry_is_none(raw):
                    return []
                spec = _gate_spec_from_dict(raw, data, index=index, registry=registry)
                if spec is not None:
                    specs.append(spec)
    return _dedupe_gate_specs(specs)


def checkpoint_gate_spec_for_pack(pack: TaskContextPack) -> GateSpec | None:
    reviewer_specs = [
        spec
        for spec in gate_specs_from_pack(
            pack,
            trigger=GATE_TRIGGER_AFTER_EACH_MILESTONE,
            target_kind="checkpoint",
        )
        if spec.strategy == REVIEWER_GATE_STRATEGY
    ]
    module_specs = [spec for spec in reviewer_specs if spec.gate == MODULE_QUALITY_GATE]
    legacy_specs = [spec for spec in reviewer_specs if spec.gate == CHECKPOINT_QUALITY_GATE]
    if module_specs:
        return module_specs[0] if _pack_is_module_terminal_checkpoint(pack) else None
    return legacy_specs[0] if legacy_specs else None


def checkpoint_admission_gate_spec_for_pack(pack: TaskContextPack) -> GateSpec | None:
    specs = gate_specs_from_pack(
        pack,
        trigger=GATE_TRIGGER_AFTER_EACH_MILESTONE,
        gate=CHECKPOINT_ADMISSION_GATE,
    )
    return specs[0] if specs else None


def checkpoint_review_policy_from_spec(spec: GateSpec | None, *, module_mode: str = "") -> dict[str, Any]:
    if spec is None:
        return {}
    scope = str(spec.policy.get("scope") or "").strip()
    if not scope:
        scope = "module_checkpoint" if str(module_mode or "").strip() else "bare_coder_checkpoint"
    return {
        "enabled": True,
        "scope": scope,
        "reviewer_profile_group": spec.reviewer_profile_group,
        "reviewer_profile_name": spec.reviewer_profile_name,
        "reviewer_profile": spec.reviewer_profile,
        "fallback_reviewer_profile": spec.fallback_reviewer_profile,
        "max_repair_attempts": int(spec.max_repair_attempts or 0) or 5,
        "require_test_or_blocker": bool(spec.policy.get("require_test_or_blocker")),
        "require_api_evidence": bool(spec.policy.get("require_api_evidence")),
        "require_lsp_when_applicable": bool(spec.policy.get("require_lsp_when_applicable")),
        "check_public_declarations_have_implementation": bool(
            spec.policy.get("check_public_declarations_have_implementation")
        ),
        "source": "gate_spec",
        "gate_spec": spec.to_dict(),
    }


def plan_gate_spec_for_pack(pack: TaskContextPack) -> GateSpec | None:
    specs = gate_specs_from_pack(
        pack,
        trigger=GATE_TRIGGER_AFTER_EACH_MILESTONE,
        gate=PLAN_ACCEPTANCE_GATE,
    )
    return specs[0] if specs else None


def _pack_is_module_terminal_checkpoint(pack: TaskContextPack) -> bool:
    metadata = dict(pack.metadata or {})
    module_execution = dict(metadata.get("module_execution") or {})
    mode = str(module_execution.get("mode") or "").strip()
    if not mode:
        return True
    current = _pack_current_milestone_index(pack, module_execution)
    count = coerce_int(module_execution.get("milestone_count"), 0)
    return count <= 1 or current >= count - 1


def _pack_current_milestone_index(pack: TaskContextPack, module_execution: dict[str, Any]) -> int:
    for source in (
        dict((pack.metadata or {}).get("prompt_view") or {}).get("milestone"),
        dict((pack.metadata or {}).get("coder_work_order") or {}).get("current_milestone"),
        dict(pack.continuity or {}).get("current_milestone"),
        module_execution,
    ):
        if not isinstance(source, dict):
            continue
        for key in ("milestone_index", "current_milestone_index"):
            if source.get(key) is not None:
                return coerce_int(source.get(key), 0)
    return 0


def plan_review_policy_from_spec(spec: GateSpec | None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    base = dict((metadata or {}).get("plan_review") or {})
    if spec is None:
        return base
    base.setdefault("enabled", True)
    base.setdefault("auto_revise", bool(spec.auto_revise))
    base.setdefault("max_revision_attempts", int(spec.max_revision_attempts or 0) or 2)
    base.setdefault("reviewer_profile_group", spec.reviewer_profile_group)
    base.setdefault("reviewer_profile_name", spec.reviewer_profile_name)
    base.setdefault("reviewer_profile", spec.reviewer_profile)
    if spec.fallback_reviewer_profile:
        base.setdefault("fallback_reviewer_profile", spec.fallback_reviewer_profile)
    base.setdefault("source", "gate_spec")
    base.setdefault("gate_spec", spec.to_dict())
    return base


def pack_requires_plan_artifact_validation(pack: TaskContextPack) -> bool:
    profile = dict(pack.resolved_profile or {})
    output_policy = dict(profile.get("effective_output_policy") or profile.get("output_policy") or {})
    if not output_policy and isinstance(pack.workspace.get("output_policy"), dict):
        output_policy = dict(pack.workspace.get("output_policy") or {})
    if coerce_bool(output_policy.get("requires_plan_artifact")):
        return True
    if plan_gate_spec_for_pack(pack) is not None:
        return True
    return False


def project_active_gate_todo(gate: dict[str, Any]) -> dict[str, Any]:
    payload = dict(gate or {})
    target = dict(payload.get("target") or {})
    verdict = str(payload.get("verdict") or "").strip().lower()
    acceptance = build_acceptance_checklist(target.get("acceptance_criteria"))
    if verdict == "pass":
        items = [
            {**dict(item), "status": "done", "metadata": {"covered_by_gate_id": str(payload.get("gate_id") or "")}}
            for item in acceptance
        ]
        return _drop_empty(
            {
                "status": "completed",
                "active": False,
                "summary": str(payload.get("summary") or "gate passed").strip(),
                "gate_ref": compact_gate_ref(payload),
                "items": compact_checklist(items),
                "updated_at": utc_now(),
            }
        )
    repair_items = _repair_todo_items(payload, acceptance)
    failed_refs = _failed_acceptance_refs(payload, acceptance)
    acceptance_items = _acceptance_todo_items(acceptance, failed_refs)
    items = [*acceptance_items, *repair_items]
    return _drop_empty(
        {
            "status": "active" if items else "blocked",
            "active": True,
            "summary": str(payload.get("summary") or "gate requires follow-up").strip(),
            "gate_ref": compact_gate_ref(payload),
            "failed_acceptance_refs": failed_refs,
            "items": items[:20],
            "repair_items": repair_items[:12],
            "acceptance_items": acceptance_items[:12],
            "updated_at": utc_now(),
        }
    )


def compact_gate_ref(gate: dict[str, Any]) -> dict[str, Any]:
    return {
        key: gate.get(key)
        for key in ("gate_id", "gate_kind", "target_kind", "target_key", "verdict", "created_at")
        if gate.get(key) not in (None, "", [])
    }


def _gate_spec_from_name(
    name: str,
    policy: dict[str, Any],
    *,
    index: int,
    registry: GateDefinitionRegistry,
) -> GateSpec | None:
    gate_name = str(name or "").strip()
    if not gate_name or gate_name == NONE_GATE:
        return None
    definition = registry.get(gate_name)
    if definition is None:
        return None
    return _gate_spec_from_definition(definition, {}, policy, index=index, registry=registry)


def _gate_spec_from_dict(
    raw: dict[str, Any],
    policy: dict[str, Any],
    *,
    index: int,
    registry: GateDefinitionRegistry,
) -> GateSpec | None:
    data = dict(raw or {})
    gate_name = _gate_name_from_entry(data, registry)
    if gate_name == NONE_GATE:
        return None
    definition = registry.get(gate_name) if gate_name else None
    if definition is not None:
        return _gate_spec_from_definition(definition, data, policy, index=index, registry=registry)
    return _gate_spec_from_expanded_dict(data, policy, index=index)


def _gate_spec_from_definition(
    definition: GateDefinition,
    raw: dict[str, Any],
    policy: dict[str, Any],
    *,
    index: int,
    registry: GateDefinitionRegistry,
) -> GateSpec:
    data = {**definition.to_dict(), **dict(raw or {})}
    data_policy = dict(definition.policy)
    data_policy.update(dict(data.get("policy") or {}))
    data["policy"] = data_policy
    refs = data.get("required_check_refs") or definition.required_check_refs
    data["required_checks"] = list(
        registry.checklist_entries.resolve_texts(refs, fallback=data.get("required_checks") or definition.required_checks)
    )
    return _gate_spec_from_expanded_dict(data, policy, index=index)


def _gate_spec_from_expanded_dict(raw: dict[str, Any], policy: dict[str, Any], *, index: int) -> GateSpec:
    data = dict(raw or {})
    gate_name = str(data.get("gate") or data.get("name") or data.get("gate_name") or data.get("id") or "").strip()
    trigger = _normalize_trigger(data.get("trigger") or data.get("when") or GATE_TRIGGER_AFTER_EACH_MILESTONE)
    target_kind = str(data.get("target_kind") or data.get("target") or "").strip()
    strategy = str(data.get("strategy") or REVIEWER_GATE_STRATEGY).strip() or REVIEWER_GATE_STRATEGY
    gate_id = str(data.get("id") or data.get("gate_id") or gate_name or target_kind or f"gate_{index + 1}").strip()
    if not gate_name:
        gate_name = gate_id
    if not target_kind:
        target_kind = _default_target_for_gate(gate_name)
    gate_kind = str(data.get("gate_kind") or _default_gate_kind_for_gate(gate_name)).strip()
    reviewer_group, reviewer_name = _profile_parts(
        data.get("reviewer_profile") or policy.get("reviewer_profile"),
        group=data.get("reviewer_profile_group") or policy.get("reviewer_profile_group"),
        name=data.get("reviewer_profile_name") or policy.get("reviewer_profile_name"),
    )
    fallback_group, fallback_name = _profile_parts(
        data.get("fallback_reviewer_profile") or policy.get("fallback_reviewer_profile"),
        group=data.get("fallback_reviewer_profile_group") or policy.get("fallback_reviewer_profile_group"),
        name=data.get("fallback_reviewer_profile_name") or policy.get("fallback_reviewer_profile_name"),
    )
    merged_policy = {**policy, **data}
    return GateSpec(
        gate_id=gate_id,
        gate=gate_name,
        trigger=trigger,
        strategy=strategy,
        target_kind=target_kind,
        gate_kind=gate_kind,
        reviewer_profile_group=reviewer_group,
        reviewer_profile_name=reviewer_name,
        fallback_reviewer_profile_group=fallback_group,
        fallback_reviewer_profile_name=fallback_name,
        max_repair_attempts=_bounded_int(_merged_value(data, policy, "max_repair_attempts"), default=5),
        max_revision_attempts=_bounded_int(_merged_value(data, policy, "max_revision_attempts"), default=2),
        auto_revise=coerce_bool(data.get("auto_revise") if "auto_revise" in data else policy.get("auto_revise")),
        required_checks=tuple(_string_list(data.get("required_checks") or policy.get("required_checks"))),
        blocking=tuple(_string_list(data.get("blocking") or policy.get("blocking"))),
        policy=merged_policy,
    )


def _dedupe_gate_specs(specs: list[GateSpec]) -> list[GateSpec]:
    result: list[GateSpec] = []
    seen: set[tuple[str, str, str]] = set()
    for spec in specs:
        key = (spec.trigger, spec.strategy, spec.gate_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(spec)
    return result


def _merged_value(data: dict[str, Any], policy: dict[str, Any], key: str) -> Any:
    return data[key] if key in data else policy.get(key)


def _gate_entry_is_none(raw: dict[str, Any]) -> bool:
    data = dict(raw or {})
    return any(str(data.get(key) or "").strip().lower() == NONE_GATE for key in ("gate", "name", "strategy", "id"))


def _gate_name_from_entry(data: dict[str, Any], registry: GateDefinitionRegistry) -> str:
    explicit = str(data.get("gate") or data.get("name") or data.get("gate_name") or "").strip()
    if explicit:
        return explicit
    gate_id = str(data.get("id") or data.get("gate_id") or "").strip()
    if gate_id and registry.get(gate_id) is not None:
        return gate_id
    legacy_strategy = str(data.get("strategy") or "").strip()
    if legacy_strategy == "plan_artifact_acceptance":
        return PLAN_ACCEPTANCE_GATE
    if legacy_strategy and registry.get(legacy_strategy) is not None:
        return legacy_strategy
    return ""


def _repair_todo_items(gate: dict[str, Any], acceptance: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings = [dict(item) for item in list(gate.get("findings") or []) if isinstance(item, dict)]
    required = [dict(item) for item in list(gate.get("required_fixes") or []) if isinstance(item, dict)]
    items: list[dict[str, Any]] = []
    source_items = required or [
        item for item in findings if _finding_requires_repair(item)
    ]
    for index, item in enumerate(source_items[:12], start=1):
        finding = _linked_finding(item, findings)
        source = "required_fix" if required else "finding"
        action = _first_text(item, "description", "summary", "suggested_fix", "fix", "message", fallback="")
        if not action:
            action = _first_text(finding, "suggested_fix", "fix", "title", "summary", "message", fallback="Address reviewer finding")
        acceptance_refs = repair_acceptance_refs({**finding, **item}, acceptance)
        if not acceptance_refs and len(acceptance) == 1:
            only_ref = str(acceptance[0].get("id") or "").strip()
            if only_ref:
                acceptance_refs = [only_ref]
        items.append(
            _drop_empty(
                {
                    "id": f"RC-{index}",
                    "kind": "repair",
                    "status": "pending",
                    "source_kind": source,
                    "source_ref": str(item.get("id") or item.get("finding_index") or index),
                    "parent_item_id": acceptance_refs[0] if acceptance_refs else "",
                    "action": _clip(action, 420),
                    "source_text": _clip(action, 420),
                    "metadata": _drop_empty(
                        {
                            "acceptance_refs": acceptance_refs,
                            "area": _clip(_first_text(item, "area", "path", fallback=_first_text(finding, "area", "path", fallback="")), 220),
                            "contract_impact": _clip(
                                _first_text(item, "contract_impact", fallback=_first_text(finding, "contract_impact", fallback="")),
                                260,
                            ),
                            "verify": _clip(
                                _first_text(item, "verification", "verify", "test", fallback="Run focused verification for this repair."),
                                260,
                            ),
                        }
                    ),
                }
            )
        )
    return items


def _acceptance_todo_items(acceptance: list[dict[str, Any]], failed_refs: list[str]) -> list[dict[str, Any]]:
    if not acceptance:
        return []
    failed = {str(item or "").strip() for item in failed_refs}
    items = []
    for item in acceptance:
        item_id = str(item.get("id") or "").strip()
        status = "failed" if item_id in failed else "pending"
        items.append({**dict(item), "status": status})
    return compact_checklist(items)


def _failed_acceptance_refs(gate: dict[str, Any], acceptance: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for finding in list(gate.get("findings") or []):
        if isinstance(finding, dict):
            refs.extend(repair_acceptance_refs(finding, acceptance))
    for fix in list(gate.get("required_fixes") or []):
        if isinstance(fix, dict):
            refs.extend(repair_acceptance_refs(fix, acceptance))
    result: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        if not ref or ref in seen:
            continue
        seen.add(ref)
        result.append(ref)
    return result


def _finding_requires_repair(item: dict[str, Any]) -> bool:
    if _first_text(item, "suggested_fix", "fix", fallback=""):
        return True
    severity = str(item.get("severity") or "").strip().lower()
    if severity in {"blocker", "critical", "major", "high", "error", "fail", "failure"}:
        return True
    impact = str(item.get("contract_impact") or "").strip().lower().replace("_", " ")
    return bool(impact and impact not in {"none", "no impact", "not applicable", "n/a", "na"})


def _linked_finding(item: dict[str, Any], findings: list[dict[str, Any]]) -> dict[str, Any]:
    raw = item.get("finding_index")
    if isinstance(raw, int) and 0 <= raw < len(findings):
        return findings[raw]
    if isinstance(raw, str) and raw.strip().isdigit():
        index = int(raw.strip())
        if 0 <= index < len(findings):
            return findings[index]
    return findings[0] if len(findings) == 1 else {}


def _builtin_gate_checklist_entries() -> tuple[GateChecklistEntry, ...]:
    return (
        GateChecklistEntry(
            "source_contract.compile",
            "Compile the source task into indexed gate_contract checks before planner work starts.",
        ),
        GateChecklistEntry(
            "source_contract.mechanical",
            "Use mechanical predicates only for finite counts or bounds Pal can verify.",
        ),
        GateChecklistEntry(
            "source_contract.submit",
            "Submit op_minion_gate_contract_submit with the compiled gate_contract.",
        ),
        GateChecklistEntry(
            "checkpoint.contract_match",
            "Verify the checkpoint matches the milestone contract.",
        ),
        GateChecklistEntry(
            "checkpoint.deliverable_complete",
            "Verify the checkpoint has a structured commit/report, changed-file evidence, command/test evidence, and no owned-area or workspace-policy violation before allowing module-local progress.",
        ),
        GateChecklistEntry(
            "checkpoint.lsp_or_equivalent_clean",
            "Verify prepared LSP/type/build diagnostics are clean for touched code, or record the concrete unavailable/not-applicable reason and equivalent focused evidence.",
        ),
        GateChecklistEntry(
            "checkpoint.relevant_tests",
            "Run or inspect relevant tests when possible.",
        ),
        GateChecklistEntry(
            "checkpoint.delivery_entrypoint",
            "Verify declared user/downstream delivery surfaces through the real invocation path, or the smallest faithful wrapper/link/import/launch probe; internal helper calls do not satisfy wrapper, manifest, public API, service/UI, plugin, command, or persisted-format contracts.",
        ),
        GateChecklistEntry(
            "checkpoint.api_evidence",
            "Verify claimed APIs with source, LSP, docs, build, or explicit not-verified findings.",
        ),
        GateChecklistEntry(
            "checkpoint.submit_gate",
            "Submit op_minion_review_checkpoint with evidence covering each acceptance criterion.",
        ),
        GateChecklistEntry(
            "module.contract_semantics",
            "Review the completed module against its module-level contract, public interfaces, cross-module handoffs, type/schema guarantees, lifecycle/ownership semantics, and error behavior.",
        ),
        GateChecklistEntry(
            "module.corner_cases",
            "Verify boundary, negative, empty/default, invalid-input, timeout/cancellation/retry, persistence, and compatibility cases that are relevant to the module contract.",
        ),
        GateChecklistEntry(
            "module.delivery_dogfood",
            "Dogfood exact declared delivery surfaces or downstream consumer paths when the module exposes CLI commands, public APIs, service/UI routes, plugin hooks, command wrappers, or persisted formats.",
        ),
        GateChecklistEntry(
            "module.downstream_ready",
            "Verify downstream modules can safely depend on only declared public interfaces/facades/contracts, with no sibling-internal imports or undeclared flywire coupling.",
        ),
        GateChecklistEntry(
            "module.no_cross_module_source_copy",
            "Verify changed files do not duplicate dependency module source/contracts/DTOs/protocols/schemas/headers/facades under a new path; consumers must import/include declared public contracts instead.",
        ),
        GateChecklistEntry(
            "plan.requirements_coverage",
            "Verify every hard user/work-order REQ or source gate_contract item is preserved without weakening, covered by plan constraints/modules/milestones/acceptance criteria, and not expanded into new functional scope without a user-owned decision.",
        ),
        GateChecklistEntry(
            "plan.dispatchable",
            "Verify the plan is dispatchable and topology/module ordering is valid.",
        ),
        GateChecklistEntry(
            "plan.boundary_import_contracts",
            "Verify every shared contract/stub/facade has one producer-owned source_path plus import_path/include/public_entrypoint, downstream modules consume that interface, and no module owns a private copy of another module's contract file.",
        ),
        GateChecklistEntry(
            "plan.source_evidence",
            "Verify referenced files, modules, and claimed APIs with source, LSP, docs, build, or explicit not-applicable evidence.",
        ),
        GateChecklistEntry(
            "plan.test_strategy",
            "Verify the test strategy is executable for the repo and each milestone has concrete acceptance criteria.",
        ),
        GateChecklistEntry(
            "plan.delivery_strategy",
            "Verify the plan includes acceptance criteria for exact user/downstream delivery surfaces when the task exposes CLI commands, package entrypoints, public APIs, services, UI flows, plugin hooks, command wrappers, or persisted formats.",
        ),
        GateChecklistEntry(
            "plan.submit_gate",
            "Submit op_minion_review_gate_submit with gate_kind=plan_acceptance and target.plan_ref.",
        ),
    )


def _builtin_gate_definitions() -> tuple[GateDefinition, ...]:
    return (
        GateDefinition(
            name=SOURCE_CONTRACT_GATE,
            target_kind="work_order",
            gate_kind="source_contract",
            strategy=REVIEWER_GATE_STRATEGY,
            trigger=GATE_TRIGGER_BEFORE_PLAN,
            reviewer_profile_group="software_engineering",
            reviewer_profile_name="reviewer",
            required_check_refs=(
                "source_contract.compile",
                "source_contract.mechanical",
                "source_contract.submit",
            ),
            blocking=(
                "missing_hard_requirement",
                "invalid_mechanical_check",
                "unclear_source_contract",
            ),
        ),
        GateDefinition(
            name=CHECKPOINT_ADMISSION_GATE,
            target_kind="checkpoint",
            gate_kind="checkpoint_admission",
            strategy=NONE_GATE_STRATEGY,
            max_repair_attempts=0,
            required_check_refs=(
                "checkpoint.deliverable_complete",
                "checkpoint.lsp_or_equivalent_clean",
                "checkpoint.relevant_tests",
            ),
            blocking=(
                "missing_checkpoint_commit",
                "missing_checkpoint_report",
                "missing_evidence",
                "lsp_or_type_diagnostics_failed",
                "owned_area_violation",
                "workspace_policy_violation",
            ),
            policy={
                "reviewer_required": False,
                "scope": "checkpoint_admission",
            },
        ),
        GateDefinition(
            name=MODULE_QUALITY_GATE,
            target_kind="checkpoint",
            gate_kind="checkpoint_verification",
            strategy=REVIEWER_GATE_STRATEGY,
            max_repair_attempts=5,
            required_check_refs=(
                "checkpoint.relevant_tests",
                "module.contract_semantics",
                "module.corner_cases",
                "checkpoint.delivery_entrypoint",
                "module.delivery_dogfood",
                "checkpoint.api_evidence",
                "module.downstream_ready",
                "module.no_cross_module_source_copy",
                "checkpoint.submit_gate",
            ),
            blocking=(
                "contract_mismatch",
                "missing_required_test",
                "missing_delivery_entrypoint_verification",
                "unimplemented_public_api",
                "scope_violation",
                "unsafe_or_unclear_module_boundary",
                "undeclared_cross_module_import",
                "copied_cross_module_contract",
            ),
            policy={
                "require_test_or_blocker": True,
                "require_api_evidence": True,
                "require_lsp_when_applicable": True,
                "check_public_declarations_have_implementation": True,
                "scope": "module_quality",
                "terminal_module_only": True,
            },
        ),
        GateDefinition(
            name=CHECKPOINT_QUALITY_GATE,
            target_kind="checkpoint",
            gate_kind="checkpoint_verification",
            strategy=REVIEWER_GATE_STRATEGY,
            max_repair_attempts=5,
            required_check_refs=(
                "checkpoint.contract_match",
                "checkpoint.relevant_tests",
                "checkpoint.delivery_entrypoint",
                "checkpoint.api_evidence",
                "checkpoint.submit_gate",
            ),
            blocking=(
                "contract_mismatch",
                "missing_required_test",
                "missing_delivery_entrypoint_verification",
                "unimplemented_public_api",
                "scope_violation",
            ),
            policy={
                "require_test_or_blocker": True,
                "require_api_evidence": True,
                "require_lsp_when_applicable": True,
                "check_public_declarations_have_implementation": True,
                "scope": "checkpoint_quality_legacy",
            },
        ),
        GateDefinition(
            name=PLAN_ACCEPTANCE_GATE,
            target_kind="plan_artifact",
            gate_kind="plan_acceptance",
            strategy=REVIEWER_GATE_STRATEGY,
            auto_revise=True,
            max_revision_attempts=2,
            required_check_refs=(
                "plan.requirements_coverage",
                "plan.dispatchable",
                "plan.boundary_import_contracts",
                "plan.source_evidence",
                "plan.test_strategy",
                "plan.delivery_strategy",
                "plan.submit_gate",
            ),
            blocking=(
                "schema_invalid",
                "undispatchable_plan",
                "missing_or_weakened_requirement_coverage",
                "missing_acceptance_criteria",
                "missing_user_entrypoint_dogfood",
                "unsafe_or_unclear_module_boundary",
                "missing_shared_import_contract",
                "copied_cross_module_contract",
            ),
        ),
    )


def _checklist_entry_from_dict(data: dict[str, Any]) -> GateChecklistEntry:
    entry_id = str(data.get("id") or data.get("entry_id") or data.get("name") or "").strip()
    return GateChecklistEntry(
        entry_id=entry_id,
        text=str(data.get("text") or data.get("summary") or data.get("description") or "").strip(),
        kind=str(data.get("kind") or "required_check").strip() or "required_check",
        metadata=dict(data.get("metadata") or {}),
    )


def _gate_definition_from_dict(data: dict[str, Any]) -> GateDefinition:
    name = str(data.get("gate") or data.get("name") or data.get("id") or "").strip()
    reviewer_group, reviewer_name = _profile_parts(
        data.get("reviewer_profile"),
        group=data.get("reviewer_profile_group"),
        name=data.get("reviewer_profile_name"),
    )
    fallback_group, fallback_name = _profile_parts(
        data.get("fallback_reviewer_profile"),
        group=data.get("fallback_reviewer_profile_group"),
        name=data.get("fallback_reviewer_profile_name"),
    )
    return GateDefinition(
        name=name,
        target_kind=str(data.get("target_kind") or _default_target_for_gate(name)).strip(),
        gate_kind=str(data.get("gate_kind") or _default_gate_kind_for_gate(name)).strip(),
        strategy=str(data.get("strategy") or REVIEWER_GATE_STRATEGY).strip() or REVIEWER_GATE_STRATEGY,
        trigger=_normalize_trigger(data.get("trigger") or data.get("when") or GATE_TRIGGER_AFTER_EACH_MILESTONE),
        reviewer_profile_group=reviewer_group,
        reviewer_profile_name=reviewer_name,
        fallback_reviewer_profile_group=fallback_group,
        fallback_reviewer_profile_name=fallback_name,
        max_repair_attempts=_bounded_int(data.get("max_repair_attempts"), default=0),
        max_revision_attempts=_bounded_int(data.get("max_revision_attempts"), default=0),
        auto_revise=coerce_bool(data.get("auto_revise")),
        required_check_refs=tuple(_string_list(data.get("required_check_refs"))),
        required_checks=tuple(_string_list(data.get("required_checks"))),
        blocking=tuple(_string_list(data.get("blocking"))),
        policy=dict(data.get("policy") or {}),
    )


def _default_strategy_for(gate_id: str, target_kind: str, trigger: str) -> str:
    text = " ".join([gate_id, target_kind, trigger]).strip().lower()
    if "checkpoint" in text:
        return REVIEWER_GATE_STRATEGY
    if "plan" in text:
        return REVIEWER_GATE_STRATEGY
    return REVIEWER_GATE_STRATEGY


def _default_target_for_gate(gate: str) -> str:
    if gate == SOURCE_CONTRACT_GATE:
        return "work_order"
    if gate in {CHECKPOINT_QUALITY_GATE, CHECKPOINT_ADMISSION_GATE, MODULE_QUALITY_GATE}:
        return "checkpoint"
    if gate == PLAN_ACCEPTANCE_GATE:
        return "plan_artifact"
    return "artifact"


def _default_gate_kind_for_gate(gate: str) -> str:
    if gate == SOURCE_CONTRACT_GATE:
        return "source_contract"
    if gate == CHECKPOINT_ADMISSION_GATE:
        return "checkpoint_admission"
    if gate == MODULE_QUALITY_GATE:
        return "checkpoint_verification"
    if gate == CHECKPOINT_QUALITY_GATE:
        return "checkpoint_verification"
    if gate == PLAN_ACCEPTANCE_GATE:
        return "plan_acceptance"
    return str(gate or "review_gate").strip()


def _normalize_trigger(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"before_plan", "pre_plan", "plan_preflight", "before_planning"}:
        return GATE_TRIGGER_BEFORE_PLAN
    if text in {"after_milestone", "milestone", "checkpoint", "after_each_checkpoint"}:
        return GATE_TRIGGER_AFTER_EACH_MILESTONE
    if text in {"terminal", "final", "completion", "after_completion"}:
        return GATE_TRIGGER_TERMINAL
    return text or GATE_TRIGGER_AFTER_EACH_MILESTONE


def _profile_parts(value: Any, *, group: Any = "", name: Any = "") -> tuple[str, str]:
    explicit_group = str(group or "").strip()
    explicit_name = str(name or "").strip()
    if explicit_group or explicit_name:
        return explicit_group, explicit_name
    text = str(value or "").strip()
    if "." in text:
        left, right = text.rsplit(".", 1)
        return left.strip(), right.strip()
    return "", text


def _profile_ref(group: str, name: str) -> str:
    if group and name:
        return f"{group}.{name}"
    return name or group


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item or "").strip()]
    return [str(value).strip()] if str(value or "").strip() else []


def _bounded_int(value: Any, *, default: int) -> int:
    return max(0, min(20, coerce_int(value, default)))


def _first_text(payload: dict[str, Any], *keys: str, fallback: str = "") -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (dict, list, tuple, set)):
            continue
        text = str(value or "").strip()
        if text:
            return text
    return fallback


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)].rstrip()}..."


def _drop_empty(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}
