from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import mimetypes
from typing import TYPE_CHECKING, Any

from pal.artifact.contracts import ARTIFACT_KIND_IMAGE
from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import EffectOutcome, EffectReceipt
from pal.execution.turn_io_contracts import TurnIOHost
from pal.foundation.diagnostics import exception_report
from pal.shared import RuntimeStatus
from pal.shared.result_rendering import render_titled_structured_for_llm
from pal.shared.tool_protocol import ToolContextMessageIR

if TYPE_CHECKING:
    from pal.artifact.service import ArtifactManager


def _scope_from_runtime(runtime: TurnIOHost | None, turn_id: str | None) -> str:
    turn_io = runtime.turn_io if runtime is not None else None
    if turn_io is not None:
        scope = turn_io.artifact_scope_for_turn(turn_id)
        if scope:
            return str(scope)
    raise KeyError("artifact_scope_unavailable")


def _result(status: str, title: str, structured: dict[str, Any], text: str = "", *,
            effect: EffectOutcome | None = None, recovery_hint: str = "") -> CapabilityResult:
    if status != RuntimeStatus.OK:
        structured = dict(structured)
        structured.setdefault("error_code", structured.get("reason") or "artifact_failed")
    if effect == EffectOutcome.NOT_STARTED:
        structured = {**structured, "kind": "rejected", "retry": "correct_input"}
    return CapabilityResult(
        status=status,
        text=text or title,
        structured=structured,
        llm_text=render_titled_structured_for_llm(title, structured),
        effect_receipt=EffectReceipt(outcome=effect) if effect is not None else None,
        recovery_hint=recovery_hint,
    )


def _key_error_reason(exc: KeyError) -> str:
    """Expose stable machine-readable reasons without KeyError's repr quotes."""

    return str(exc.args[0]) if exc.args else "artifact_not_found"


@dataclass
class ArtifactImportTool:
    service: ArtifactManager

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        started = False
        try:
            scope_key = _scope_from_runtime(kwargs.get("runtime"), kwargs.get("turn_id"))
            raw_path = str(args.get("path") or "").strip()
            if not raw_path:
                return _result(RuntimeStatus.INVALID, "Artifact import failed", {"reason": "path_required"}, effect=EffectOutcome.NOT_STARTED)
            path = Path(raw_path).expanduser().resolve(strict=True)
            if not path.is_file():
                return _result(RuntimeStatus.INVALID, "Artifact import failed", {"reason": "not_regular_file"}, effect=EffectOutcome.NOT_STARTED)
            if path.stat().st_size > self.service.policy.limits.max_original_bytes:
                return _result(RuntimeStatus.INVALID, "Artifact import failed", {"reason": "artifact_too_large"}, effect=EffectOutcome.NOT_STARTED)
            kind = self.service.processor_registry.resolve_kind(
                mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                file_name=path.name,
            )
            if kind == ARTIFACT_KIND_IMAGE:
                from PIL import Image

                try:
                    with Image.open(path) as source_image:
                        source_image.verify()
                except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
                    return _result(RuntimeStatus.UNSUPPORTED, "Artifact import failed",
                        {"reason": "artifact_processing_failed", "error": exception_report(exc)}, effect=EffectOutcome.NOT_STARTED)
            started = True
            ref = self.service.register_ingested(
                kwargs.get("stored_source") or path,
                scope_key=scope_key,
                turn_id=str(kwargs.get("turn_id") or ""),
                source_channel=str(kwargs.get("source_channel") or "local_file"),
            )
            payload = {"artifact": ref.to_dict(), "artifact_id": ref.artifact_id}
            if ref.status == "failed":
                payload["reason"] = "artifact_processing_failed"
                return _result(RuntimeStatus.UNSUPPORTED, "Artifact import failed", payload,
                    effect=EffectOutcome.APPLIED,
                    recovery_hint="The artifact record was stored, but processing failed. Inspect its diagnostic before importing again.")
            return CapabilityResult(
                status=RuntimeStatus.OK,
                text="Local file imported",
                structured=payload,
                llm_text=render_titled_structured_for_llm("Local file imported", payload),
                recovery_hint=("Artifact processing is partial. Inspect its notes and available representations."
                               if ref.status == "partial" else ""),
                context_messages=(ToolContextMessageIR(
                    content=(
                        "<runtime_context_update>Local file imported into the current conversation. "
                        "The artifact reference follows; inspect image pixels only when they are attached inline "
                        "for a vision-capable model. Import alone is not visual inspection.</runtime_context_update>"
                    ),
                    semantic_kind="artifact_import",
                    artifact_ids=(ref.artifact_id,),
                ),),
            )
        except KeyError as exc:
            return _result(RuntimeStatus.NOT_FOUND, "Artifact import failed",
                {"reason": _key_error_reason(exc), "error": exception_report(exc)},
                effect=EffectOutcome.UNKNOWN if started else EffectOutcome.NOT_STARTED)
        except FileNotFoundError as exc:
            return _result(RuntimeStatus.NOT_FOUND, "Artifact import failed",
                {"reason": "source_not_found", "error": exception_report(exc)},
                effect=EffectOutcome.UNKNOWN if started else EffectOutcome.NOT_STARTED)
        except (OSError, RuntimeError, ValueError, ImportError) as exc:
            return _result(RuntimeStatus.ERROR, "Artifact import failed",
                {"reason": "artifact_import_failed", "error": exception_report(exc)},
                effect=EffectOutcome.UNKNOWN if started else EffectOutcome.NOT_STARTED)


