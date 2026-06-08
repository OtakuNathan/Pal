from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from pal.foundation import utc_now
from pal.minion.utils import coerce_int
from pal.minion.work_order import ReviewGateResult, validate_review_gate_result


@dataclass
class MinionReviewGateStore:
    owner: Any

    def submit_review_gate(
        self,
        gate_payload: dict[str, Any] | ReviewGateResult,
        *,
        reviewer_profile: str = "",
        work_order_id: str = "",
        run_id: str = "",
    ) -> dict[str, Any]:
        repo = self.owner
        repo.ensure_schema()
        gate = validate_review_gate_result(gate_payload)
        payload = gate.to_dict()
        if reviewer_profile:
            payload["reviewer_profile"] = str(reviewer_profile or "").strip()
            gate = ReviewGateResult.from_dict(payload)
        target_kind, target_key, target_binding = repo._validate_review_gate_target(gate)
        self._validate_review_gate_tool_evidence_refs(gate)
        self._validate_bound_review_gate_evidence(gate, target_binding)
        created_at = utc_now()
        normalized_payload = gate.to_dict()
        normalized_payload["target"] = dict(target_binding)
        normalized_payload["created_at"] = created_at
        normalized_payload["target_kind"] = target_kind
        normalized_payload["target_key"] = target_key
        normalized_work_order_id = str(work_order_id or normalized_payload.get("target", {}).get("work_order_id") or "").strip()
        normalized_run_id = str(run_id or normalized_payload.get("target", {}).get("run_id") or "").strip()
        with repo._connect() as db:
            db.execute(
                """
                INSERT INTO minion_review_gates(
                    gate_id, gate_kind, target_kind, target_key, verdict, summary,
                    payload_json, reviewer_profile, work_order_id, run_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    gate.gate_id,
                    gate.gate_kind,
                    target_kind,
                    target_key,
                    gate.verdict,
                    gate.summary,
                    _json(normalized_payload),
                    gate.reviewer_profile,
                    normalized_work_order_id,
                    normalized_run_id,
                    created_at,
                ),
            )
            ledger_work_order_id = normalized_work_order_id or str(normalized_payload.get("target", {}).get("plan_ref", {}).get("plan_id") or "plan_review")
            repo._insert_ledger(
                db,
                ledger_work_order_id,
                "review_gate",
                gate.summary or f"{gate.gate_kind} review gate {gate.verdict}",
                normalized_payload,
                gate.reviewer_profile,
                normalized_run_id,
                created_at,
            )
            milestone_closure: dict[str, Any] = {}
            if gate.gate_kind in {"checkpoint_verification", "repair_verification"} and gate.verdict == "pass" and target_kind == "checkpoint":
                milestone_closure = repo._close_checkpoint_from_review_gate_locked(
                    db,
                    checkpoint_id=target_key,
                    gate_payload=normalized_payload,
                    created_at=created_at,
                )
        return {
            "status": "recorded",
            "review_gate": normalized_payload,
            "review_gate_ref": {
                "gate_id": gate.gate_id,
                "gate_kind": gate.gate_kind,
                "target_kind": target_kind,
                "target_key": target_key,
                "verdict": gate.verdict,
                "created_at": created_at,
            },
            "milestone_closure": milestone_closure,
        }

    def record_review_tool_evidence_refs(
        self,
        refs: list[dict[str, Any]],
        *,
        work_order_id: str,
        run_id: str = "",
        reviewer_profile: str = "",
    ) -> list[dict[str, Any]]:
        repo = self.owner
        repo.ensure_schema()
        normalized: list[dict[str, Any]] = []
        now = utc_now()
        with repo._connect() as db:
            for raw in list(refs or []):
                if not isinstance(raw, dict):
                    continue
                ref = dict(raw)
                if str(ref.get("ledger_id") or "").strip():
                    normalized.append(ref)
                    continue
                evidence_ref_id = str(ref.get("evidence_ref_id") or "").strip() or f"tev_{uuid4().hex[:12]}"
                ref["evidence_ref_id"] = evidence_ref_id
                payload = {
                    "status": "recorded",
                    "kind": "review_tool_evidence",
                    "evidence_ref": ref,
                    "reviewer_profile": str(reviewer_profile or ""),
                }
                ledger_id = repo._insert_ledger(
                    db,
                    str(work_order_id or "review_tool_evidence"),
                    "review_tool_evidence",
                    str(ref.get("summary") or ref.get("tool_name") or evidence_ref_id),
                    payload,
                    "",
                    str(run_id or ""),
                    now,
                )
                ref["ledger_id"] = ledger_id
                normalized.append(ref)
        return normalized

    def validate_external_verification_ref(self, ref: Any) -> dict[str, Any]:
        repo = self.owner
        repo.ensure_schema()
        payload = dict(ref or {}) if isinstance(ref, dict) else {"ref": str(ref or "")}
        kind = str(payload.get("kind") or payload.get("type") or "").strip().lower()
        if kind in {"review_gate", "review_gate_ref"} or payload.get("gate_id"):
            loaded = self.load_review_gate(payload.get("review_gate_ref") or payload)
            gate = dict(loaded.get("review_gate") or {})
            if str(gate.get("verdict") or "").strip().lower() != "pass":
                raise ValueError("external_verification_ref review_gate must be a pass gate")
            return {"kind": "review_gate", "review_gate_ref": dict(loaded.get("review_gate_ref") or {})}
        if kind in {"checkpoint", "checkpoint_ref"} or payload.get("checkpoint_id"):
            checkpoint_id = str(payload.get("checkpoint_id") or payload.get("id") or "").strip()
            if not checkpoint_id:
                raise ValueError("external_verification_ref checkpoint_id is required")
            with repo._connect() as db:
                row = repo._fetch_one(db, "SELECT * FROM minion_worker_checkpoints WHERE checkpoint_id = ?", (checkpoint_id,))
            if row is None:
                raise ValueError(f"unknown external checkpoint ref: {checkpoint_id}")
            return {"kind": "checkpoint", "checkpoint_id": checkpoint_id, "status": str(row["status"] or "")}
        if kind in {"ledger", "ledger_event"} or payload.get("ledger_id"):
            ledger_id = str(payload.get("ledger_id") or payload.get("id") or "").strip()
            if not ledger_id:
                raise ValueError("external_verification_ref ledger_id is required")
            with repo._connect() as db:
                row = repo._fetch_one(db, "SELECT * FROM minion_worker_ledger WHERE ledger_id = ?", (ledger_id,))
            if row is None:
                raise ValueError(f"unknown external ledger ref: {ledger_id}")
            return {"kind": "ledger", "ledger_id": ledger_id, "event_kind": str(row["event_kind"] or "")}
        if kind in {"artifact", "file", "path"} or payload.get("path"):
            path = _resolve_runtime_file_ref(payload, runtime_root=repo.runtime_root)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            expected = str(payload.get("sha256") or "").strip()
            if expected and expected != digest:
                raise ValueError(f"external_verification_ref sha256 mismatch for {path}")
            return {"kind": "artifact", "path": str(path), "sha256": digest}
        raise ValueError("external_verification_ref must reference a review_gate, checkpoint, ledger event, or runtime artifact path")

    def count_ledger_events(self, work_order_id: str, event_kind: str) -> int:
        repo = self.owner
        repo.ensure_schema()
        with repo._connect() as db:
            row = repo._fetch_one(
                db,
                "SELECT COUNT(*) AS count FROM minion_worker_ledger WHERE work_order_id = ? AND event_kind = ?",
                (str(work_order_id), str(event_kind)),
            )
        return int(row["count"] if row else 0)

    def load_review_gate(self, review_gate_ref: Any) -> dict[str, Any]:
        repo = self.owner
        repo.ensure_schema()
        gate_id = _coerce_review_gate_id(review_gate_ref)
        with repo._connect() as db:
            row = repo._fetch_one(db, "SELECT * FROM minion_review_gates WHERE gate_id = ?", (gate_id,))
        if row is None:
            raise ValueError(f"unknown review_gate_ref: {gate_id}")
        payload = _loads(row["payload_json"])
        payload.setdefault("gate_id", str(row["gate_id"]))
        payload.setdefault("gate_kind", str(row["gate_kind"]))
        payload.setdefault("verdict", str(row["verdict"]))
        payload.setdefault("summary", str(row["summary"] or ""))
        payload.setdefault("reviewer_profile", str(row["reviewer_profile"] or ""))
        payload.setdefault("created_at", str(row["created_at"] or ""))
        payload.setdefault("target_kind", str(row["target_kind"] or ""))
        payload.setdefault("target_key", str(row["target_key"] or ""))
        return {"review_gate": payload, "review_gate_ref": compact_review_gate_ref(payload)}

    def latest_review_gate_for_checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        repo = self.owner
        repo.ensure_schema()
        normalized = str(checkpoint_id or "").strip()
        if not normalized:
            return {"status": "invalid", "error": "checkpoint_id is required"}
        with repo._connect() as db:
            row = repo._fetch_one(
                db,
                """
                SELECT * FROM minion_review_gates
                WHERE target_kind = 'checkpoint' AND target_key = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (normalized,),
            )
        if row is None:
            return {"status": "not_found", "checkpoint_id": normalized}
        payload = _loads(row["payload_json"])
        return {
            "status": "ok",
            "review_gate": payload,
            "review_gate_ref": compact_review_gate_ref(payload),
        }

    def latest_review_gate_for_plan_ref(self, plan_ref: Any) -> dict[str, Any]:
        repo = self.owner
        repo.ensure_schema()
        try:
            loaded = repo.load_dispatchable_plan_ref(plan_ref)
        except Exception as exc:
            return {"status": "invalid", "error": str(exc)}
        normalized_ref = dict(loaded.get("plan_ref") or {})
        target_key = plan_target_key(normalized_ref)
        if not target_key:
            return {"status": "invalid", "error": "plan_ref target key is empty"}
        with repo._connect() as db:
            row = repo._fetch_one(
                db,
                """
                SELECT * FROM minion_review_gates
                WHERE gate_kind = 'plan_acceptance' AND target_kind = 'plan_ref' AND target_key = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (target_key,),
            )
        if row is None:
            return {"status": "not_found", "plan_ref": normalized_ref}
        payload = _loads(row["payload_json"])
        return {
            "status": "ok",
            "review_gate": payload,
            "review_gate_ref": compact_review_gate_ref(payload),
        }

    def close_checkpoint_from_review_gate(self, review_gate_ref: Any) -> dict[str, Any]:
        repo = self.owner
        repo.ensure_schema()
        loaded = self.load_review_gate(review_gate_ref)
        gate = dict(loaded.get("review_gate") or {})
        if str(gate.get("verdict") or "") != "pass":
            raise ValueError("checkpoint closure requires a passing review gate")
        target = dict(gate.get("target") or {})
        checkpoint_id = str(target.get("checkpoint_id") or "").strip()
        if not checkpoint_id:
            raise ValueError("checkpoint closure requires target.checkpoint_id")
        with repo._connect() as db:
            return repo._close_checkpoint_from_review_gate_locked(db, checkpoint_id=checkpoint_id, gate_payload=gate, created_at=utc_now())

    def validate_plan_acceptance_gate(self, review_gate_ref: Any, loaded_plan: dict[str, Any]) -> dict[str, Any]:
        gate_payload = dict(self.load_review_gate(review_gate_ref).get("review_gate") or {})
        if str(gate_payload.get("gate_kind") or "") != "plan_acceptance":
            raise ValueError("review_gate_ref must reference a plan_acceptance gate")
        if str(gate_payload.get("verdict") or "") != "pass":
            raise ValueError("plan acceptance requires review gate verdict=pass")
        plan_ref = dict(loaded_plan.get("plan_ref") or {})
        target = dict(gate_payload.get("target") or {})
        target_ref = dict(target.get("plan_ref") or {})
        if plan_target_key(target_ref) != plan_target_key(plan_ref):
            raise ValueError("review gate target plan_ref does not match accepted plan_ref")
        return gate_payload

    def _validate_review_gate_tool_evidence_refs(self, gate: ReviewGateResult) -> None:
        refs = [dict(item) for item in list((gate.metadata or {}).get("tool_evidence_refs") or []) if isinstance(item, dict)]
        if not refs:
            return
        repo = self.owner
        with repo._connect() as db:
            for ref in refs:
                ledger_id = str(ref.get("ledger_id") or "").strip()
                if not ledger_id:
                    raise ValueError("review gate tool_evidence_refs require ledger_id")
                row = repo._fetch_one(db, "SELECT * FROM minion_worker_ledger WHERE ledger_id = ?", (ledger_id,))
                if row is None:
                    raise ValueError(f"unknown review tool evidence ledger_id: {ledger_id}")
                if str(row["event_kind"] or "") != "review_tool_evidence":
                    raise ValueError(f"ledger_id is not review_tool_evidence: {ledger_id}")

    def _validate_bound_review_gate_evidence(self, gate: ReviewGateResult, target_binding: dict[str, Any]) -> None:
        if gate.verdict != "pass" or gate.gate_kind not in {"checkpoint_verification", "repair_verification"}:
            return
        criteria = [str(item).strip() for item in list(target_binding.get("acceptance_criteria") or []) if str(item or "").strip()]
        if not criteria:
            return
        covered = _review_gate_coverage_tokens(gate)
        missing: list[str] = []
        for index, criterion in enumerate(criteria, start=1):
            normalized = _coverage_token(criterion)
            aliases = {normalized, str(index), f"#{index}", f"ac{index}", f"acceptance_{index}"}
            if not any(alias in covered for alias in aliases):
                missing.append(criterion)
        if missing:
            raise ValueError(
                "checkpoint pass review gate lacks evidence coverage for acceptance criteria: "
                + "; ".join(missing[:3])
            )


def compact_review_gate_ref(payload: dict[str, Any]) -> dict[str, Any]:
    gate = dict(payload or {})
    result: dict[str, Any] = {}
    for key in ("gate_id", "gate_kind", "verdict", "target_kind", "target_key", "created_at"):
        value = gate.get(key)
        if value not in (None, "", []):
            result[key] = value
    target = dict(gate.get("target") or {})
    if isinstance(target.get("plan_ref"), dict):
        plan_ref = dict(target.get("plan_ref") or {})
        result["plan_ref"] = {
            key: plan_ref.get(key)
            for key in ("path", "sha256", "plan_id", "task_id", "plan_revision")
            if plan_ref.get(key) not in (None, "", [])
        }
    for key in ("checkpoint_id", "work_order_id", "run_id", "module_id", "milestone_id", "milestone_index", "commit_sha"):
        if target.get(key) not in (None, "", []):
            result[key] = target.get(key)
    return result


def plan_target_key(plan_ref: dict[str, Any]) -> str:
    ref = dict(plan_ref or {})
    return ":".join(
        [
            "plan",
            _safe_id(str(ref.get("task_id") or "")),
            _safe_id(str(ref.get("plan_id") or "")),
            f"v{_plan_revision_from_payload(None, ref)}",
            str(ref.get("sha256") or "").strip(),
        ]
    )


def _coerce_review_gate_id(value: Any) -> str:
    if isinstance(value, dict):
        gate_id = str(value.get("gate_id") or value.get("id") or "").strip()
    else:
        gate_id = str(value or "").strip()
    if not gate_id:
        raise ValueError("review_gate_ref.gate_id is required")
    return gate_id


def _review_gate_coverage_tokens(gate: ReviewGateResult) -> set[str]:
    tokens: set[str] = set()
    for item in [*gate.evidence, *gate.commands_run, *gate.api_evidence]:
        tokens.update(_coverage_tokens_from_item(item))
    return tokens


def _coverage_tokens_from_item(item: dict[str, Any]) -> set[str]:
    if not isinstance(item, dict):
        return set()
    raw_values: list[Any] = []
    for key in ("covers", "coverage", "acceptance_criteria", "acceptance_criteria_refs", "acceptance_refs"):
        if key in item:
            raw_values.append(item.get(key))
    if isinstance(item.get("coverage"), dict):
        raw_values.extend(item["coverage"].values())
    result: set[str] = set()
    for value in raw_values:
        if isinstance(value, str):
            result.add(_coverage_token(value))
        elif isinstance(value, (int, float)):
            result.add(str(int(value)))
        elif isinstance(value, dict):
            for nested in value.values():
                if isinstance(nested, (list, tuple, set)):
                    result.update(_coverage_token(item) for item in nested)
                else:
                    result.add(_coverage_token(nested))
        elif isinstance(value, (list, tuple, set)):
            result.update(_coverage_token(item) for item in value)
    return {item for item in result if item}


def _coverage_token(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _resolve_runtime_file_ref(ref: dict[str, Any], *, runtime_root: Path) -> Path:
    raw = str(ref.get("path") or ref.get("artifact_path") or ref.get("relative_path") or ref.get("ref") or "").strip()
    if not raw:
        raise ValueError("external_verification_ref artifact path is required")
    root = Path(runtime_root).resolve()
    path = Path(raw)
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("external_verification_ref artifact path must be under runtime_root") from exc
    if not resolved.exists() or not resolved.is_file():
        raise ValueError(f"external_verification_ref artifact path does not exist: {resolved}")
    return resolved


def _plan_revision_from_payload(payload: Any, ref: Any | None = None) -> int:
    payload_dict = dict(payload) if isinstance(payload, dict) else {}
    ref_dict = dict(ref) if isinstance(ref, dict) else {}
    metadata = dict(payload_dict.get("metadata") or {}) if isinstance(payload_dict.get("metadata"), dict) else {}
    for value in (payload_dict.get("plan_revision"), metadata.get("plan_revision"), ref_dict.get("plan_revision")):
        coerced = coerce_int(value, -1)
        if coerced >= 0:
            return int(coerced)
    return 0


def _safe_id(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(value or "")).strip("_")[:80]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return {}
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return {}
