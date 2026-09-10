from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import mimetypes
from typing import Any

from pal.artifact.contracts import ARTIFACT_KIND_IMAGE
from pal.artifact.service import ArtifactManager
from pal.execution.contracts import CapabilityResult
from pal.shared import RuntimeStatus
from pal.shared.result_rendering import render_titled_structured_for_llm
from pal.shared.tool_protocol import ToolContextMessageIR


def _scope_from_runtime(runtime: Any, turn_id: str | None) -> str:
    registry = getattr(runtime, "provider_registry", {}) if runtime is not None else {}
    turn_io = registry.get("core:turn_io") if isinstance(registry, dict) else None
    scope_for_turn = getattr(turn_io, "artifact_scope_for_turn", None)
    if callable(scope_for_turn):
        scope = scope_for_turn(turn_id)
        if scope:
            return str(scope)
    raise KeyError("artifact_scope_unavailable")


def _result(status: str, title: str, structured: dict[str, Any], text: str = "") -> CapabilityResult:
    return CapabilityResult(
        status=status,
        text=text or title,
        structured=structured,
        llm_text=render_titled_structured_for_llm(title, structured),
    )


def _key_error_reason(exc: KeyError) -> str:
    """Expose stable machine-readable reasons without KeyError's repr quotes."""

    return str(exc.args[0]) if exc.args else "artifact_not_found"


@dataclass
class ArtifactImportTool:
    service: ArtifactManager

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        try:
            scope_key = _scope_from_runtime(kwargs.get("runtime"), kwargs.get("turn_id"))
            raw_path = str(args.get("path") or "").strip()
            if not raw_path:
                return _result(RuntimeStatus.INVALID, "Artifact import failed", {"reason": "path_required"})
            path = Path(raw_path).expanduser().resolve(strict=True)
            if not path.is_file():
                return _result(RuntimeStatus.INVALID, "Artifact import failed", {"reason": "not_regular_file"})
            if path.stat().st_size > self.service.policy.limits.max_original_bytes:
                return _result(RuntimeStatus.INVALID, "Artifact import failed", {"reason": "artifact_too_large"})
            kind = self.service.processor_registry.resolve_kind(
                mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                file_name=path.name,
            )
            if kind == ARTIFACT_KIND_IMAGE:
                runtime = kwargs.get("runtime")
                turn_io = getattr(runtime, "provider_registry", {}).get("core:turn_io")
                resolve = getattr(turn_io, "llm_capabilities_for_turn", None)
                facts = resolve(kwargs.get("turn_id")) if callable(resolve) else {}
                if not isinstance(facts, dict) or not facts.get("supports_vision"):
                    return _result(RuntimeStatus.UNSUPPORTED, "Image import unavailable", {
                        "reason": "vision_not_supported" if isinstance(facts, dict) and "supports_vision" in facts else "vision_capability_unavailable",
                        "next_step": (
                            "Use search_tools to find an OCR or image-analysis tool that explicitly accepts local paths, "
                            "then pass this source path using that tool's schema. OCR provides text, not full visual inspection. "
                            "If no suitable tool exists, explain the missing capability and how to select a vision-capable endpoint. "
                            "Do not retry artifact_import unchanged or claim to have inspected pixels."
                        ),
                    })
                from PIL import Image

                try:
                    with Image.open(path) as source_image:
                        source_image.verify()
                except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
                    return _result(RuntimeStatus.UNSUPPORTED, "Artifact import failed", {"reason": "artifact_processing_failed"})
            ref = self.service.register_ingested(
                path,
                scope_key=scope_key,
                turn_id=str(kwargs.get("turn_id") or ""),
                source_channel="local_file",
            )
            payload = {"artifact": ref.to_dict(), "artifact_id": ref.artifact_id}
            if ref.status == "failed":
                payload["reason"] = "artifact_processing_failed"
                return _result(RuntimeStatus.UNSUPPORTED, "Artifact import failed", payload)
            return CapabilityResult(
                status=RuntimeStatus.OK,
                text="Local file imported",
                structured=payload,
                llm_text=render_titled_structured_for_llm("Local file imported", payload),
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
            return _result(RuntimeStatus.NOT_FOUND, "Artifact import failed", {"reason": _key_error_reason(exc)})
        except FileNotFoundError:
            return _result(RuntimeStatus.NOT_FOUND, "Artifact import failed", {"reason": "source_not_found"})
        except (OSError, RuntimeError, ValueError) as exc:
            return _result(RuntimeStatus.ERROR, "Artifact import failed", {"reason": str(exc)})


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
            return _result(RuntimeStatus.NOT_FOUND, "Artifact list failed", {"reason": _key_error_reason(exc)})


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
            return _result(RuntimeStatus.NOT_FOUND, "Artifact info failed", {"reason": _key_error_reason(exc)})


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
        except KeyError as exc:
            return _result(RuntimeStatus.NOT_FOUND, "Artifact read failed", {"reason": _key_error_reason(exc)})


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
                time_hint=str(args.get("time_hint") or "recent"),
                limit=_optional_int(args.get("limit")) or 5,
            )
            structured = {"results": [item.to_dict() for item in results], "ttl_refreshed": False}
            return _result(RuntimeStatus.OK, "Artifact search results", structured, text=f"{len(results)} artifact candidate(s)")
        except KeyError as exc:
            return _result(RuntimeStatus.NOT_FOUND, "Artifact search failed", {"reason": _key_error_reason(exc)})


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
            return _result(RuntimeStatus.NOT_FOUND, "Artifact select failed", {"reason": _key_error_reason(exc)})


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
            return _result(RuntimeStatus.NOT_FOUND, "Artifact content search failed", {"reason": _key_error_reason(exc)})


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
            info = self.service.info(artifact_id, scope_key)
            structured = {"reason": "needs_transcription", "artifact": info.get("artifact", {})}
            return _result(RuntimeStatus.UNSUPPORTED, "Artifact transcription needed", structured)
        except KeyError as exc:
            return _result(RuntimeStatus.NOT_FOUND, "Artifact transcription failed", {"reason": _key_error_reason(exc)})


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None