@dataclass
class ArtifactListTool:
    service: ArtifactManager

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        _ = args
        return _result(RuntimeStatus.INVALID, "Artifact list unavailable", {"reason": "async_required"})

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        try:
            scope_key = _scope_from_runtime(kwargs.get("runtime"), kwargs.get("turn_id"))
            refs = self.service.list_hot(scope_key, query_context=str(args.get("query_context") or ""))
            structured = {"artifacts": [ref.to_dict() for ref in refs]}
            return _result(RuntimeStatus.OK, "Visible artifacts", structured, text=f"{len(refs)} artifact(s)")
        except KeyError as exc:
            return _result(RuntimeStatus.NOT_FOUND, "Artifact list failed", {"reason": _key_error_reason(exc), "error": exception_report(exc)})


@dataclass
class ArtifactInfoTool:
    service: ArtifactManager

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        _ = args
        return _result(RuntimeStatus.INVALID, "Artifact info unavailable", {"reason": "async_required"})

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        try:
            scope_key = _scope_from_runtime(kwargs.get("runtime"), kwargs.get("turn_id"))
            structured = self.service.info(str(args.get("artifact_id") or ""), scope_key)
            return _result(RuntimeStatus.OK, "Artifact info", structured)
        except KeyError as exc:
            return _result(RuntimeStatus.NOT_FOUND, "Artifact info failed", {"reason": _key_error_reason(exc), "error": exception_report(exc)})


@dataclass
class ArtifactReadTool:
    service: ArtifactManager

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        _ = args
        return _result(RuntimeStatus.INVALID, "Artifact read unavailable", {"reason": "async_required"})

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        try:
            scope_key = _scope_from_runtime(kwargs.get("runtime"), kwargs.get("turn_id"))
            result = self.service.read(
                str(args.get("artifact_id") or ""),
                scope_key,
                representation=str(args.get("representation") or "auto"),
                page=_optional_int(args.get("page")),
                chunk=_optional_int(args.get("chunk")),
                max_chars=_optional_int(args.get("max_chars")),
            )
            status = RuntimeStatus.OK if result.ok else RuntimeStatus.UNSUPPORTED
            return _result(status, "Artifact read", result.to_dict(), text=result.text)
        except ValueError as exc:
            return _result(RuntimeStatus.INVALID, "Artifact read failed", {"reason": str(exc), "error": exception_report(exc)})
        except KeyError as exc:
            return _result(RuntimeStatus.NOT_FOUND, "Artifact read failed", {"reason": _key_error_reason(exc), "error": exception_report(exc)})


