from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from pal.foundation import utc_now
from pal.minion.gates import project_active_gate_todo
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
        target_kind, target_key, target_binding = self._validate_review_gate_target(gate)
        self._validate_review_gate_tool_evidence_refs(gate)
        self._validate_pass_gate_findings(gate)
        self._validate_bound_review_gate_evidence(gate, target_binding)
        detected_contract_violations = _numeric_range_evidence_violations(
            gate,
            _review_target_numeric_range_criteria(target_binding),
            require_coverage=gate.verdict == "pass",
        )
        created_at = utc_now()
        normalized_payload = gate.to_dict()
        normalized_payload["target"] = dict(target_binding)
        if detected_contract_violations:
            _annotate_detected_contract_violations(normalized_payload, detected_contract_violations)
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
            repo.ledger.insert_ledger(
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
                milestone_closure = self._close_checkpoint_from_review_gate_locked(
                    db,
                    checkpoint_id=target_key,
                    gate_payload=normalized_payload,
                    created_at=created_at,
                )
            todo_projection = project_active_gate_todo(normalized_payload)
            todo_work_order_id = str(normalized_payload.get("target", {}).get("work_order_id") or normalized_work_order_id).strip()
            if todo_work_order_id:
                _write_active_gate_todo_locked(repo, db, todo_work_order_id, todo_projection)
                repo.ledger.insert_ledger(
                    db,
                    todo_work_order_id,
                    "gate_todo_projected",
                    str(todo_projection.get("summary") or f"{gate.gate_kind} todo projected"),
                    {
                        "status": str(todo_projection.get("status") or ""),
                        "review_gate_ref": {
                            "gate_id": gate.gate_id,
                            "gate_kind": gate.gate_kind,
                            "target_kind": target_kind,
                            "target_key": target_key,
                            "verdict": gate.verdict,
                            "created_at": created_at,
                        },
                        "active_gate_todo": todo_projection,
                    },
                    gate.reviewer_profile,
                    normalized_run_id,
                    created_at,
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
            "active_gate_todo": todo_projection,
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
                ledger_id = repo.ledger.insert_ledger(
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

    def _validate_review_gate_target(self, gate: ReviewGateResult) -> tuple[str, str, dict[str, Any]]:
        repo = self.owner
        target = dict(gate.target)
        if gate.gate_kind == "plan_acceptance":
            loaded = repo.load_dispatchable_plan_ref(target.get("plan_ref"))
            plan_ref = dict(loaded.get("plan_ref") or {})
            target["plan_ref"] = plan_ref
            target["plan_validation"] = dict(loaded.get("plan_validation") or {})
            return "plan_ref", plan_target_key(plan_ref), target
        if gate.gate_kind in {"checkpoint_verification", "repair_verification"}:
            checkpoint_id = str(target.get("checkpoint_id") or "").strip()
            if checkpoint_id:
                with repo._connect() as db:
                    row = repo._fetch_one(
                        db,
                        "SELECT * FROM minion_worker_checkpoints WHERE checkpoint_id = ?",
                        (checkpoint_id,),
                    )
                if row is None:
                    raise ValueError(f"unknown checkpoint_id for review gate: {checkpoint_id}")
                payload = _loads(row["payload_json"])
                expected_gate_kind = str(payload.get("expected_review_gate_kind") or "").strip().lower()
                if expected_gate_kind and gate.gate_kind != expected_gate_kind:
                    raise ValueError(f"review gate kind mismatch: checkpoint expects {expected_gate_kind}")
                if gate.gate_kind == "repair_verification" and not (expected_gate_kind == "repair_verification" or isinstance(payload.get("repair_attempt"), dict)):
                    raise ValueError("repair_verification requires a repair checkpoint target")
                if gate.verdict == "pass" and list(payload.get("shell_mutation_violations") or []):
                    raise ValueError("checkpoint has unresolved shell_mutation_violations; review gate cannot pass")
                commit_sha = str(target.get("commit_sha") or "").strip()
                row_commit = str(payload.get("commit_sha") or payload.get("git_commit", {}).get("commit_sha") or "").strip()
                if commit_sha and row_commit and commit_sha != row_commit:
                    raise ValueError("review gate checkpoint commit_sha mismatch")
                target.setdefault("work_order_id", str(row["work_order_id"] or ""))
                target.setdefault("run_id", str(row["run_id"] or ""))
                target.setdefault("minion_id", str(row["minion_id"] or ""))
                target.setdefault("milestone_index", coerce_int(row["milestone_index"], 0))
                target.setdefault("milestone_id", str(payload.get("milestone_id") or ""))
                target.setdefault("acceptance_criteria", [str(item) for item in list(payload.get("acceptance_criteria") or []) if str(item or "").strip()])
                if row_commit:
                    target.setdefault("commit_sha", row_commit)
                return "checkpoint", checkpoint_id, target
            commit_sha = str(target.get("commit_sha") or "").strip()
            if not commit_sha:
                raise ValueError("checkpoint review gate requires checkpoint_id or commit_sha")
            return "commit", commit_sha, target
        raise ValueError(f"unsupported review gate kind: {gate.gate_kind}")

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
            return self._close_checkpoint_from_review_gate_locked(db, checkpoint_id=checkpoint_id, gate_payload=gate, created_at=utc_now())

    def _close_checkpoint_from_review_gate_locked(
        self,
        db: Any,
        *,
        checkpoint_id: str,
        gate_payload: dict[str, Any],
        created_at: str,
    ) -> dict[str, Any]:
        repo = self.owner
        row = repo._fetch_one(db, "SELECT * FROM minion_worker_checkpoints WHERE checkpoint_id = ?", (str(checkpoint_id),))
        if row is None:
            raise ValueError(f"unknown checkpoint_id for closure: {checkpoint_id}")
        work_order_id = str(row["work_order_id"] or "")
        milestone_index = int(row["milestone_index"])
        existing = repo._fetch_one(
            db,
            """
            SELECT * FROM minion_worker_checkpoints
            WHERE work_order_id = ? AND milestone_index = ? AND status = 'completed'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (work_order_id, milestone_index),
        )
        if existing is not None:
            return {
                "status": "already_closed",
                "checkpoint_id": str(existing["checkpoint_id"] or ""),
                "work_order_id": work_order_id,
                "milestone_index": milestone_index,
            }
        claim_payload = _loads(row["payload_json"])
        closure_payload = {
            **claim_payload,
            "checkpoint_id": f"chk_{uuid4().hex[:16]}",
            "status": "completed",
            "claimed_checkpoint_id": str(checkpoint_id),
            "review_gate_ref": compact_review_gate_ref(gate_payload),
            "review_gate": gate_payload,
            "summary": str(gate_payload.get("summary") or claim_payload.get("summary") or "checkpoint review passed"),
        }
        repo.ledger.insert_checkpoint(db, work_order_id, closure_payload, str(row["minion_id"] or ""), str(row["run_id"] or ""), created_at)
        repo.ledger.insert_ledger(
            db,
            work_order_id,
            "milestone_closed",
            str(closure_payload["summary"]),
            closure_payload,
            str(row["minion_id"] or ""),
            str(row["run_id"] or ""),
            created_at,
        )
        return {
            "status": "closed",
            "work_order_id": work_order_id,
            "milestone_index": milestone_index,
            "claimed_checkpoint_id": str(checkpoint_id),
            "commit_sha": str(closure_payload.get("commit_sha") or ""),
            "payload": closure_payload,
        }

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
        criteria = _review_target_acceptance_criteria(target_binding)
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
        self._validate_numeric_range_evidence(gate, _review_target_numeric_range_criteria(target_binding))

    def _validate_numeric_range_evidence(self, gate: ReviewGateResult, criteria: list[str]) -> None:
        for violation in _numeric_range_evidence_violations(gate, criteria, require_coverage=True):
            raise ValueError(str(violation.get("summary") or "checkpoint pass review gate evidence violates acceptance criterion"))

    def _validate_pass_gate_findings(self, gate: ReviewGateResult) -> None:
        if gate.verdict != "pass":
            return
        if gate.required_fixes:
            raise ValueError("pass review gate cannot include required_fixes; submit fail/partial until required fixes are resolved")
        blocking = _blocking_pass_findings(gate)
        if not blocking:
            return
        rendered = "; ".join(_finding_title(item) for item in blocking[:3])
        raise ValueError(
            "pass review gate cannot include unresolved contract findings: "
            + rendered
            + ". Submit fail/partial with required_fixes, or move non-contract observations to residual_risk/note with contract_impact=none."
        )


def _review_target_acceptance_criteria(target_binding: dict[str, Any]) -> list[str]:
    criteria: list[str] = []
    seen: set[str] = set()
    sources = [target_binding.get("acceptance_criteria")]
    for source in sources:
        for item in list(source or []):
            criterion = str(item or "").strip()
            if not criterion:
                continue
            key = _coverage_token(criterion)
            if key in seen:
                continue
            seen.add(key)
            criteria.append(criterion)
    return criteria


def _review_target_numeric_range_criteria(target_binding: dict[str, Any]) -> list[str]:
    criteria = list(_review_target_acceptance_criteria(target_binding))
    seen = {_coverage_token(item) for item in criteria}
    source_contract = target_binding.get("source_contract")
    if isinstance(source_contract, dict):
        for key in ("acceptance_criteria", "overall_acceptance_criteria"):
            for item in list(source_contract.get(key) or []):
                token = _coverage_token(item)
                if token and token not in seen:
                    seen.add(token)
                    criteria.append(str(item or "").strip())
        for key in ("instruction", "goal", "task", "summary"):
            for clause in _numeric_range_contract_clauses(str(source_contract.get(key) or "")):
                token = _coverage_token(clause)
                if token and token not in seen:
                    seen.add(token)
                    criteria.append(clause)
    return criteria


_NON_BLOCKING_FINDING_SEVERITIES = {"note", "info", "informational", "nit", "nitpick", "observation"}
_BLOCKING_FINDING_SEVERITIES = {"blocker", "critical", "major", "high", "error", "fail", "failure"}


def _blocking_pass_findings(gate: ReviewGateResult) -> list[dict[str, Any]]:
    blocking: list[dict[str, Any]] = []
    for finding in [dict(item) for item in list(gate.findings or []) if isinstance(item, dict)]:
        severity = _finding_severity(finding)
        contract_impact = _contract_impact_text(finding)
        if contract_impact:
            blocking.append(finding)
            continue
        if severity in _BLOCKING_FINDING_SEVERITIES:
            blocking.append(finding)
            continue
        if _truthy(finding.get("non_blocking")) and severity not in _NON_BLOCKING_FINDING_SEVERITIES:
            blocking.append(finding)
    return blocking


def _finding_severity(finding: dict[str, Any]) -> str:
    return str(finding.get("severity") or "finding").strip().lower()


def _finding_title(finding: dict[str, Any]) -> str:
    return (
        _finding_text(finding, "title", "summary", "message", "description", "area")
        or "review finding"
    )


def _finding_text(finding: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = finding.get(key)
        if isinstance(value, (dict, list, tuple, set)):
            continue
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _contract_impact_text(finding: dict[str, Any]) -> str:
    text = _finding_text(finding, "contract_impact", "contract impact", "impact")
    lowered = text.strip().lower()
    if lowered in {"none", "n/a", "na", "not applicable", "no impact", "no contract impact"}:
        return ""
    if lowered.startswith(("none;", "none,", "n/a;", "n/a,", "not applicable;", "not applicable,")):
        return ""
    return text


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


_TEST_COUNT_RANGE_RE = re.compile(r"\b(\d+)\s*(?:-|to)\s*(\d+)\b", re.IGNORECASE)
_PYTEST_COUNT_RE = re.compile(
    r"\b(?:(\d+)\s+passed|(\d+)\s+tests?\s+collected|collected\s+(\d+)\s+items?)\b",
    re.IGNORECASE,
)


def _test_count_range_from_criterion(criterion: str) -> tuple[int, int] | None:
    text = str(criterion or "")
    lowered = text.lower()
    if "test" not in lowered:
        return None
    match = None
    for candidate in _TEST_COUNT_RANGE_RE.finditer(text):
        if _range_match_is_line_reference(text, candidate):
            continue
        match = candidate
        break
    if match is None:
        return None
    lower = int(match.group(1))
    upper = int(match.group(2))
    if lower > upper:
        lower, upper = upper, lower
    return lower, upper


def _range_match_is_line_reference(text: str, match: re.Match[str]) -> bool:
    prefix = text[max(0, match.start() - 24) : match.start()].lower()
    suffix = text[match.end() : min(len(text), match.end() + 24)].lower()
    return bool(re.search(r"\blines?\s*$", prefix) or re.search(r"^\s*(?:in|of)?\s*lines?\b", suffix))


def _observed_pytest_pass_counts(gate: ReviewGateResult, criterion: str) -> list[int]:
    return _observed_pytest_pass_counts_for_items(gate, criterion, require_coverage=True)


def _numeric_range_evidence_violations(
    gate: ReviewGateResult,
    criteria: list[str],
    *,
    require_coverage: bool,
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    seen: set[tuple[int, int, int]] = set()
    for criterion in criteria:
        bounds = _test_count_range_from_criterion(criterion)
        if bounds is None:
            continue
        lower, upper = bounds
        for count in _observed_pytest_pass_counts_for_items(gate, criterion, require_coverage=require_coverage):
            if lower <= count <= upper:
                continue
            key = (lower, upper, count)
            if key in seen:
                continue
            seen.add(key)
            violations.append(
                {
                    "kind": "numeric_range_violation",
                    "severity": "blocker",
                    "criterion": criterion,
                    "observed_count": count,
                    "required_min": lower,
                    "required_max": upper,
                    "summary": (
                        "checkpoint review gate evidence violates acceptance criterion "
                        f"{criterion!r}: observed {count} passed tests outside required range {lower}-{upper}"
                    ),
                }
            )
    return violations


def _observed_pytest_pass_counts_for_items(gate: ReviewGateResult, criterion: str, *, require_coverage: bool) -> list[int]:
    result: list[int] = []
    bounds = _test_count_range_from_criterion(criterion)
    if bounds is None:
        return result
    lower, upper = bounds
    tool_ref_text_by_id = _tool_ref_text_by_id(gate)
    for item in [*gate.commands_run, *gate.evidence, *gate.api_evidence]:
        if not isinstance(item, dict):
            continue
        text = _evidence_text_for_numeric_checks(item)
        evidence_ref_id = str(item.get("evidence_ref_id") or "").strip()
        if evidence_ref_id and tool_ref_text_by_id.get(evidence_ref_id):
            text = "\n".join(part for part in (text, tool_ref_text_by_id[evidence_ref_id]) if part)
        matches = list(_PYTEST_COUNT_RE.finditer(text))
        if not matches:
            continue
        if require_coverage and not _item_relevant_to_test_count_range(item, criterion, lower, upper):
            continue
        for match in matches:
            for group in match.groups():
                if group:
                    result.append(int(group))
                    break
    return result


def _tool_ref_text_by_id(gate: ReviewGateResult) -> dict[str, str]:
    metadata = dict(gate.metadata or {})
    result: dict[str, str] = {}
    for ref in list(metadata.get("tool_evidence_refs") or []):
        if not isinstance(ref, dict):
            continue
        evidence_ref_id = str(ref.get("evidence_ref_id") or "").strip()
        if not evidence_ref_id:
            continue
        result[evidence_ref_id] = _evidence_text_for_numeric_checks(ref)
    return result


def _item_relevant_to_test_count_range(item: dict[str, Any], criterion: str, lower: int, upper: int) -> bool:
    criterion_token = _coverage_token(criterion)
    item_tokens = _coverage_tokens_from_item(item)
    if criterion_token in item_tokens:
        return True
    for token in item_tokens:
        if _test_count_range_from_criterion(token) == (lower, upper):
            return True
    command = str(item.get("command") or item.get("cmd") or item.get("name") or "").lower()
    if "pytest" in command:
        return True
    text = "\n".join((_coverage_text_from_item(item), _evidence_text_for_numeric_checks(item))).lower()
    return "pytest" in text and "test" in text


def _annotate_detected_contract_violations(payload: dict[str, Any], violations: list[dict[str, Any]]) -> None:
    metadata = dict(payload.get("metadata") or {})
    existing = [dict(item) for item in list(metadata.get("detected_contract_violations") or []) if isinstance(item, dict)]
    existing.extend(dict(item) for item in violations)
    metadata["detected_contract_violations"] = existing
    payload["metadata"] = metadata
    if str(payload.get("verdict") or "").strip().lower() == "pass":
        return
    findings = [dict(item) for item in list(payload.get("findings") or []) if isinstance(item, dict)]
    existing_text = "\n".join(str(item.get("summary") or item.get("message") or "") for item in findings).lower()
    for violation in violations:
        summary = str(violation.get("summary") or "").strip()
        if not summary or summary.lower() in existing_text:
            continue
        findings.append(
            {
                "severity": "blocker",
                "summary": summary,
                "contract_impact": str(violation.get("criterion") or ""),
                "suggested_fix": "Adjust the implementation or tests so observed evidence satisfies the binding numeric range.",
                "source": "system_detected",
            }
        )
    payload["findings"] = findings


def _evidence_text_for_numeric_checks(item: dict[str, Any]) -> str:
    fields = (
        "output_summary",
        "output summary",
        "outputSummary",
        "summary",
        "description",
        "message",
        "detail",
        "evidence",
        "text",
        "llm_text",
        "stdout_preview",
        "stderr_preview",
        "output",
    )
    parts = [str(item.get(key) or "") for key in fields if str(item.get(key) or "").strip()]
    return "\n".join(parts)


def _coverage_text_from_item(item: dict[str, Any]) -> str:
    if not isinstance(item, dict):
        return ""
    raw_values: list[Any] = []
    for key in ("covers", "coverage", "acceptance_criteria", "acceptance_criteria_refs", "acceptance_refs"):
        if key in item:
            raw_values.append(item.get(key))
    if isinstance(item.get("coverage"), dict):
        raw_values.extend(item["coverage"].values())
    parts: list[str] = []
    for value in raw_values:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (int, float)):
            parts.append(str(int(value)))
        elif isinstance(value, dict):
            parts.extend(str(nested) for nested in value.values() if str(nested or "").strip())
        elif isinstance(value, (list, tuple, set)):
            parts.extend(str(nested) for nested in value if str(nested or "").strip())
    return "\n".join(parts)


def _numeric_range_contract_clauses(text: str) -> list[str]:
    clauses: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip(" -\t")
        if _test_count_range_from_criterion(line) is None:
            continue
        clauses.append(_compact_numeric_range_clause(line))
    if clauses:
        return clauses
    for match in _TEST_COUNT_RANGE_RE.finditer(str(text or "")):
        start = max(0, match.start() - 120)
        end = min(len(text), match.end() + 160)
        snippet = str(text[start:end]).replace("\n", " ").strip(" -\t,.;")
        if _test_count_range_from_criterion(snippet) is not None:
            clauses.append(_compact_numeric_range_clause(snippet))
    return clauses


def _compact_numeric_range_clause(text: str, *, limit: int = 260) -> str:
    clause = " ".join(str(text or "").split())
    if len(clause) <= limit:
        return clause
    match = _TEST_COUNT_RANGE_RE.search(clause)
    if not match:
        return clause[:limit].rstrip()
    half = limit // 2
    start = max(0, match.start() - half)
    end = min(len(clause), start + limit)
    start = max(0, end - limit)
    return clause[start:end].strip(" -\t,.;")


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


def _write_active_gate_todo_locked(repo: Any, db: Any, work_order_id: str, todo: dict[str, Any]) -> None:
    normalized = str(work_order_id or "").strip()
    if not normalized:
        return
    row = repo._fetch_one(db, "SELECT metadata_json FROM minion_work_orders WHERE work_order_id = ?", (normalized,))
    if row is None:
        return
    metadata = _loads(row["metadata_json"])
    metadata["active_gate_todo"] = dict(todo or {})
    db.execute(
        "UPDATE minion_work_orders SET metadata_json = ?, updated_at = ? WHERE work_order_id = ?",
        (_json(metadata), utc_now(), normalized),
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
