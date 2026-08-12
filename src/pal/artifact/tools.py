from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pal.artifact.service import ArtifactManager
from pal.execution.contracts import CapabilityResult
from pal.shared import RuntimeStatus
from pal.shared.result_rendering import render_titled_structured_for_llm


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
