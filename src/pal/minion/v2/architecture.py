from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import re
from typing import Any, Mapping
from datetime import datetime, timezone

from pal.minion.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.minion.v2.contracts import ActionEnvelope, AggregateType, DispatchResult
from pal.minion.v2.repository import MinionV2Repository


class ResearchMode(StrEnum):
    NONE = "none"
    LOCAL_ONLY = "local_only"
    EXTERNAL_ALLOWED = "external_allowed"


EVIDENCE_SOURCE_KINDS = frozenset(
    {"local", "external", "approved", "user_supplied", "input_artifact", "verification", "review"}
)


class RequirementStrength(StrEnum):
    HARD = "hard"
    SOFT = "soft"


class UnitBehaviorKind(StrEnum):
    STATELESS = "stateless"
    RESOURCE_OWNER = "resource_owner"
    SERVICE = "service"
    WORKFLOW = "workflow"
    ADAPTER = "adapter"


class ArchitectureFindingKind(StrEnum):
    REQUIREMENTS_DEFECT = "requirements_defect"
    CONTRACT_DEFECT = "contract_defect"
    ARCHITECTURE_DEFECT = "architecture_defect"


REVISION_TARGET_SECTIONS = frozenset(
    {
        "requirements",
        "constraint",
        "design_decision",
        "gate_check",
        "unit",
        "cross_unit_contract",
        "topology",
        "integration_contract",
        "assumption_ledger",
        "risk_ledger",
    }
)
REVISION_TARGET_OPERATIONS = frozenset({"create", "update", "delete"})


@dataclass(frozen=True)
class ArchitectureRevisionTarget:
    """A stable semantic location that a revision may change.

    The target deliberately contains no artifact handle or JSON pointer. IDs and
    field names survive content-addressed artifact replacement; the manager
    resolves them against the current manifest before binding the revision.
    """

    section: str
    target_id: str
    fields: tuple[str, ...] = ()
    operation: str = "update"

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "id": self.target_id,
            "fields": list(self.fields),
            "operation": self.operation,
        }


def normalize_revision_targets(values: Any) -> tuple[ArchitectureRevisionTarget, ...]:
    """Validate and deduplicate stable revision targets in input order."""

    result: list[ArchitectureRevisionTarget] = []
    seen: set[tuple[str, str, tuple[str, ...], str]] = set()
    for raw in list(values or []):
        if not isinstance(raw, Mapping):
            raise ValueError("revision target must be an object")
        section = str(raw.get("section") or "").strip()
        target_id = str(raw.get("id") or "").strip()
        operation = str(raw.get("operation") or "update").strip()
        fields = tuple(dict.fromkeys(str(item).strip() for item in list(raw.get("fields") or []) if str(item).strip()))
        if section not in REVISION_TARGET_SECTIONS:
            raise ValueError(f"invalid revision target section: {section or '<empty>'}")
        if not target_id:
            raise ValueError(f"revision target {section} requires a stable id")
        if operation not in REVISION_TARGET_OPERATIONS:
            raise ValueError(f"invalid revision target operation: {operation}")
        target = ArchitectureRevisionTarget(section, target_id, fields, operation)
        key = (target.section, target.target_id, target.fields, target.operation)
        if key not in seen:
            result.append(target)
            seen.add(key)
    return tuple(result)