@dataclass
class ArtifactSearchTool:
    service: ArtifactManager

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        _ = args
        return _result(RuntimeStatus.INVALID, "Artifact search unavailable", {"reason": "async_required"})

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        try:
            scope_key = _scope_from_runtime(kwargs.get("runtime"), kwargs.get("turn_id"))
            results = self.service.artifact_search(
                scope_key,
                query=str(args.get("query") or ""),
                kind=str(args.get("kind") or "") or None,
                limit=_optional_int(args.get("limit")) or 5,
            )
            structured = {"results": [item.to_dict() for item in results], "ttl_refreshed": False}
            return _result(RuntimeStatus.OK, "Artifact search results", structured, text=f"{len(results)} artifact candidate(s)")
        except KeyError as exc:
            return _result(RuntimeStatus.NOT_FOUND, "Artifact search failed", {"reason": _key_error_reason(exc), "error": exception_report(exc)})


@dataclass
class ArtifactSelectTool:
    service: ArtifactManager

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        _ = args
        return _result(RuntimeStatus.INVALID, "Artifact select unavailable", {"reason": "async_required"})

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        try:
            scope_key = _scope_from_runtime(kwargs.get("runtime"), kwargs.get("turn_id"))
            structured = self.service.select(str(args.get("artifact_id") or ""), scope_key)
            return _result(RuntimeStatus.OK, "Artifact selected", structured)
        except KeyError as exc:
            return _result(RuntimeStatus.NOT_FOUND, "Artifact select failed", {"reason": _key_error_reason(exc), "error": exception_report(exc)})


@dataclass
class ArtifactContentSearchTool:
    service: ArtifactManager

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        _ = args
        return _result(RuntimeStatus.INVALID, "Artifact content search unavailable", {"reason": "async_required"})

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        try:
            scope_key = _scope_from_runtime(kwargs.get("runtime"), kwargs.get("turn_id"))
            results = self.service.content_search(
                str(args.get("artifact_id") or ""),
                scope_key,
                query=str(args.get("query") or ""),
                top_k=_optional_int(args.get("top_k")) or 5,
                max_chars_per_result=_optional_int(args.get("max_chars_per_result")) or 2000,
            )
            structured = {
                "results": [item.to_dict() for item in results],
                "ttl_refreshed": self.service.writable,
            }
            return _result(RuntimeStatus.OK, "Artifact content search results", structured, text=f"{len(results)} content match(es)")
        except KeyError as exc:
            return _result(RuntimeStatus.NOT_FOUND, "Artifact content search failed", {"reason": _key_error_reason(exc), "error": exception_report(exc)})


@dataclass
class ArtifactTranscribeTool:
    service: ArtifactManager

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        _ = args
        return _result(RuntimeStatus.INVALID, "Artifact transcription unavailable", {"reason": "async_required"})

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        try:
            scope_key = _scope_from_runtime(kwargs.get("runtime"), kwargs.get("turn_id"))
            artifact_id = str(args.get("artifact_id") or "")
            transcript = self.service.read(artifact_id, scope_key, representation="transcript")
            if transcript.ok:
                return _result(RuntimeStatus.OK, "Artifact transcript", transcript.to_dict(), text=transcript.text)
            structured = {"reason": "needs_transcription", "artifact": transcript.metadata,
                          "read_result": transcript.to_dict()}
            return _result(RuntimeStatus.UNSUPPORTED, "Artifact transcription needed", structured)
        except KeyError as exc:
            return _result(RuntimeStatus.NOT_FOUND, "Artifact transcription failed", {"reason": _key_error_reason(exc), "error": exception_report(exc)})


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None
