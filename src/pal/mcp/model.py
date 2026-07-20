from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from pal.shared import MountedSubtreeHandle
from pal.skill.contracts import SkillDescriptor


@dataclass(frozen=True)
class McpServerConfig:
    server_id: str
    command: tuple[str, ...]
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    transport: str = "stdio"
    protocol_version: str = "2025-06-18"
    startup_timeout_ms: int = 10_000
    request_timeout_ms: int = 300_000
    shutdown_timeout_ms: int = 5_000
    kill_on_close: bool = True
    trust_level: str = "unknown"


@dataclass(frozen=True)
class McpToolSpec:
    name: str
    description: str = ""
    input_schema: Any | None = None
    output_schema: Any | None = None
    annotations: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class McpPromptArgumentSpec:
    name: str
    description: str = ""
    required: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class McpPromptSpec:
    name: str
    description: str = ""
    arguments: tuple[McpPromptArgumentSpec, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class McpRejectedItem:
    kind: str
    external_name: str
    reason: str
    raw_schema: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "external_name": self.external_name,
            "reason": self.reason,
            "raw_schema": self.raw_schema,
            "raw": dict(self.raw),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "McpRejectedItem":
        return cls(
            kind=str(payload.get("kind") or ""),
            external_name=str(payload.get("external_name") or ""),
            reason=str(payload.get("reason") or ""),
            raw_schema=dict(payload.get("raw_schema") or {}) if isinstance(payload.get("raw_schema"), dict) else None,
            raw=dict(payload.get("raw") or {}),
        )


@dataclass(frozen=True)
class McpDiscoverySnapshot:
    server_id: str
    transport: str
    server_info: dict[str, Any] = field(default_factory=dict)
    server_capabilities: dict[str, Any] = field(default_factory=dict)
    tools: tuple[McpToolSpec, ...] = ()
    prompts: tuple[McpPromptSpec, ...] = ()
    discovered_at: str = ""
    snapshot_hash: str = ""
    warnings: tuple[str, ...] = ()
    rejected_items: tuple[McpRejectedItem, ...] = ()

    def with_diagnostics(
        self,
        *,
        warnings: tuple[str, ...] = (),
        rejected_items: tuple[McpRejectedItem, ...] = (),
    ) -> "McpDiscoverySnapshot":
        snapshot = McpDiscoverySnapshot(
            server_id=self.server_id,
            transport=self.transport,
            server_info=dict(self.server_info),
            server_capabilities=dict(self.server_capabilities),
            tools=tuple(self.tools),
            prompts=tuple(self.prompts),
            discovered_at=self.discovered_at,
            warnings=tuple(dict.fromkeys((*self.warnings, *warnings))),
            rejected_items=tuple((*self.rejected_items, *rejected_items)),
        )
        return snapshot.with_hash()

    def with_hash(self) -> "McpDiscoverySnapshot":
        payload = {
            "server_id": self.server_id,
            "transport": self.transport,
            "server_info": _stable(self.server_info),
            "server_capabilities": _stable(self.server_capabilities),
            "tools": [_stable(asdict(item)) for item in sorted(self.tools, key=lambda item: item.name)],
            "prompts": [_stable(asdict(item)) for item in sorted(self.prompts, key=lambda item: item.name)],
            "warnings": sorted(self.warnings),
            "rejected_items": [_stable(item.to_dict()) for item in self.rejected_items],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return McpDiscoverySnapshot(
            server_id=self.server_id,
            transport=self.transport,
            server_info=dict(self.server_info),
            server_capabilities=dict(self.server_capabilities),
            tools=tuple(self.tools),
            prompts=tuple(self.prompts),
            discovered_at=self.discovered_at,
            snapshot_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            warnings=tuple(self.warnings),
            rejected_items=tuple(self.rejected_items),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_id": self.server_id,
            "transport": self.transport,
            "server_info": dict(self.server_info),
            "server_capabilities": dict(self.server_capabilities),
            "tools": [asdict(item) for item in self.tools],
            "prompts": [asdict(item) for item in self.prompts],
            "discovered_at": self.discovered_at,
            "snapshot_hash": self.snapshot_hash,
            "warnings": list(self.warnings),
            "rejected_items": [item.to_dict() for item in self.rejected_items],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "McpDiscoverySnapshot":
        return cls(
            server_id=str(payload.get("server_id") or ""),
            transport=str(payload.get("transport") or "stdio"),
            server_info=dict(payload.get("server_info") or {}),
            server_capabilities=dict(payload.get("server_capabilities") or {}),
            tools=tuple(_tool_from_dict(item) for item in list(payload.get("tools") or []) if isinstance(item, dict)),
            prompts=tuple(_prompt_from_dict(item) for item in list(payload.get("prompts") or []) if isinstance(item, dict)),
            discovered_at=str(payload.get("discovered_at") or ""),
            snapshot_hash=str(payload.get("snapshot_hash") or ""),
            warnings=tuple(str(item) for item in list(payload.get("warnings") or [])),
            rejected_items=tuple(
                McpRejectedItem.from_dict(item)
                for item in list(payload.get("rejected_items") or [])
                if isinstance(item, dict)
            ),
        )


@dataclass(frozen=True)
class McpProjectionResult:
    module_id: str
    mounted_subtree: MountedSubtreeHandle
    skills: tuple[SkillDescriptor, ...]
    snapshot: McpDiscoverySnapshot


class McpProtocolError(RuntimeError):
    pass


class McpProjectionError(RuntimeError):
    pass


def _stable(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True)
        return value
    except TypeError:
        if isinstance(value, dict):
            return {str(key): _stable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_stable(item) for item in value]
        return str(value)


def _tool_from_dict(payload: dict[str, Any]) -> McpToolSpec:
    return McpToolSpec(
        name=str(payload.get("name") or ""),
        description=str(payload.get("description") or ""),
        input_schema=payload.get("input_schema"),
        output_schema=payload.get("output_schema"),
        annotations=dict(payload.get("annotations") or {}),
        raw=dict(payload.get("raw") or {}),
    )


def _prompt_from_dict(payload: dict[str, Any]) -> McpPromptSpec:
    arguments = tuple(
        McpPromptArgumentSpec(
            name=str(item.get("name") or ""),
            description=str(item.get("description") or ""),
            required=bool(item.get("required", False)),
            raw=dict(item.get("raw") or {}),
        )
        for item in list(payload.get("arguments") or [])
        if isinstance(item, dict)
    )
    return McpPromptSpec(
        name=str(payload.get("name") or ""),
        description=str(payload.get("description") or ""),
        arguments=arguments,
        raw=dict(payload.get("raw") or {}),
    )