def contract_revision_changes(
    base: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> tuple[ArchitectureRevisionTarget, ...]:
    """Return semantic CRUD changes between two Contract Builder payloads."""

    changes: list[ArchitectureRevisionTarget] = []

    def stable_collection(section: str, field_name: str, id_field: str = "id") -> None:
        before = {
            str(dict(item or {}).get(id_field) or ""): dict(item or {})
            for item in list(base.get(field_name) or [])
            if str(dict(item or {}).get(id_field) or "")
        }
        after = {
            str(dict(item or {}).get(id_field) or ""): dict(item or {})
            for item in list(candidate.get(field_name) or [])
            if str(dict(item or {}).get(id_field) or "")
        }
        for item_id in sorted(set(before) | set(after)):
            old = before.get(item_id)
            new = after.get(item_id)
            if old is None:
                changes.append(ArchitectureRevisionTarget(section, item_id, tuple(sorted(new or {})), "create"))
            elif new is None:
                changes.append(ArchitectureRevisionTarget(section, item_id, (), "delete"))
            elif old != new:
                fields = tuple(sorted(key for key in set(old) | set(new) if old.get(key) != new.get(key)))
                changes.append(ArchitectureRevisionTarget(section, item_id, fields, "update"))

    stable_collection("constraint", "global_constraints")
    stable_collection("design_decision", "design_decisions")
    stable_collection("gate_check", "gate_checks")
    stable_collection("unit", "units", "unit_id")
    stable_collection("cross_unit_contract", "cross_unit_contracts")

    before_dependencies = dict(dict(base.get("topology") or {}).get("depends_on") or {})
    after_dependencies = dict(dict(candidate.get("topology") or {}).get("depends_on") or {})
    for unit_id in sorted(set(before_dependencies) | set(after_dependencies)):
        old = before_dependencies.get(unit_id)
        new = after_dependencies.get(unit_id)
        if old is None:
            changes.append(ArchitectureRevisionTarget("topology", str(unit_id), ("depends_on",), "create"))
        elif new is None:
            changes.append(ArchitectureRevisionTarget("topology", str(unit_id), (), "delete"))
        elif old != new:
            changes.append(ArchitectureRevisionTarget("topology", str(unit_id), ("depends_on",), "update"))

    for section, field_name, target_id in (
        ("integration_contract", "integration_contract", "integration"),
        ("assumption_ledger", "assumption_ledger", "assumptions"),
        ("risk_ledger", "risk_ledger", "risks"),
    ):
        old = dict(base.get(field_name) or {})
        new = dict(candidate.get(field_name) or {})
        if old != new:
            fields = tuple(sorted(key for key in set(old) | set(new) if old.get(key) != new.get(key)))
            changes.append(ArchitectureRevisionTarget(section, target_id, fields, "update"))
    return tuple(changes)


def revision_target_allows(
    target: ArchitectureRevisionTarget,
    allowed: tuple[ArchitectureRevisionTarget, ...],
) -> bool:
    """Whether an actual semantic change is inside a manager-issued scope."""

    for scope in allowed:
        if (scope.section, scope.target_id, scope.operation) != (target.section, target.target_id, target.operation):
            continue
        if not scope.fields or set(target.fields).issubset(scope.fields):
            return True
    return False


@dataclass(frozen=True)
class ComplexityBudgetPolicy:
    target_file_count: int = 12
    estimated_context_tokens: int = 65536
    public_interface_count: int = 16
    cross_unit_contract_count: int = 12
    stateful_resource_count: int = 2
    expected_candidate_cycles: int = 3
    platform_dependency_level: int = 3

    def violations(self, budget: Mapping[str, Any]) -> tuple[str, ...]:
        violations: list[str] = []
        for field_name in (
            "target_file_count",
            "estimated_context_tokens",
            "public_interface_count",
            "cross_unit_contract_count",
            "stateful_resource_count",
            "expected_candidate_cycles",
            "platform_dependency_level",
        ):
            actual = _non_negative_int(budget.get(field_name), field_name)
            maximum = int(getattr(self, field_name))
            if actual > maximum:
                violations.append(f"{field_name}={actual} exceeds policy maximum {maximum}")
        return tuple(violations)


@dataclass(frozen=True)
class ArchitectureFinding:
    finding_kind: ArchitectureFindingKind
    summary: str
    refs: tuple[str, ...] = ()
    severity: str = "error"
    revision_targets: tuple[ArchitectureRevisionTarget, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_kind": self.finding_kind.value,
            "summary": self.summary,
            "refs": list(self.refs),
            "severity": self.severity,
            "revision_targets": [item.to_dict() for item in self.revision_targets],
        }


@dataclass(frozen=True)
class ArchitectureReviewResult:
    verdict: str
    findings: tuple[ArchitectureFinding, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "findings": [item.to_dict() for item in self.findings]}


@dataclass(frozen=True)
class HumanReviewCard:
    workflow_id: str
    architecture_revision_id: str
    manifest_sha: str
    actor_id: str
    active_channel_id: str
    decision_token: str
    markdown: str
    actions: tuple[str, ...] = ("accept", "edit", "reject")


@dataclass
class ArchitectureArtifactService:
    artifacts: ContentAddressedArtifactStore
    repository: MinionV2Repository
    complexity_policy: ComplexityBudgetPolicy = field(default_factory=ComplexityBudgetPolicy)

    def publish_requirements(
        self,
        payload: Mapping[str, Any],
        *,
        provenance: Mapping[str, Any] | None = None,
    ) -> ArtifactRef:
        normalized = validate_requirements_artifact(payload)
        return self.artifacts.put_json(
            normalized,
            artifact_type="RequirementsArtifact",
            provenance=provenance,
        )

    def publish_evidence_catalog(
        self,
        payload: Mapping[str, Any],
        *,
        requirements_ref: ArtifactRef,
        research_mode: ResearchMode,
        provenance: Mapping[str, Any] | None = None,
    ) -> ArtifactRef:
        normalized = validate_evidence_catalog(payload, research_mode=research_mode)
        requirements = validate_requirements_artifact(self.artifacts.read_json(requirements_ref))
        _validate_evidence_requirement_coverage(requirements, normalized)
        normalized["requirements_ref"] = requirements_ref.to_dict()
        return self.artifacts.put_json(
            normalized,
            artifact_type="EvidenceCatalogArtifact",
            provenance=provenance,
            child_refs=((requirements_ref.sha256, "requirements"),),
        )

    def validate_evidence_coverage(
        self,
        *,
        requirements_ref: ArtifactRef | Mapping[str, Any],
        evidence_ref: ArtifactRef | Mapping[str, Any],
    ) -> None:
        requirements = validate_requirements_artifact(self.artifacts.read_json(requirements_ref))
        evidence_payload = dict(self.artifacts.read_json(evidence_ref))
        evidence = validate_evidence_catalog(
            evidence_payload,
            research_mode=ResearchMode(str(evidence_payload.get("research_mode") or "local_only")),
        )
        _validate_evidence_requirement_coverage(requirements, evidence)

    def publish_unit_contract(
        self,
        payload: Mapping[str, Any],
        *,
        provenance: Mapping[str, Any] | None = None,
    ) -> ArtifactRef:
        normalized = validate_unit_contract(payload, complexity_policy=self.complexity_policy)
        return self.artifacts.put_json(
            normalized,
            artifact_type="UnitContractArtifact",
            provenance=provenance,
        )

    def publish_fragment(
        self,
        payload: Mapping[str, Any] | list[Any],
        *,
        artifact_type: str,
        provenance: Mapping[str, Any] | None = None,
        child_refs: tuple[tuple[str, str], ...] = (),
    ) -> ArtifactRef:
        return self.artifacts.put_json(
            payload,
            artifact_type=artifact_type,
            provenance=provenance,
            child_refs=child_refs,
        )

    def publish_manifest(
        self,
        payload: Mapping[str, Any],
        *,
        provenance: Mapping[str, Any] | None = None,
    ) -> ArtifactRef:
        normalized = validate_architecture_manifest(payload)
        child_refs = tuple(
            (digest, relation)
            for relation, digest in architecture_manifest_child_refs(normalized)
        )
        return self.artifacts.put_json(
            normalized,
            artifact_type="ArchitectureContractArtifact",
            provenance=provenance,
            child_refs=child_refs,
        )

    def review_manifest(self, manifest_ref: ArtifactRef | Mapping[str, Any]) -> ArchitectureReviewResult:
        manifest = validate_architecture_manifest(self.artifacts.read_json(manifest_ref))
        fragments = self.load_manifest_fragments(manifest)
        return review_architecture_contract(
            manifest,
            fragments,
            complexity_policy=self.complexity_policy,
        )

    def load_manifest_fragments(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for field_name, value in manifest.items():
            if field_name.endswith("_ref") and isinstance(value, Mapping) and value.get("sha256"):
                result[field_name.removesuffix("_ref")] = self.artifacts.read_json(value)
            elif field_name.endswith("_refs") and isinstance(value, list):
                result[field_name.removesuffix("_refs")] = [
                    self.artifacts.read_json(item)
                    for item in value
                    if isinstance(item, Mapping) and item.get("sha256")
                ]
        return result

    def compile_human_review_markdown(self, manifest_ref: ArtifactRef | Mapping[str, Any]) -> str:
        manifest = validate_architecture_manifest(self.artifacts.read_json(manifest_ref))
        fragments = self.load_manifest_fragments(manifest)
        return compile_architecture_markdown(manifest, fragments)

    def create_human_review_card(
        self,
        *,
        workflow_id: str,
        architecture_revision_id: str,
        manifest_ref: ArtifactRef,
        actor_id: str,
        active_channel_id: str,
        ttl_seconds: int = 86400,
    ) -> HumanReviewCard:
        markdown = self.compile_human_review_markdown(manifest_ref)
        token = self.repository.issue_human_decision_token(
            workflow_id=workflow_id,
            architecture_revision_id=architecture_revision_id,
            manifest_sha=manifest_ref.sha256,
            actor_id=actor_id,
            active_channel_id=active_channel_id,
            ttl_seconds=ttl_seconds,
        )
        return HumanReviewCard(
            workflow_id=workflow_id,
            architecture_revision_id=architecture_revision_id,
            manifest_sha=manifest_ref.sha256,
            actor_id=actor_id,
            active_channel_id=active_channel_id,
            decision_token=token,
            markdown=markdown,
        )

    def submit_human_decision(
        self,
        card: HumanReviewCard,
        *,
        decision: str,
        edit_instruction: str = "",
    ) -> DispatchResult:
        normalized = str(decision).strip().lower()
        action_types = {
            "accept": "HUMAN_ACCEPT",
            "edit": "HUMAN_EDIT",
            "reject": "HUMAN_REJECT",
        }
        if normalized not in action_types:
            raise ValueError("decision must be accept, edit, or reject")
        token_record = self.repository.inspect_human_decision_token(card.decision_token)
        if token_record is None:
            raise ValueError("unknown human decision token")
        if str(token_record.get("status") or "") != "issued":
            raise ValueError("human decision token is stale or already consumed")
        manifest_record = self.repository.read_artifact_record(card.manifest_sha)
        if manifest_record is None:
            raise ValueError("architecture manifest is no longer available")
        manifest_ref = ArtifactRef(
            sha256=card.manifest_sha,
            artifact_type=str(manifest_record["artifact_type"]),
            schema_version=str(manifest_record["schema_version"]),
            media_type=str(manifest_record["media_type"]),
            byte_size=int(manifest_record["byte_size"]),
            durable=True,
        )
        payload: dict[str, Any] = {
            "decision_token": card.decision_token,
            "architecture_manifest_ref": manifest_ref.to_dict(),
        }
        if normalized == "edit":
            if not edit_instruction.strip():
                raise ValueError("edit decision requires edit_instruction")
            instruction_ref = self.artifacts.put_json(
                {"instruction": edit_instruction.strip(), "manifest_sha": card.manifest_sha},
                artifact_type="ArchitectureEditInstructionArtifact",
                provenance={"actor_id": card.actor_id, "channel_id": card.active_channel_id},
                child_refs=((card.manifest_sha, "revises"),),
            )
            payload["edit_instruction_ref"] = instruction_ref.to_dict()
        snapshot = self.repository.read_snapshot(
            AggregateType.ARCHITECTURE_REVISION,
            card.architecture_revision_id,
        )
        if snapshot is None:
            raise ValueError("architecture revision does not exist")
        return self.repository.dispatch(
            ActionEnvelope(
                action_type=action_types[normalized],
                workflow_id=card.workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=card.architecture_revision_id,
                actor=card.actor_id,
                source_channel=card.active_channel_id,
                expected_version=snapshot.version,
                idempotency_key=f"human-decision:{card.decision_token}",
                payload=payload,
            )
        )

    def publish_human_waiver(
        self,
        *,
        actor: str,
        workflow_id: str,
        architecture_revision_id: str,
        manifest_ref: ArtifactRef,
        finding_refs: list[str],
        reason: str,
        allowed_impact: str,
        scope: str,
        fragment_hashes: Mapping[str, str],
        expires_at: str = "",
    ) -> ArtifactRef:
        if not reason.strip() or not finding_refs or not fragment_hashes:
            raise ValueError("waiver requires reason, finding refs, and bound fragment hashes")
        payload = {
            "actor": actor,
            "workflow_id": workflow_id,
            "architecture_revision_id": architecture_revision_id,
            "manifest_sha": manifest_ref.sha256,
            "finding_refs": list(finding_refs),
            "reason": reason.strip(),
            "allowed_impact": allowed_impact.strip(),
            "scope": scope.strip(),
            "fragment_hashes": dict(fragment_hashes),
            "expires_at": expires_at,
        }
        return self.artifacts.put_json(
            payload,
            artifact_type="HumanWaiverArtifact",
            provenance={"actor": actor},
            child_refs=((manifest_ref.sha256, "waives"),),
        )

    def validate_human_waiver(
        self,
        waiver_ref: ArtifactRef | Mapping[str, Any],
        *,
        manifest_ref: ArtifactRef | Mapping[str, Any],
        fragment_hashes: Mapping[str, str],
    ) -> bool:
        waiver = dict(self.artifacts.read_json(waiver_ref))
        manifest_sha = (
            manifest_ref.sha256 if isinstance(manifest_ref, ArtifactRef) else str(manifest_ref.get("sha256") or "")
        )
        if str(waiver.get("manifest_sha") or "") != manifest_sha:
            return False
        bound = {str(key): str(value) for key, value in dict(waiver.get("fragment_hashes") or {}).items()}
        current = {str(key): str(value) for key, value in dict(fragment_hashes).items()}
        if not bound or any(current.get(key) != value for key, value in bound.items()):
            return False
        expires_at = str(waiver.get("expires_at") or "").strip()
        if expires_at:
            try:
                expiry = datetime.fromisoformat(expires_at)
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if expiry.astimezone(timezone.utc) <= datetime.now(timezone.utc):
                    return False
            except ValueError:
                return False
        return True


def validate_requirements_artifact(payload: Mapping[str, Any]) -> dict[str, Any]:
    requirements = list(payload.get("requirements") or [])
    if not requirements:
        raise ValueError("requirements artifact must contain at least one requirement")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in requirements:
        if not isinstance(item, Mapping):
            raise ValueError("requirement entries must be objects")
        requirement_id = str(item.get("requirement_id") or item.get("id") or "").strip()
        statement = str(item.get("statement") or item.get("text") or "").strip()
        strength = str(item.get("strength") or "hard").strip().lower()
        if not requirement_id or not statement:
            raise ValueError("each requirement needs requirement_id and statement")
        if requirement_id in seen:
            raise ValueError(f"duplicate requirement id: {requirement_id}")
        if strength not in {item.value for item in RequirementStrength}:
            raise ValueError(f"invalid requirement strength: {strength}")
        seen.add(requirement_id)
        normalized.append(
            {
                "requirement_id": requirement_id,
                "statement": statement,
                "strength": strength,
                "source_refs": _text_list(item.get("source_refs")),
                "acceptance_semantics": str(item.get("acceptance_semantics") or "").strip(),
                "ambiguities": _text_list(item.get("ambiguities")),
            }
        )
    return {
        "schema_version": "1",
        "requirements": normalized,
        "open_clarifications": list(payload.get("open_clarifications") or []),
        "source_coverage": list(payload.get("source_coverage") or []),
    }


def validate_evidence_catalog(payload: Mapping[str, Any], *, research_mode: ResearchMode) -> dict[str, Any]:
    evidence = list(payload.get("evidence") or [])
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, Mapping):
            raise ValueError("evidence entries must be objects")
        evidence_id = str(item.get("evidence_id") or item.get("id") or "").strip()
        location = str(item.get("location") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if not evidence_id or not location or not summary:
            raise ValueError("each evidence entry needs evidence_id, location, and summary")
        if evidence_id in seen:
            raise ValueError(f"duplicate evidence id: {evidence_id}")
        source_kind = str(item.get("source_kind") or "local").strip()
        if source_kind not in EVIDENCE_SOURCE_KINDS:
            raise ValueError(f"invalid evidence source_kind: {source_kind}")
        if research_mode == ResearchMode.LOCAL_ONLY and source_kind == "external":
            raise ValueError("research_mode=local_only cannot publish external evidence")
        line_start = _optional_non_negative_int(item.get("line_start"))
        line_end = _optional_non_negative_int(item.get("line_end"))
        content_sha = str(item.get("content_sha256") or "").strip()
        if content_sha and (len(content_sha) != 64 or any(character not in "0123456789abcdefABCDEF" for character in content_sha)):
            raise ValueError("evidence content_sha256 must be a 64-character hexadecimal digest")
        if (line_start is None) != (line_end is None):
            raise ValueError("evidence line_start and line_end must be provided together")
        if line_start is not None and line_end is not None and line_end < line_start:
            raise ValueError("evidence line_end must not precede line_start")
        if source_kind == "local" and line_start is None and not content_sha:
            raise ValueError("local evidence requires a precise line range or content_sha256")
        supports_requirement_ids = _text_list(item.get("supports_requirement_ids"))
        if not supports_requirement_ids:
            raise ValueError(f"evidence {evidence_id} must support at least one requirement")
        seen.add(evidence_id)
        normalized.append(
            {
                "evidence_id": evidence_id,
                "source_kind": source_kind,
                "location": location,
                "line_start": line_start,
                "line_end": line_end,
                "summary": summary,
                "supports_requirement_ids": supports_requirement_ids,
                "content_sha256": content_sha,
            }
        )
    if research_mode == ResearchMode.NONE and any(
        str(item.get("source_kind") or "") not in {"approved", "user_supplied", "input_artifact"}
        for item in normalized
    ):
        raise ValueError("research_mode=none may carry approved input evidence but cannot publish newly gathered evidence")
    return {"schema_version": "1", "research_mode": research_mode.value, "evidence": normalized}


def _validate_evidence_requirement_coverage(
    requirements: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> None:
    requirement_ids = {str(item["requirement_id"]) for item in list(requirements.get("requirements") or [])}
    supported_ids = {
        requirement_id
        for item in list(evidence.get("evidence") or [])
        for requirement_id in list(item.get("supports_requirement_ids") or [])
    }
    unknown_ids = supported_ids - requirement_ids
    if unknown_ids:
        raise ValueError("evidence references unknown requirements: " + ", ".join(sorted(unknown_ids)))
    missing_ids = requirement_ids - supported_ids
    if missing_ids:
        raise ValueError("requirements lack supporting evidence: " + ", ".join(sorted(missing_ids)))


def validate_unit_contract(
    payload: Mapping[str, Any],
    *,
    complexity_policy: ComplexityBudgetPolicy,
) -> dict[str, Any]:
    if "evidence_ids" in payload:
        raise ValueError("unit contracts must not contain architect evidence_ids")
    forbidden = {
        "milestones",
        "milestone",
        "test_matrix",
        "implementation_checklist",
        "implementation_steps",
        "function_steps",
    }
    present = sorted(forbidden & set(payload))
    if present:
        raise ValueError(f"unit contract contains implementation-level fields: {', '.join(present)}")
    unit_id = str(payload.get("unit_id") or "").strip()
    responsibility = str(payload.get("responsibility") or "").strip()
    behavior_kind = str(payload.get("unit_behavior_kind") or "").strip().lower()
    if not unit_id or not responsibility:
        raise ValueError("unit contract needs unit_id and responsibility")
    if behavior_kind not in {item.value for item in UnitBehaviorKind}:
        raise ValueError(f"invalid unit_behavior_kind: {behavior_kind or '<empty>'}")
    owned_area = _text_list(payload.get("owned_area"))
    if not owned_area:
        raise ValueError("unit contract needs at least one owned_area path")
    state_model = payload.get("state_model")
    lifecycle = payload.get("lifecycle")
    invariants = list(payload.get("invariants") or [])
    if behavior_kind == UnitBehaviorKind.STATELESS:
        state_text = str(state_model or "").strip().lower()
        if state_text not in {"stateless", "n/a", "none"}:
            raise ValueError("stateless module must declare state_model=stateless")
    else:
        if lifecycle in (None, "", {}, []) or state_model in (None, "", {}, []):
            raise ValueError("stateful module needs explicit lifecycle and state_model")
        if not invariants:
            raise ValueError("stateful module needs explicit invariants")
    budget = dict(payload.get("complexity_budget") or {})
    if not budget:
        raise ValueError("unit contract needs a structured complexity_budget")
    violations = complexity_policy.violations(budget)
    split_conditions = _text_list(payload.get("split_conditions"))
    waiver_ref = payload.get("complexity_waiver_ref")
    if violations and not (split_conditions or waiver_ref):
        raise ValueError("module exceeds complexity policy without split_conditions or waiver: " + "; ".join(violations))
    normalized = dict(payload)
    normalized.update(
        {
            "schema_version": "1",
            "unit_id": unit_id,
            "unit_behavior_kind": behavior_kind,
            "responsibility": responsibility,
            "owned_area": owned_area,
            "reference_only_paths": _text_list(payload.get("reference_only_paths")),
            "provided_interfaces": list(payload.get("provided_interfaces") or []),
            "consumed_interfaces": list(payload.get("consumed_interfaces") or []),
            "ownership": dict(payload.get("ownership") or {}),
            "lifecycle": lifecycle,
            "state_model": state_model,
            "invariants": invariants,
            "error_behavior": list(payload.get("error_behavior") or []),
            "compatibility": list(payload.get("compatibility") or []),
            "dependency_constraints": list(payload.get("dependency_constraints") or []),
            "requirement_ids": _text_list(payload.get("requirement_ids")),
            "verification_obligations": list(payload.get("verification_obligations") or []),
            "complexity_budget": budget,
            "split_conditions": split_conditions,
            "complexity_policy_violations": list(violations),
        }
    )
    return normalized


def validate_architecture_manifest(payload: Mapping[str, Any]) -> dict[str, Any]:
    if "evidence_catalog_ref" in payload:
        raise ValueError("architecture manifests must not contain an evidence catalog")
    required_single = (
        "requirements_ref",
        "global_constraints_ref",
        "design_decisions_ref",
        "gate_checks_ref",
        "topology_ref",
        "integration_contract_ref",
        "assumption_ledger_ref",
        "risk_ledger_ref",
    )
    missing = [field for field in required_single if not _is_artifact_ref(payload.get(field))]
    module_refs = list(payload.get("unit_contract_refs") or [])
    cross_refs = list(payload.get("cross_unit_contract_refs") or [])
    if not module_refs or any(not _is_artifact_ref(item) for item in module_refs):
        missing.append("unit_contract_refs")
    if any(not _is_artifact_ref(item) for item in cross_refs):
        missing.append("cross_unit_contract_refs")
    if missing:
        raise ValueError("architecture manifest missing valid artifact refs: " + ", ".join(sorted(set(missing))))
    normalized = dict(payload)
    normalized["schema_version"] = "1"
    normalized["unit_contract_refs"] = [dict(item) for item in module_refs]
    normalized["cross_unit_contract_refs"] = [dict(item) for item in cross_refs]
    for field in required_single:
        normalized[field] = dict(payload[field])
    return normalized


def architecture_manifest_child_refs(manifest: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    refs: list[tuple[str, str]] = []
    for field_name, value in manifest.items():
        if field_name.endswith("_ref") and _is_artifact_ref(value):
            refs.append((field_name.removesuffix("_ref"), str(value["sha256"])))
        elif field_name.endswith("_refs") and isinstance(value, list):
            relation = field_name.removesuffix("_refs")
            refs.extend(
                (f"{relation}[{index}]", str(item["sha256"]))
                for index, item in enumerate(value)
                if _is_artifact_ref(item)
            )
    return tuple(refs)


def review_architecture_contract(
    manifest: Mapping[str, Any],
    fragments: Mapping[str, Any],
    *,
    complexity_policy: ComplexityBudgetPolicy,
) -> ArchitectureReviewResult:
    _ = manifest
    findings: list[ArchitectureFinding] = []
    try:
        requirements = validate_requirements_artifact(dict(fragments.get("requirements") or {}))
    except ValueError as exc:
        findings.append(ArchitectureFinding(ArchitectureFindingKind.REQUIREMENTS_DEFECT, str(exc)))
        requirements = {"requirements": []}
    modules: list[dict[str, Any]] = []
    for raw_module in list(fragments.get("unit_contract") or []):
        try:
            modules.append(validate_unit_contract(raw_module, complexity_policy=complexity_policy))
        except ValueError as exc:
            unit_id = str(dict(raw_module or {}).get("unit_id") or "unknown")
            findings.append(
                ArchitectureFinding(
                    ArchitectureFindingKind.CONTRACT_DEFECT,
                    str(exc),
                    (unit_id,),
                    revision_targets=(ArchitectureRevisionTarget("unit", unit_id, (), "update"),),
                )
            )
    requirement_ids = {str(item["requirement_id"]) for item in requirements["requirements"]}
    covered_requirements = {requirement_id for module in modules for requirement_id in module["requirement_ids"]}
    unknown_requirement_refs = covered_requirements - requirement_ids
    if unknown_requirement_refs:
        affected_units = [
            str(module["unit_id"])
            for module in modules
            if set(module["requirement_ids"]) & unknown_requirement_refs
        ]
        findings.append(
            ArchitectureFinding(
                ArchitectureFindingKind.CONTRACT_DEFECT,
                "unit contracts reference unknown requirements",
                tuple(sorted(unknown_requirement_refs)),
                revision_targets=tuple(
                    ArchitectureRevisionTarget("unit", unit_id, ("requirement_ids",), "update")
                    for unit_id in sorted(affected_units)
                ),
            )
        )
    missing_coverage = requirement_ids - covered_requirements
    if missing_coverage:
        findings.append(
            ArchitectureFinding(
                ArchitectureFindingKind.CONTRACT_DEFECT,
                "requirements are not owned by any unit contract",
                tuple(sorted(missing_coverage)),
                revision_targets=tuple(
                    ArchitectureRevisionTarget("unit", str(module["unit_id"]), ("requirement_ids",), "update")
                    for module in modules
                ),
            )
        )
    topology = dict(fragments.get("topology") or {})
    unit_ids = {str(module["unit_id"]) for module in modules}
    topology_findings = _validate_topology(topology, unit_ids)
    findings.extend(topology_findings)
    findings.extend(_review_complexity_budget(modules))
    findings.extend(
        _review_declared_interface_handoffs(
            modules,
            [dict(item or {}) for item in list(fragments.get("cross_unit_contract") or [])],
        )
    )
    verdict = "PASS" if not findings else "FAIL"
    return ArchitectureReviewResult(verdict=verdict, findings=tuple(findings))


def _review_complexity_budget(modules: list[Mapping[str, Any]]) -> list[ArchitectureFinding]:
    findings: list[ArchitectureFinding] = []
    for module in modules:
        violations = _text_list(module.get("complexity_policy_violations"))
        if not violations or module.get("complexity_waiver_ref"):
            continue
        unit_id = str(module.get("unit_id") or "unknown")
        findings.append(
            ArchitectureFinding(
                ArchitectureFindingKind.CONTRACT_DEFECT,
                "module exceeds its complexity budget without a human waiver; split it before execution: " + "; ".join(violations),
                (unit_id, *violations),
                revision_targets=(
                    ArchitectureRevisionTarget(
                        "unit",
                        unit_id,
                        ("complexity_budget", "split_conditions"),
                        "update",
                    ),
                ),
            )
        )
    return findings


def _review_declared_interface_handoffs(
    modules: list[Mapping[str, Any]],
    cross_contracts: list[Mapping[str, Any]],
) -> list[ArchitectureFinding]:
    """Check that a consumed internal interface has a real provider and handoff.

    `consumed_interfaces[].ownership` already names the owning module in the
    contract schema. Requiring a matching producer/consumer contract turns
    that field into an auditable dependency rather than an aspirational note.
    """

    findings: list[ArchitectureFinding] = []
    module_by_id = {str(item.get("unit_id") or ""): item for item in modules}
    for consumer in modules:
        consumer_id = str(consumer.get("unit_id") or "")
        for consumed in list(consumer.get("consumed_interfaces") or []):
            interface = dict(consumed or {})
            provider_id = str(interface.get("ownership") or "").strip()
            interface_name = str(interface.get("name") or "").strip()
            if not consumer_id or not interface_name or provider_id not in module_by_id:
                continue
            provider = module_by_id[provider_id]
            pair_contracts = [
                contract
                for contract in cross_contracts
                if str(contract.get("producer") or "") == provider_id
                and str(contract.get("consumer") or "") == consumer_id
            ]
            matching_contract = next(
                (
                    contract
                    for contract in pair_contracts
                    if _interface_names_compatible(interface_name, str(contract.get("interface") or ""))
                ),
                None,
            )
            if matching_contract is None:
                if pair_contracts:
                    contract = pair_contracts[0]
                    contract_id = str(contract.get("id") or _revision_cross_contract_id(provider_id, consumer_id))
                    findings.append(
                        ArchitectureFinding(
                            ArchitectureFindingKind.CONTRACT_DEFECT,
                            f"cross-unit contract {contract_id} does not describe {consumer_id}'s consumed interface {interface_name} from {provider_id}",
                            (provider_id, consumer_id, contract_id, interface_name),
                            revision_targets=(
                                ArchitectureRevisionTarget(
                                    "cross_unit_contract",
                                    contract_id,
                                    ("interface", "data_shape", "ownership_transfer", "lifecycle_handoff", "error_behavior"),
                                    "update",
                                ),
                            ),
                        )
                    )
                else:
                    contract_id = _revision_cross_contract_id(provider_id, consumer_id)
                    findings.append(
                        ArchitectureFinding(
                            ArchitectureFindingKind.CONTRACT_DEFECT,
                            f"{consumer_id} consumes {interface_name} from {provider_id} without a directional cross-unit contract",
                            (provider_id, consumer_id, interface_name),
                            revision_targets=(
                                ArchitectureRevisionTarget("cross_unit_contract", contract_id, (), "create"),
                            ),
                        )
                    )
            provided = [dict(item or {}) for item in list(provider.get("provided_interfaces") or [])]
            if not any(_interface_names_compatible(interface_name, str(item.get("name") or "")) for item in provided):
                findings.append(
                    ArchitectureFinding(
                        ArchitectureFindingKind.CONTRACT_DEFECT,
                        f"{consumer_id} consumes {interface_name} from {provider_id}, but {provider_id} does not declare a compatible provided interface",
                        (provider_id, consumer_id, interface_name),
                        revision_targets=(
                            ArchitectureRevisionTarget("unit", provider_id, ("provided_interfaces",), "update"),
                        ),
                    )
                )
    return findings


def _revision_cross_contract_id(producer_id: str, consumer_id: str) -> str:
    def normalize(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", value.lower().removeprefix("ohos_")).strip("_")

    return f"xcu_{normalize(producer_id)}_to_{normalize(consumer_id)}"


def _interface_names_compatible(expected: str, actual: str) -> bool:
    if not expected or not actual:
        return False
    ignored = {"ohos", "native", "typed", "ops", "interface", "lifecycle", "the", "and"}
    expected_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", expected.lower())
        if token not in ignored
    }
    actual_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", actual.lower())
        if token not in ignored
    }
    return bool(expected_tokens) and expected_tokens.issubset(actual_tokens)


def compile_architecture_markdown(manifest: Mapping[str, Any], fragments: Mapping[str, Any]) -> str:
    requirements = list(dict(fragments.get("requirements") or {}).get("requirements") or [])
    modules = list(fragments.get("unit_contract") or [])
    topology = dict(fragments.get("topology") or {})
    assumptions = dict(fragments.get("assumption_ledger") or {})
    risks = dict(fragments.get("risk_ledger") or {})
    lines = ["# Architecture Contract", "", f"Manifest: `{_manifest_digest_hint(manifest)}`", "", "## Requirements", ""]
    for item in requirements:
        lines.append(f"- **{item.get('requirement_id')}** [{item.get('strength', 'hard')}] {item.get('statement', '')}")
    lines.extend(["", "## Module Topology", ""])
    dependency_map = dict(topology.get("depends_on") or {})
    for module in modules:
        unit_id = str(module.get("unit_id") or "")
        dependencies = ", ".join(str(item) for item in list(dependency_map.get(unit_id) or [])) or "none"
        lines.extend(
            [
                f"### {unit_id}",
                "",
                str(module.get("responsibility") or ""),
                "",
                f"- Behavior: `{module.get('unit_behavior_kind', '')}`",
                f"- Starts after: {dependencies}",
                f"- Owns: {', '.join(_text_list(module.get('owned_area'))) or 'none'}",
                f"- Requirements: {', '.join(_text_list(module.get('requirement_ids'))) or 'none'}",
                "- Invariants:",
            ]
        )
        module_invariants = list(module.get("invariants") or [])
        lines.extend(f"  - {_display(item)}" for item in module_invariants) if module_invariants else lines.append("  - none")
        lines.extend(["- Lifecycle / state:", f"  - {_display(module.get('lifecycle'))}", f"  - {_display(module.get('state_model'))}", ""])
    lines.extend(["## Assumptions", ""])
    assumption_items = list(assumptions.get("assumptions") or [])
    lines.extend(f"- {_display(item)}" for item in assumption_items) if assumption_items else lines.append("- none")
    lines.extend(["", "## Risks", ""])
    risk_items = list(risks.get("risks") or [])
    lines.extend(f"- {_display(item)}" for item in risk_items) if risk_items else lines.append("- none")
    return "\n".join(lines).rstrip() + "\n"


def _validate_topology(topology: Mapping[str, Any], unit_ids: set[str]) -> tuple[ArchitectureFinding, ...]:
    depends_on = {
        str(unit_id): _text_list(dependencies)
        for unit_id, dependencies in dict(topology.get("depends_on") or {}).items()
    }
    findings: list[ArchitectureFinding] = []
    if set(depends_on) != unit_ids:
        missing_nodes = sorted(unit_ids - set(depends_on))
        extra_nodes = sorted(set(depends_on) - unit_ids)
        findings.append(
            ArchitectureFinding(
                ArchitectureFindingKind.ARCHITECTURE_DEFECT,
                "topology node set does not match unit contracts",
                tuple(sorted(set(depends_on) ^ unit_ids)),
                revision_targets=tuple(
                    [ArchitectureRevisionTarget("topology", unit_id, ("depends_on",), "create") for unit_id in missing_nodes]
                    + [ArchitectureRevisionTarget("topology", unit_id, (), "delete") for unit_id in extra_nodes]
                ),
            )
        )
    unknown = {dependency for dependencies in depends_on.values() for dependency in dependencies if dependency not in unit_ids}
    if unknown:
        affected_nodes = sorted(
            unit_id
            for unit_id, dependencies in depends_on.items()
            if any(dependency in unknown for dependency in dependencies)
        )
        findings.append(
            ArchitectureFinding(
                ArchitectureFindingKind.ARCHITECTURE_DEFECT,
                "topology references unknown dependency nodes",
                tuple(sorted(unknown)),
                revision_targets=tuple(
                    ArchitectureRevisionTarget("topology", unit_id, ("depends_on",), "update")
                    for unit_id in affected_nodes
                ),
            )
        )
    indegree = {unit_id: 0 for unit_id in unit_ids}
    dependents = {unit_id: [] for unit_id in unit_ids}
    for unit_id, dependencies in depends_on.items():
        for dependency in dependencies:
            if unit_id in indegree and dependency in dependents:
                indegree[unit_id] += 1
                dependents[dependency].append(unit_id)
    ready = sorted(unit_id for unit_id, count in indegree.items() if count == 0)
    visited = 0
    while ready:
        current = ready.pop(0)
        visited += 1
        for dependent in sorted(dependents[current]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort()
    if visited != len(unit_ids):
        cycle_nodes = tuple(sorted(unit_id for unit_id, count in indegree.items() if count > 0))
        findings.append(
            ArchitectureFinding(
                ArchitectureFindingKind.ARCHITECTURE_DEFECT,
                "topology contains a cycle",
                cycle_nodes,
                revision_targets=tuple(
                    ArchitectureRevisionTarget("topology", unit_id, ("depends_on",), "update")
                    for unit_id in cycle_nodes
                ),
            )
        )
    return tuple(findings)


def _is_artifact_ref(value: Any) -> bool:
    return isinstance(value, Mapping) and len(str(value.get("sha256") or "")) == 64 and bool(value.get("durable"))


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _non_negative_int(value: Any, field_name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"complexity_budget.{field_name} must be an integer") from exc
    if parsed < 0:
        raise ValueError(f"complexity_budget.{field_name} must be non-negative")
    return parsed


def _optional_non_negative_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return _non_negative_int(value, "line")


def _manifest_digest_hint(manifest: Mapping[str, Any]) -> str:
    values = architecture_manifest_child_refs(manifest)
    return ", ".join(f"{name}:{digest[:8]}" for name, digest in values)


def _display(value: Any) -> str:
    if value in (None, "", [], {}):
        return "N/A"
    if isinstance(value, Mapping):
        return "; ".join(f"{key}: {_display(item)}" for key, item in value.items())
    if isinstance(value, list):
        return "; ".join(_display(item) for item in value)
    return str(value)
