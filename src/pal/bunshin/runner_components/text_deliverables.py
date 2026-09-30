from __future__ import annotations
from pal.bunshin.runner_components.result_values import _strip_single_json_code_fence
import json
from dataclasses import dataclass
from pal.bunshin.workspace_tools import _write_bunshin_artifact
from pal.shared import BunshinInvocationPack
from pal.bunshin.runner_components.artifacts import Artifacts
from pal.bunshin.runner_components.prompt_context import PromptContext


@dataclass
class TextDeliverables:
    artifacts: Artifacts
    prompt_context: PromptContext
    bunshin_id: str
    pack: BunshinInvocationPack
    run_id: str

    async def persist_text_deliverable_if_needed(
        self,
        final_text: str,
        *,
        partial: bool = False,
        truncation_reason: str = "",
    ) -> None:
        text = str(final_text or "").strip()
        if not text or self.artifacts.produced_artifacts:
            return
        if not str((self.pack.workspace or {}).get("artifact_dir") or "").strip():
            return
        suffix = ".partial" if partial else ""
        title = str(self.pack.instruction or self.pack.goal or "Bunshin invocation result")
        reviewer_json = "" if partial else self.reviewer_json_deliverable(text)
        if reviewer_json:
            artifact = _write_bunshin_artifact(
                self.pack.workspace,
                {
                    "relative_path": "review_report.json",
                    "title": "Review report",
                    "role": "primary",
                    "mime_type": "application/json",
                    "content": reviewer_json,
                },
            )
            self.artifacts.record_produced_artifact(artifact)
            return
        header_lines = [
            f"# {title}",
            "",
            f"- invocation_id: {self.pack.invocation_id}",
            f"- bunshin_id: {self.bunshin_id}",
            f"- run_id: {self.run_id}",
        ]
        if partial:
            header_lines.extend(
                [
                    f"- status: blocked",
                    f"- truncation_reason: {str(truncation_reason or 'unknown')}",
                    "",
                    "This is partial LLM output saved for diagnosis. It must not be treated as a completed deliverable.",
                ]
            )
        header_lines.append("")
        artifact = _write_bunshin_artifact(
            self.pack.workspace,
            {
                "relative_path": self.default_text_artifact_path(suffix=suffix, partial=partial),
                "title": self.default_text_artifact_title(title, partial=partial),
                "role": "primary" if not partial else "partial",
                "mime_type": "text/markdown",
                "content": "\n".join([*header_lines, text, ""]),
            },
        )
        self.artifacts.record_produced_artifact(artifact)

    def default_text_artifact_path(self, *, suffix: str = "", partial: bool = False) -> str:
        if self.is_reviewer_profile() and not partial:
            return "review_report.md"
        return f"invocation_{self.safe_path_part(self.pack.bunshin_profile)}{suffix}.md"

    def default_text_artifact_title(self, title: str, *, partial: bool = False) -> str:
        if partial:
            return f"{title} (partial truncated output)"
        if self.is_reviewer_profile():
            return "Review report"
        return title

    def is_reviewer_profile(self) -> bool:
        output_policy = self.prompt_context.policy_from_workspace_or_profile("output_policy")
        primary_artifact = str(output_policy.get("primary_artifact") or "").strip().lower()
        output_types = {str(item or "").strip() for item in list(output_policy.get("allowed_output_types") or [])}
        return (
            "review" in primary_artifact
            or any("Review" in item or "Verification" in item for item in output_types)
        )

    def reviewer_json_deliverable(self, text: str) -> str:
        if not self.is_reviewer_profile():
            return ""
        candidate = _strip_single_json_code_fence(text)
        if not candidate.startswith("{"):
            return ""
        try:
            parsed, end = json.JSONDecoder().raw_decode(candidate)
        except json.JSONDecodeError:
            return ""
        if not isinstance(parsed, dict):
            return ""
        if candidate[end:].strip():
            return ""
        return candidate

    def truncated_output_blocked_summary(self, finish_reason: str) -> str:
        reason = str(finish_reason or "unknown").strip() or "unknown"
        primary = dict(self.artifacts.artifact_payload().get("primary_artifact") or {})
        artifact_path = str(primary.get("path") or primary.get("relative_path") or "").strip()
        saved = f" Partial output was saved to {artifact_path}." if artifact_path else ""
        scope = "contract invocation"
        return (
            f"LLM output was truncated before the bunshin completed the {scope} "
            f"(finish_reason={reason}). Treat this {scope} as blocked.{saved} "
            "For long deliverables, write the full result as an artifact/file and keep the final reply short."
        )

    @staticmethod
    def safe_path_part(value: str) -> str:
        normalized = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(value or "").strip())
        return normalized.strip("_")[:80] or "bunshin"
