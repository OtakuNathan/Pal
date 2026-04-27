from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from pal.artifact.service import ArtifactManager
from pal.execution.contracts import CapabilityResult
from pal.shared import RuntimeStatus
from pal.shared.result_rendering import render_titled_structured_for_llm


ARTIFACT_ID_SCHEMA = {"type": "string", "description": "Artifact id from Available Artifacts or op_artifact_search."}
_OBJECT_RESULT_SCHEMA = {"type": "object"}


ARTIFACT_TOOL_ARGS_SCHEMAS: dict[str, dict[str, Any]] = {
    "op_artifact_list": {
        "type": "object",
        "properties": {"query_context": {"type": "string"}},
    },
    "op_artifact_info": {
        "type": "object",
        "properties": {"artifact_id": ARTIFACT_ID_SCHEMA},
        "required": ["artifact_id"],
    },
    "op_artifact_read": {
        "type": "object",
        "properties": {
            "artifact_id": ARTIFACT_ID_SCHEMA,
            "representation": {
                "type": "string",
                "enum": ["auto", "text", "page_text", "chunk_text", "transcript", "metadata"],
                "default": "auto",
            },
            "page": {"type": "integer"},
            "chunk": {"type": "integer"},
            "max_chars": {"type": "integer", "default": 12000},
        },
        "required": ["artifact_id"],
    },
    "op_artifact_search": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "kind": {"type": "string"},
            "time_hint": {"type": "string", "default": "recent"},
            "limit": {"type": "integer", "default": 5},
        },
    },
    "op_artifact_select": {
        "type": "object",
        "properties": {"artifact_id": ARTIFACT_ID_SCHEMA},
        "required": ["artifact_id"],
    },
    "op_artifact_content_search": {
        "type": "object",
        "properties": {
            "artifact_id": ARTIFACT_ID_SCHEMA,
            "query": {"type": "string"},
            "top_k": {"type": "integer", "default": 5},
            "max_chars_per_result": {"type": "integer", "default": 2000},
        },
        "required": ["artifact_id", "query"],
    },
    "op_artifact_transcribe": {
        "type": "object",
        "properties": {"artifact_id": ARTIFACT_ID_SCHEMA},
        "required": ["artifact_id"],
    },
}


def artifact_args_schema(tool_name: str) -> dict[str, Any]:
    return deepcopy(ARTIFACT_TOOL_ARGS_SCHEMAS.get(tool_name) or {"type": "object", "properties": {}})


def artifact_result_schema(_: str) -> dict[str, Any]:
    return deepcopy(_OBJECT_RESULT_SCHEMA)


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


@dataclass
class ArtifactListTool:
    service: ArtifactManager
    name: str = "op_artifact_list"
    display_name: str = "List Artifacts"
    family: str = "artifact"
    description: str = "List short-lived conversation artifacts visible to the current turn."
    tags: tuple[str, ...] = ("artifact", "attachment", "list")
    keywords: tuple[str, ...] = ("artifact", "attachment", "file", "recent")
    args_schema: dict[str, Any] = field(default_factory=lambda: artifact_args_schema("op_artifact_list"))
    result_schema: dict[str, Any] = field(default_factory=lambda: artifact_result_schema("op_artifact_list"))

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
            return _result(RuntimeStatus.NOT_FOUND, "Artifact list failed", {"reason": str(exc)})


@dataclass
class ArtifactInfoTool:
    service: ArtifactManager
    name: str = "op_artifact_info"
    display_name: str = "Artifact Info"
    family: str = "artifact"
    description: str = "Inspect metadata and available representations for one artifact id."
    tags: tuple[str, ...] = ("artifact", "attachment", "info")
    keywords: tuple[str, ...] = ("artifact", "metadata", "representation")
    args_schema: dict[str, Any] = field(default_factory=lambda: artifact_args_schema("op_artifact_info"))
    result_schema: dict[str, Any] = field(default_factory=lambda: artifact_result_schema("op_artifact_info"))

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        _ = args
        return _result(RuntimeStatus.INVALID, "Artifact info unavailable", {"reason": "async_required"})

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        try:
            scope_key = _scope_from_runtime(kwargs.get("runtime"), kwargs.get("turn_id"))
            structured = self.service.info(str(args.get("artifact_id") or ""), scope_key)
            return _result(RuntimeStatus.OK, "Artifact info", structured)
        except KeyError as exc:
            return _result(RuntimeStatus.NOT_FOUND, "Artifact info failed", {"reason": str(exc)})


