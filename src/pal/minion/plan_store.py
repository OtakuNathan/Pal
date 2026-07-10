from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from pal.foundation import utc_now
from pal.minion.review_gate_store import compact_review_gate_ref
from pal.minion.work_order import (
    PlanArtifact,
    dispatchable_plan_validation,
    validate_dispatchable_plan_artifact,
    validate_final_plan_artifact,
)


PLAN_REVISION_DIR_PARTS = ("data", "minion", "plan_revisions")


@dataclass
class MinionPlanStore:
    owner: Any

    @property
    def runtime_root(self) -> Path:
        return self.owner.runtime_root

    def load_dispatchable_plan_ref(self, plan_ref: Any) -> dict[str, Any]:
        return self._load_plan_ref(plan_ref, require_dispatchable=True)

    def load_revisable_plan_ref(self, plan_ref: Any) -> dict[str, Any]:
        return self._load_plan_ref(plan_ref, require_dispatchable=False)

    def _load_plan_ref(self, plan_ref: Any, *, require_dispatchable: bool) -> dict[str, Any]:
        ref = coerce_plan_ref(plan_ref)
        path = resolve_plan_ref_path(ref, runtime_root=self.runtime_root)
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        expected = str(ref.get("sha256") or "").strip()
        if expected and expected != digest:
            raise ValueError(f"plan_ref sha256 mismatch for {path}")
        try:
            payload = json.loads(content.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"plan_ref is not valid JSON: {path}") from exc
        artifact = validate_final_plan_artifact(payload)
        try:
            validation = dispatchable_plan_validation(artifact)
        except ValueError as exc:
            if require_dispatchable:
                raise
            validation = {"status": "invalid", "error": str(exc)}
        plan_revision = plan_revision_from_payload(payload, ref)
        payload_metadata = dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {}
        normalized_ref = {
            "path": str(path),
            "sha256": digest,
            "plan_id": artifact.plan_id,
            "task_id": artifact.task_id,
            "plan_revision": plan_revision,
        }
        revision_of = payload_metadata.get("revision_of")
        if isinstance(revision_of, dict):
            normalized_ref["revision_of"] = dict(revision_of)
        for key in ("accepted", "accepted_at", "acceptance_marker_path", "accepted_latest_marker_path"):
            if key in ref:
                normalized_ref[key] = ref[key]
        return {
            "plan_artifact": plan_artifact_payload(artifact, plan_revision=plan_revision),
            "plan_ref": normalized_ref,
            "plan_validation": validation,
        }

    def load_accepted_plan_ref(self, plan_ref: Any) -> dict[str, Any]:
        loaded = self.load_dispatchable_plan_ref(plan_ref)
        ref = dict(loaded.get("plan_ref") or {})
        marker = load_accepted_plan_marker(self.runtime_root, ref)
        marker_ref = dict(marker.get("plan_ref") or {})
        expected_sha = str(ref.get("sha256") or "").strip()
        marker_sha = str(marker_ref.get("sha256") or "").strip()
        if expected_sha and marker_sha and expected_sha != marker_sha:
            raise ValueError("plan_ref acceptance marker sha256 mismatch")
        expected_revision = plan_revision_from_payload(loaded.get("plan_artifact"), ref)
        marker_revision = plan_revision_from_payload(None, marker_ref)
        if marker_revision != expected_revision:
            raise ValueError("plan_ref acceptance marker revision mismatch")
        if marker.get("review_gate_ref"):
            self.owner.review_gates.validate_plan_acceptance_gate(marker.get("review_gate_ref"), loaded)
        elif not marker.get("human_override"):
            raise ValueError("plan_ref acceptance marker lacks review_gate_ref or human_override")
        accepted_ref = dict(ref)
        accepted_ref.update(
            {
                "accepted": True,
                "accepted_at": str(marker.get("accepted_at") or ""),
                "acceptance_marker_path": str(marker.get("acceptance_marker_path") or ""),
                "accepted_latest_marker_path": str(marker.get("accepted_latest_marker_path") or ""),
            }
        )
        loaded["plan_ref"] = accepted_ref
        loaded["review_gate_ref"] = dict(marker.get("review_gate_ref") or {})
        loaded["human_override"] = dict(marker.get("human_override") or {})
        return loaded

    def read_plan_ref(self, plan_ref: Any) -> dict[str, Any]:
        loaded = self.load_dispatchable_plan_ref(plan_ref)
        accepted: dict[str, Any] = {}
        try:
            accepted = self.load_accepted_plan_ref(loaded.get("plan_ref") or plan_ref)
        except Exception:
            accepted = {}
        payload = dict(loaded)
        if accepted:
            payload["plan_ref"] = dict(accepted.get("plan_ref") or loaded.get("plan_ref") or {})
            payload["accepted"] = True
        else:
            payload["accepted"] = False
        return {"status": "ok", **payload}

    def search_plan_refs(self, query: str = "", *, limit: int = 10) -> dict[str, Any]:
        self.owner.ensure_schema()
        terms = [item.lower() for item in str(query or "").split() if item.strip()]
        items: list[dict[str, Any]] = []
        for path in iter_plan_json_files(self.runtime_root):
            try:
                loaded = self.load_dispatchable_plan_ref({"path": str(path)})
            except Exception:
                continue
            artifact = dict(loaded.get("plan_artifact") or {})
            haystack = plan_search_text(artifact, loaded.get("plan_ref") or {}).lower()
            if terms and not all(term in haystack for term in terms):
                continue
            accepted = False
            accepted_ref: dict[str, Any] = {}
            with contextlib.suppress(Exception):
                accepted_loaded = self.load_accepted_plan_ref(loaded.get("plan_ref") or {})
                accepted = True
                accepted_ref = dict(accepted_loaded.get("plan_ref") or {})
            ref = accepted_ref or dict(loaded.get("plan_ref") or {})
            items.append(
                {
                    "plan_ref": ref,
                    "accepted": accepted,
                    "plan_id": str(ref.get("plan_id") or artifact.get("plan_id") or ""),
                    "task_id": str(ref.get("task_id") or artifact.get("task_id") or ""),
                    "plan_revision": plan_revision_from_payload(artifact, ref),
                    "summary": str(artifact.get("summary") or ""),
                    "path": str(path),
                }
            )
            if len(items) >= max(1, int(limit or 10)):
                break
        return {"status": "ok", "items": items, "count": len(items)}

    def revise_plan_ref(
        self,
        plan_ref: Any,
        revised_plan_artifact: dict[str, Any],
        *,
        revision_notes: str = "",
        accepted: bool = False,
        review_gate_ref: Any = None,
        human_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        source = self.load_revisable_plan_ref(plan_ref)
        source_ref = dict(source.get("plan_ref") or {})
        source_revision = plan_revision_from_payload(source.get("plan_artifact"), source_ref)
        if not isinstance(revised_plan_artifact, dict):
            raise ValueError("revised_plan_artifact must be an object")
        revised_payload = dict(revised_plan_artifact or {})
        expected_revision = source_revision + 1
        declared_revision = coerce_int(revised_payload.get("plan_revision"))
        if declared_revision is not None and declared_revision != expected_revision:
            raise ValueError(
                f"revised plan_revision must be {expected_revision} when revising source revision {source_revision}"
            )
        artifact = validate_dispatchable_plan_artifact(revised_payload)
        if artifact.plan_id != str(source_ref.get("plan_id") or ""):
            raise ValueError("revised plan_id must match source plan_ref")
        if artifact.task_id != str(source_ref.get("task_id") or ""):
            raise ValueError("revised task_id must match source plan_ref")
        latest_revision = latest_plan_revision(self.runtime_root, task_id=artifact.task_id, plan_id=artifact.plan_id)
        if latest_revision > source_revision:
            raise ValueError(
                f"source plan_ref is stale: latest revision is {latest_revision}, source revision is {source_revision}"
            )
        result = self._write_plan_revision(
            artifact,
            revision=expected_revision,
            source_plan_ref=source_ref,
            revision_notes=revision_notes,
        )
        if accepted:
            acceptance = self.accept_plan_ref(
                result["plan_ref"],
                reason=revision_notes or f"accepted plan revision {expected_revision}",
                review_gate_ref=review_gate_ref,
                human_override=human_override,
            )
            result["acceptance"] = acceptance
            result["plan_ref"] = dict(acceptance.get("plan_ref") or result["plan_ref"])
        return result

    def submit_plan_ref(
        self,
        plan_artifact: dict[str, Any] | PlanArtifact,
        *,
        submission_notes: str = "",
        replace_unaccepted_revision: bool = False,
    ) -> dict[str, Any]:
        artifact = validate_dispatchable_plan_artifact(plan_artifact)
        plan_revision = plan_revision_from_payload(plan_artifact)
        payload = plan_artifact_payload(artifact, plan_revision=plan_revision)
        metadata = dict(payload.get("metadata") or {})
        metadata["plan_revision"] = plan_revision
        if submission_notes:
            metadata["submission_notes"] = str(submission_notes)
        payload["metadata"] = metadata
        validation = dispatchable_plan_validation(artifact)
        plan_dir = plan_revision_dir(self.runtime_root, task_id=artifact.task_id, plan_id=artifact.plan_id)
        plan_dir.mkdir(parents=True, exist_ok=True)
        path = plan_dir / f"plan.v{plan_revision}.json"
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        replaced = False
        if path.exists():
            existing_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            new_digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            if existing_digest != new_digest:
                if not replace_unaccepted_revision:
                    raise ValueError(f"plan revision already exists with different content: {path}")
                acceptance_marker = plan_dir / f"accepted.v{plan_revision}.json"
                if acceptance_marker.exists():
                    raise ValueError(f"accepted plan revision cannot be replaced: {path}")
                temporary_path = plan_dir / f".{path.name}.{uuid4().hex}.tmp"
                try:
                    with temporary_path.open("x", encoding="utf-8") as handle:
                        handle.write(encoded)
                    if acceptance_marker.exists():
                        raise ValueError(f"accepted plan revision cannot be replaced: {path}")
                    temporary_path.replace(path)
                    replaced = True
                finally:
                    temporary_path.unlink(missing_ok=True)
        else:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(encoded)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        plan_ref = {
            "path": str(path),
            "sha256": digest,
            "plan_id": artifact.plan_id,
            "task_id": artifact.task_id,
            "plan_revision": plan_revision,
        }
        return {
            "status": "submitted",
            "plan_ref": plan_ref,
            "plan_artifact": payload,
            "plan_validation": validation,
            "plan_revision": plan_revision,
            "replaced_unaccepted_revision": replaced,
        }

    def accept_plan_ref(
        self,
        plan_ref: Any,
        *,
        reason: str = "",
        review_gate_ref: Any = None,
        human_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        loaded = self.load_dispatchable_plan_ref(plan_ref)
        ref = dict(loaded.get("plan_ref") or {})
        plan_revision = plan_revision_from_payload(loaded.get("plan_artifact"), ref)
        gate_payload: dict[str, Any] | None = None
        override_payload: dict[str, Any] | None = None
        if review_gate_ref:
            gate_payload = self.owner.review_gates.validate_plan_acceptance_gate(review_gate_ref, loaded)
            override_payload = validate_human_override(human_override)
        else:
            override_payload = validate_human_override(human_override)
            if override_payload is None:
                raise ValueError("plan acceptance requires passing plan review gate or explicit human_override.reason")
        accepted_at = utc_now()
        marker = {
            "status": "accepted",
            "accepted_at": accepted_at,
            "reason": str(reason or "").strip(),
            "plan_ref": ref,
            "plan_validation": dict(loaded.get("plan_validation") or {}),
        }
        if gate_payload is not None:
            marker["review_gate_ref"] = compact_review_gate_ref(gate_payload)
        if override_payload is not None:
            marker["human_override"] = dict(override_payload)
        marker_dir = plan_revision_dir(self.runtime_root, task_id=str(ref.get("task_id") or ""), plan_id=str(ref.get("plan_id") or ""))
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker_json = json.dumps(marker, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        marker_path = marker_dir / f"accepted.v{plan_revision}.json"
        if not marker_path.exists():
            with marker_path.open("x", encoding="utf-8") as handle:
                handle.write(marker_json)
        latest_marker_path = marker_dir / "accepted.json"
        latest_marker_path.write_text(marker_json, encoding="utf-8")
        accepted_ref = dict(ref)
        accepted_ref.update(
            {
                "accepted": True,
                "accepted_at": accepted_at,
                "acceptance_marker_path": str(marker_path),
                "accepted_latest_marker_path": str(latest_marker_path),
            }
        )
        return {
            "status": "accepted",
            "plan_ref": accepted_ref,
            "plan_artifact": loaded.get("plan_artifact"),
            "plan_validation": loaded.get("plan_validation"),
            "review_gate_ref": marker.get("review_gate_ref") or {},
            "human_override": marker.get("human_override") or {},
            "acceptance_marker_path": str(marker_path),
            "accepted_latest_marker_path": str(latest_marker_path),
            "plan_revision": plan_revision,
        }

    def _write_plan_revision(
        self,
        artifact: PlanArtifact,
        *,
        revision: int,
        source_plan_ref: dict[str, Any],
        revision_notes: str = "",
    ) -> dict[str, Any]:
        plan_revision = max(0, int(revision))
        payload = plan_artifact_payload(artifact, plan_revision=plan_revision)
        metadata = dict(payload.get("metadata") or {})
        metadata["plan_revision"] = plan_revision
        metadata["revision_of"] = {
            "path": str(source_plan_ref.get("path") or ""),
            "sha256": str(source_plan_ref.get("sha256") or ""),
            "plan_revision": plan_revision_from_payload(None, source_plan_ref),
        }
        if revision_notes:
            metadata["revision_notes"] = str(revision_notes)
        payload["metadata"] = metadata
        validation = dispatchable_plan_validation(artifact)
        revision_dir = plan_revision_dir(self.runtime_root, task_id=artifact.task_id, plan_id=artifact.plan_id)
        revision_dir.mkdir(parents=True, exist_ok=True)
        path = revision_dir / f"plan.v{plan_revision}.json"
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        with path.open("x", encoding="utf-8") as handle:
            handle.write(encoded)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        plan_ref = {
            "path": str(path),
            "sha256": digest,
            "plan_id": artifact.plan_id,
            "task_id": artifact.task_id,
            "plan_revision": plan_revision,
            "revision_of": metadata["revision_of"],
        }
        return {
            "status": "revised",
            "plan_ref": plan_ref,
            "plan_artifact": payload,
            "plan_validation": validation,
            "plan_revision": plan_revision,
        }


def iter_plan_json_files(runtime_root: Path) -> list[Path]:
    roots = [
        Path(runtime_root) / "data" / "minion" / "plan_revisions",
        Path(runtime_root) / "data" / "minion",
        Path(runtime_root) / "artifacts",
    ]
    seen: set[Path] = set()
    result: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.json")):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            result.append(resolved)
            if len(result) >= 2000:
                return result
    return result


def plan_search_text(plan_payload: dict[str, Any], plan_ref: dict[str, Any]) -> str:
    parts = [
        str(plan_ref.get("task_id") or ""),
        str(plan_ref.get("plan_id") or ""),
        str(plan_payload.get("task_id") or ""),
        str(plan_payload.get("plan_id") or ""),
        str(plan_payload.get("summary") or ""),
    ]
    for module in list(plan_payload.get("modules") or []):
        if not isinstance(module, dict):
            continue
        parts.extend(
            [
                str(module.get("module_id") or ""),
                str(module.get("owned_area") or ""),
                str(module.get("responsibility") or ""),
            ]
        )
        for milestone in list(module.get("internal_milestones") or []):
            if isinstance(milestone, dict):
                parts.extend([str(milestone.get("milestone_id") or ""), str(milestone.get("task") or "")])
    return "\n".join(part for part in parts if part)


def coerce_plan_ref(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        ref = dict(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{"):
            try:
                loaded = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError("plan_ref string is not valid JSON") from exc
            ref = dict(loaded) if isinstance(loaded, dict) else {"path": stripped}
        else:
            ref = {"path": stripped}
    else:
        ref = {}
    if not str(ref.get("path") or "").strip():
        artifact_dir = str(ref.get("artifact_dir") or "").strip()
        relative_path = str(ref.get("relative_path") or "").strip()
        if artifact_dir and not relative_path:
            ref_kind = str(ref.get("ref_kind") or "").strip()
            if ref_kind == "plan_draft":
                relative_path = "plan.draft.json"
            elif ref_kind in {"plan", "final_plan"}:
                relative_path = "plan.json"
        if artifact_dir and relative_path:
            ref["path"] = str(Path(artifact_dir) / relative_path)
    if not str(ref.get("path") or "").strip():
        raise ValueError("plan_ref.path is required")
    return ref


def resolve_plan_ref_path(plan_ref: dict[str, Any], *, runtime_root: Path) -> Path:
    raw = str(plan_ref.get("path") or "").strip()
    if not raw:
        raise ValueError("plan_ref.path is required")
    root = Path(runtime_root).resolve()
    path = Path(raw)
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("plan_ref.path must be under runtime_root") from exc
    if not resolved.exists() or not resolved.is_file():
        raise ValueError(f"plan_ref.path does not exist: {resolved}")
    return resolved


def load_accepted_plan_marker(runtime_root: Path, plan_ref: dict[str, Any]) -> dict[str, Any]:
    revision = plan_revision_from_payload(None, plan_ref)
    raw = str(plan_ref.get("acceptance_marker_path") or "").strip()
    paths: list[Path] = []
    if raw:
        with contextlib.suppress(Exception):
            paths.append(resolve_plan_ref_path({"path": raw}, runtime_root=runtime_root))
    task_id = str(plan_ref.get("task_id") or "").strip()
    plan_id = str(plan_ref.get("plan_id") or "").strip()
    if task_id and plan_id:
        marker_dir = plan_revision_dir(runtime_root, task_id=task_id, plan_id=plan_id)
        paths.append(marker_dir / f"accepted.v{revision}.json")
        paths.append(marker_dir / "accepted.json")
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        marker = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(marker, dict):
            continue
        marker_ref = dict(marker.get("plan_ref") or {})
        if plan_revision_from_payload(None, marker_ref) != revision:
            continue
        marker.setdefault("acceptance_marker_path", str(path))
        latest = plan_revision_dir(runtime_root, task_id=task_id, plan_id=plan_id) / "accepted.json" if task_id and plan_id else path
        marker.setdefault("accepted_latest_marker_path", str(latest))
        return marker
    raise ValueError("plan_ref is not accepted for dispatch")


def plan_revision_dir(runtime_root: Path, *, task_id: str, plan_id: str) -> Path:
    path = Path(runtime_root)
    for part in PLAN_REVISION_DIR_PARTS:
        path = path / part
    return path / safe_id(task_id) / safe_id(plan_id)


def latest_plan_revision(runtime_root: Path, *, task_id: str, plan_id: str) -> int:
    revision_dir = plan_revision_dir(runtime_root, task_id=task_id, plan_id=plan_id)
    latest = -1
    if not revision_dir.exists():
        return latest
    for path in revision_dir.glob("plan.v*.json"):
        stem = path.stem
        raw = stem.removeprefix("plan.v")
        value = coerce_int(raw)
        if value is not None:
            latest = max(latest, int(value))
    return latest


def plan_revision_from_payload(payload: Any, ref: Any | None = None) -> int:
    payload_dict = dict(payload) if isinstance(payload, dict) else {}
    ref_dict = dict(ref) if isinstance(ref, dict) else {}
    metadata = dict(payload_dict.get("metadata") or {}) if isinstance(payload_dict.get("metadata"), dict) else {}
    for value in (payload_dict.get("plan_revision"), metadata.get("plan_revision"), ref_dict.get("plan_revision")):
        coerced = coerce_int(value)
        if coerced is not None and coerced >= 0:
            return int(coerced)
    return 0


def plan_revision_gate_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "gate_id": str(payload.get("gate_id") or ""),
        "gate_kind": str(payload.get("gate_kind") or ""),
        "verdict": str(payload.get("verdict") or ""),
        "summary": str(payload.get("summary") or ""),
        "findings": [dict(item) for item in list(payload.get("findings") or []) if isinstance(item, dict)],
        "required_fixes": [dict(item) for item in list(payload.get("required_fixes") or []) if isinstance(item, dict)],
        "residual_risk": [dict(item) for item in list(payload.get("residual_risk") or []) if isinstance(item, dict)],
        "report_artifact_ref": dict(payload.get("report_artifact_ref") or {}),
    }


def validate_human_override(value: Any) -> dict[str, Any] | None:
    if value in (None, "", False):
        return None
    if not isinstance(value, dict):
        raise ValueError("human_override must be an object with reason")
    payload = dict(value or {})
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        raise ValueError("human_override.reason is required")
    if str(payload.get("source") or "").strip() != "control_action":
        raise ValueError("human_override.source must be control_action")
    if not str(payload.get("action_kind") or "").strip():
        raise ValueError("human_override.action_kind is required")
    return {
        "reason": reason,
        "actor": str(payload.get("actor") or payload.get("by") or "human").strip() or "human",
        "source": "control_action",
        "action_kind": str(payload.get("action_kind") or "").strip(),
        "target_scope": str(payload.get("target_scope") or "").strip(),
        "target_id": str(payload.get("target_id") or "").strip(),
        "interaction_origin": str(payload.get("interaction_origin") or "").strip(),
        "interaction_id": str(payload.get("interaction_id") or "").strip(),
        "interaction_kind": str(payload.get("interaction_kind") or "").strip(),
        "route": dict(payload.get("route") or {}) if isinstance(payload.get("route"), dict) else {},
        "created_at": utc_now(),
    }


def plan_artifact_payload(artifact: PlanArtifact, *, plan_revision: int = 0) -> dict[str, Any]:
    revision = max(0, int(plan_revision or 0))
    payload = {"type": "FinalPlanArtifact", **artifact.to_dict(), "plan_revision": revision}
    metadata = dict(payload.get("metadata") or {})
    metadata.setdefault("plan_revision", revision)
    payload["metadata"] = metadata
    return payload


def coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def safe_id(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(value or "").strip())[:80] or uuid4().hex[:12]
