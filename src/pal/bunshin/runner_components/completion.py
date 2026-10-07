from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from pal.shared import BunshinInvocationPack
from pal.bunshin.runner_components.artifacts import Artifacts


@dataclass
class Completion:
    artifacts: Artifacts
    pack: BunshinInvocationPack
    runtime_root: Path
    manager_submission_receipt_observed: bool = field(default=False, init=False, repr=False)

    def completion_gate_progress_marker(self) -> str:
        """Observe semantic state, excluding RPC counts and ledger bookkeeping.

        Read failures propagate: unavailable evidence must not be mistaken for
        unchanged work. The caller runs this observation outside the event loop.
        """
        from pal.bunshin.work_items import read_work_items

        workspace = dict(self.pack.workspace or {})
        binding = dict((self.pack.metadata or {}).get("bunshin_v2") or {})
        binding.update(dict(workspace.get("bunshin_v2") or {}))
        workspace.update(
            runtime_root=str(self.runtime_root),
            invocation_id=self.pack.invocation_id,
            bunshin_v2=binding,
        )
        items = []
        if binding.get("role"):
            ledger = read_work_items(workspace)
            items = [
                {key: item.get(key) for key in ("kind", "summary", "status", "finding")}
                for item in ledger["items"]
            ]
        # Observe only explicitly bound/registered outputs, never scan the repo.
        paths = {
            str(item.get("stage_path") or item.get("path") or "")
            for item in self.artifacts.produced_artifacts
        }
        if workspace.get("architect_path"):
            paths.add(str(workspace["architect_path"]))
        artifacts = []
        for raw_path in sorted(paths - {""}):
            path = Path(raw_path)
            try:
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
            except FileNotFoundError:
                digest = "missing"
            artifacts.append((raw_path, digest))
        payload: dict[str, Any] = {
            "items": sorted(items, key=lambda item: json.dumps(item, sort_keys=True)),
            "artifacts": artifacts,
        }
        if str(binding.get("role") or "") == "verifier":
            from pal.bunshin.review_findings import empty_review_draft
            from pal.bunshin.submission_drafts import SubmissionDraftContext, SubmissionDraftStore
            from pal.bunshin.verification_readiness import verification_corpus_snapshot

            context = SubmissionDraftContext.from_workspace(workspace, draft_kind="verification")
            snapshot = SubmissionDraftStore(self.runtime_root).read(context, seed=empty_review_draft())
            cases = dict(dict(snapshot.payload.get("evidence") or {}).get("cases") or {})
            payload["verification"] = {
                "corpus": verification_corpus_snapshot(workspace),
                "cases": _unique_progress_values([
                    _verification_case_progress(case) for case in cases.values()
                ]),
            }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def missing_completion_evidence_feedback(self) -> str:
        primary = self.required_primary_artifact_name()
        target = f"the required primary artifact {primary!r}" if primary else "required submit evidence"
        submit_tool = {
            "architect.yaml": "submit_contract",
            "contract_review.json": "submit_review",
            "coder_report.json": "submit_candidate",
            "producer_report.json": "submit_candidate",
            "verification_plan.json": "submit_verification",
            "verification_submission.json": "a semantic verification outcome tool",
            "standalone_review.json": "submit_review",
        }.get(primary, "the bound role-specific submit tool")
        return (
            f"Completion gate rejected the final response because {target} is absent. "
            f"Continue in this same invocation, correct any prior submit rejection, and call {submit_tool}. "
            "Do not answer with another completion summary until that submit call succeeds."
        )

    def required_primary_artifact_name(self) -> str:
        output_policy = dict((self.pack.workspace or {}).get("output_policy") or {})
        if not output_policy:
            output_policy = dict((self.pack.resolved_profile or {}).get("effective_output_policy") or {})
        return str(output_policy.get("primary_artifact") or "").strip()

    def completion_evidence_present(self) -> bool:
        if self.manager_submission_receipt_required():
            return self.manager_submission_receipt_present()
        return self.required_primary_artifact_present()

    def artifact_completion_evidence_present(self) -> bool:
        if self.manager_submission_receipt_required():
            return self.manager_submission_receipt_present()
        return self.required_primary_artifact_present()

    def manager_submission_receipt_required(self) -> bool:
        metadata = dict(self.pack.metadata or {})
        bunshin_v2 = dict(metadata.get("bunshin_v2") or {})
        return bool(bunshin_v2.get("submission_receipt_required"))

    def manager_submission_receipt_present(self) -> bool:
        if self.manager_submission_receipt_observed:
            return True
        try:
            from pal.bunshin.role_gateway_client import role_gateway_client_from_env

            client = role_gateway_client_from_env(self.runtime_root)
            if client is None:
                return False
            status = client.request_sync("submission_status", {})
        except Exception:
            return False
        self.manager_submission_receipt_observed = bool(status.get("recorded"))
        return self.manager_submission_receipt_observed

    def required_primary_artifact_present(self) -> bool:
        required = self.required_primary_artifact_name()
        if not required:
            return bool(self.artifacts.produced_artifacts)
        for artifact in self.artifacts.produced_artifacts:
            if str(artifact.get("role") or "").strip().lower() != "primary":
                continue
            candidates = {
                str(artifact.get("relative_path") or "").strip(),
                str(artifact.get("requested_relative_path") or "").strip(),
                Path(str(artifact.get("path") or "")).name,
            }
            if required in candidates:
                return True
        return False

    def completion_evidence_fallback_text(self, error_text: str) -> str:
        summary = str(error_text or "LLM final report generation failed").strip()
        if len(summary) > 500:
            summary = summary[:497].rstrip() + "..."
        return (
            "Milestone produced completion evidence through capability calls.\n\n"
            "The final report generation failed after evidence was produced, so the runner is "
            "using the verified workspace evidence as the completion signal.\n\n"
            f"LLM error: {summary}"
        )

    def restore_receipt(self, observed: bool) -> None:
        self.manager_submission_receipt_observed = observed


def _unique_progress_values(values: list[Any]) -> list[Any]:
    """Canonicalize unordered semantic facts without counting duplicates."""
    encoded = {json.dumps(value, sort_keys=True, ensure_ascii=False): value for value in values}
    return [encoded[key] for key in sorted(encoded)]


def _verification_case_progress(case: Mapping[str, Any]) -> dict[str, Any]:
    """Observe evidence, not operation IDs, versions or replay metadata.

    Content-addressed result hashes carry the actual stdout/stderr changes.
    Request fingerprints are deliberately absent: they include unrelated repo
    state and arguments, neither of which establishes new verification work.
    """
    result = {
        key: case.get(key)
        for key in (
            "name", "case_kind", "command", "status",
            "exit_code", "input_fingerprint",
        )
    }
    for key in ("obligation_tags", "expected_exit_codes", "invariants"):
        result[key] = _unique_progress_values(list(case.get(key) or []))
    for key, fields in (
        ("locations", ("path", "symbol", "section")),
        ("requirements", ("section", "requirement")),
    ):
        result[key] = _unique_progress_values([
            {field: item.get(field) for field in fields}
            for item in list(case.get(key) or [])
        ])
    environment = dict(case.get("environment") or {})
    result["environment"] = {
        key: environment.get(key)
        for key in ("cwd", "runner", "workspace_root", "primary_language", "environment_fingerprint")
    }
    for key in ("stdout_ref", "stderr_ref"):
        result[key] = str(dict(case.get(key) or {}).get("sha256") or "")
    return result