@dataclass
class ArtifactReadTool:
    service: ArtifactManager
    name: str = "op_artifact_read"
    display_name: str = "Read Artifact"
    family: str = "artifact"
    description: str = "Read a text-like representation of a scoped artifact by artifact_id."
    tags: tuple[str, ...] = ("artifact", "attachment", "read")
    keywords: tuple[str, ...] = ("artifact", "read", "pdf", "text", "transcript")
    args_schema: dict[str, Any] = field(default_factory=lambda: artifact_args_schema("op_artifact_read"))
    result_schema: dict[str, Any] = field(default_factory=lambda: artifact_result_schema("op_artifact_read"))

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
            return _result(RuntimeStatus.NOT_FOUND, "Artifact read failed", {"reason": str(exc)})


@dataclass
class ArtifactSearchTool:
    service: ArtifactManager
    name: str = "op_artifact_search"
    display_name: str = "Search Artifacts"
    family: str = "artifact"
    description: str = "Find a recent conversation artifact by filename, type, summary, or time hint."
    tags: tuple[str, ...] = ("artifact", "attachment", "search")
    keywords: tuple[str, ...] = ("artifact", "search", "recent", "file")
    args_schema: dict[str, Any] = field(default_factory=lambda: artifact_args_schema("op_artifact_search"))
    result_schema: dict[str, Any] = field(default_factory=lambda: artifact_result_schema("op_artifact_search"))

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
            return _result(RuntimeStatus.NOT_FOUND, "Artifact search failed", {"reason": str(exc)})


@dataclass
class ArtifactSelectTool:
    service: ArtifactManager
    name: str = "op_artifact_select"
    display_name: str = "Select Artifact"
    family: str = "artifact"
    description: str = "Mark an artifact search result as selected and refresh its short-lived hot state."
    tags: tuple[str, ...] = ("artifact", "select", "ttl")
    keywords: tuple[str, ...] = ("artifact", "select", "refresh")
    args_schema: dict[str, Any] = field(default_factory=lambda: artifact_args_schema("op_artifact_select"))
    result_schema: dict[str, Any] = field(default_factory=lambda: artifact_result_schema("op_artifact_select"))

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        _ = args
        return _result(RuntimeStatus.INVALID, "Artifact select unavailable", {"reason": "async_required"})

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        try:
            scope_key = _scope_from_runtime(kwargs.get("runtime"), kwargs.get("turn_id"))
            structured = self.service.select(str(args.get("artifact_id") or ""), scope_key)
            return _result(RuntimeStatus.OK, "Artifact selected", structured)
        except KeyError as exc:
            return _result(RuntimeStatus.NOT_FOUND, "Artifact select failed", {"reason": str(exc)})


@dataclass
class ArtifactContentSearchTool:
    service: ArtifactManager
    name: str = "op_artifact_content_search"
    display_name: str = "Search Artifact Content"
    family: str = "artifact"
    description: str = "Search inside a known artifact's text, PDF pages/chunks, transcript, or OCR text."
    tags: tuple[str, ...] = ("artifact", "content", "search")
    keywords: tuple[str, ...] = ("artifact", "content", "pdf", "transcript", "search")
    args_schema: dict[str, Any] = field(default_factory=lambda: artifact_args_schema("op_artifact_content_search"))
    result_schema: dict[str, Any] = field(default_factory=lambda: artifact_result_schema("op_artifact_content_search"))

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
            structured = {"results": [item.to_dict() for item in results], "ttl_refreshed": True}
            return _result(RuntimeStatus.OK, "Artifact content search results", structured, text=f"{len(results)} content match(es)")
        except KeyError as exc:
            return _result(RuntimeStatus.NOT_FOUND, "Artifact content search failed", {"reason": str(exc)})


@dataclass
class ArtifactTranscribeTool:
    service: ArtifactManager
    name: str = "op_artifact_transcribe"
    display_name: str = "Transcribe Artifact"
    family: str = "artifact"
    description: str = "Request transcription for an audio artifact. V1 returns needs_transcription when no ASR provider is registered."
    tags: tuple[str, ...] = ("artifact", "audio", "transcribe")
    keywords: tuple[str, ...] = ("artifact", "audio", "voice", "transcript")
    args_schema: dict[str, Any] = field(default_factory=lambda: artifact_args_schema("op_artifact_transcribe"))
    result_schema: dict[str, Any] = field(default_factory=lambda: artifact_result_schema("op_artifact_transcribe"))

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
            return _result(RuntimeStatus.NOT_FOUND, "Artifact transcription failed", {"reason": str(exc)})


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None
