from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import re
import sys
import tomllib
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pal.channel.channel_endpoint_queue_base import ChannelEndpointBase
from pal.channel.factory import ChannelEndpointFactory, SocketChannelEndpointFactory
from pal.channel.models import ChannelEndpointModel
from pal.channel.repository import ChannelEndpointRepository
from pal.channel.runtime import ChannelRuntime
from pal.shared import IntrospectionResult, RuntimeStatus
from pal.shared.result_rendering import render_titled_structured_for_llm


RUNTIME_CHANNEL_PROVIDER_DIR = "channel/providers"
RUNTIME_CHANNEL_DATA_DIR = "data/channel"
CHANNEL_PROVIDER_MANIFEST_FILENAME = "provider.toml"


@dataclass(frozen=True)
class RuntimeChannelProviderManifest:
    provider_id: str
    entrypoint: str
    version: str
    enabled: bool
    filesystem_path: str
    reload_modules: tuple[str, ...] = ()
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelProviderContext:
    runtime: ChannelRuntime
    repository: ChannelEndpointRepository
    runtime_root: Path

    def endpoint_data_root(self, endpoint_id: str) -> Path:
        return channel_endpoint_data_root(self.runtime_root, endpoint_id)


@dataclass
class ChannelProviderBuildContext:
    runtime_root: Path
    provider_dir: Path
    manifest: RuntimeChannelProviderManifest
    manager: Any
    cleanup_callbacks: list[Callable[[], Any]] = field(default_factory=list)

    @property
    def channel_data_root(self) -> Path:
        return self.runtime_root / RUNTIME_CHANNEL_DATA_DIR

    def register_cleanup(self, callback: Callable[[], Any]) -> None:
        if not callable(callback):
            raise TypeError("channel provider cleanup callback must be callable")
        self.cleanup_callbacks.append(callback)


@dataclass
class RuntimeChannelProviderHandle:
    manifest: RuntimeChannelProviderManifest
    provider: ChannelProvider
    module_names: tuple[str, ...]
    cleanup_callbacks: list[Callable[[], Any]] = field(default_factory=list)
    lifecycle_context: ChannelProviderBuildContext | None = None
    attached: bool = False


@dataclass(frozen=True)
class DiscoveredChannelProvider:
    manifest: RuntimeChannelProviderManifest
    endpoint_types: tuple[str, ...]


class ChannelProvider(Protocol):
    provider_id: str
    endpoint_types: tuple[str, ...]
    reload_modules: tuple[str, ...]

    def create_endpoint(
        self,
        record: ChannelEndpointModel,
        context: ChannelProviderContext,
    ) -> ChannelEndpointBase | None:
        ...

    def attach_endpoint(self, endpoint_id: str, context: ChannelProviderContext) -> IntrospectionResult:
        ...

    def detach_endpoint(self, endpoint_id: str, context: ChannelProviderContext) -> IntrospectionResult:
        ...

    def restart_endpoint(self, endpoint_id: str, context: ChannelProviderContext) -> IntrospectionResult:
        ...

    def inspect_endpoint(self, endpoint_id: str, context: ChannelProviderContext) -> IntrospectionResult:
        ...

    def inspect_auth_state(self, endpoint_id: str, context: ChannelProviderContext) -> IntrospectionResult:
        ...

    def set_auth_material(
        self,
        endpoint_id: str,
        material: dict[str, Any],
        context: ChannelProviderContext,
    ) -> IntrospectionResult:
        ...

    def inspect_backlog(self, endpoint_id: str, context: ChannelProviderContext) -> IntrospectionResult:
        ...

    def inspect_health(self, endpoint_id: str, context: ChannelProviderContext) -> IntrospectionResult:
        ...


@dataclass
class FactoryChannelProvider:
    provider_id: str
    endpoint_types: tuple[str, ...]
    factory: ChannelEndpointFactory
    reload_modules: tuple[str, ...] = ()

    def create_endpoint(
        self,
        record: ChannelEndpointModel,
        context: ChannelProviderContext,
    ) -> ChannelEndpointBase | None:
        return self.factory.create(record, runtime_root=context.runtime_root)

    def attach_endpoint(self, endpoint_id: str, context: ChannelProviderContext) -> IntrospectionResult:
        record = context.repository.get(endpoint_id)
        if record is None:
            return _not_found(endpoint_id)
        endpoint = self.create_endpoint(record, context)
        if endpoint is None:
            return _provider_missing(endpoint_id, record.channel_kind)
        old_endpoint = context.runtime.get_endpoint(endpoint_id)
        _preserve_runtime_endpoint_state(old_endpoint, endpoint)
        endpoint.attached = True
        record = commit_channel_endpoint_attach(endpoint_id, endpoint, context)
        return _ok(
            "Channel endpoint attached",
            {
                "endpoint_id": endpoint_id,
                "endpoint_type": record.channel_kind,
                "provider_id": self.provider_id,
                "reload_modules": list(self.reload_modules),
                "attached": True,
                "enabled": bool(endpoint.enabled),
            },
        )

    def detach_endpoint(self, endpoint_id: str, context: ChannelProviderContext) -> IntrospectionResult:
        endpoint = context.runtime.get_endpoint(endpoint_id)
        record = context.repository.get(endpoint_id)
        if is_recovery_socket_endpoint(record, endpoint, context.runtime_root):
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="recovery socket endpoint cannot be detached",
                structured={
                    "endpoint_id": endpoint_id,
                    "endpoint_type": "socket",
                    "channel_kind": "socket",
                    "binding_key": str(recovery_socket_path(context.runtime_root)),
                    "attached": True,
                    "reason": "recovery_socket_control_channel",
                },
                llm_text="recovery socket endpoint cannot be detached",
            )
        if record is None and endpoint is None:
            return _not_found(endpoint_id)
        record, endpoint, removed = commit_channel_endpoint_detach(endpoint_id, context)
        endpoint_type = record.channel_kind if record is not None else endpoint.endpoint.channel_kind
        return _ok(
            "Channel endpoint detached",
            {
                "endpoint_id": endpoint_id,
                "endpoint_type": endpoint_type,
                "provider_id": self.provider_id,
                "attached": False,
                "removed_runtime_endpoint": bool(removed),
            },
        )

    def restart_endpoint(self, endpoint_id: str, context: ChannelProviderContext) -> IntrospectionResult:
        record = context.repository.get(endpoint_id)
        if record is None:
            return _not_found(endpoint_id)
        endpoint = self.create_endpoint(record, context)
        if endpoint is None:
            return _provider_missing(endpoint_id, record.channel_kind)
        old_endpoint = context.runtime.get_endpoint(endpoint_id)
        _preserve_runtime_endpoint_state(old_endpoint, endpoint)
        context.runtime.replace_endpoint(endpoint)
        return _ok(
            "Channel endpoint restarted",
            {
                "endpoint_id": endpoint_id,
                "endpoint_type": record.channel_kind,
                "provider_id": self.provider_id,
                "reload_modules": list(self.reload_modules),
                "attached": bool(endpoint.attached),
                "enabled": bool(endpoint.enabled),
            },
        )

    def inspect_endpoint(self, endpoint_id: str, context: ChannelProviderContext) -> IntrospectionResult:
        record = context.repository.get(endpoint_id)
        endpoint = context.runtime.get_endpoint(endpoint_id)
        if record is None and endpoint is None:
            return _not_found(endpoint_id)
        payload = _endpoint_snapshot(endpoint_id, record, endpoint)
        payload["provider_id"] = self.provider_id
        return _ok("Channel endpoint snapshot", payload)

    def inspect_auth_state(self, endpoint_id: str, context: ChannelProviderContext) -> IntrospectionResult:
        record = context.repository.get(endpoint_id)
        endpoint = context.runtime.get_endpoint(endpoint_id)
        if record is None and endpoint is None:
            return _not_found(endpoint_id)
        if endpoint is None:
            payload = {
                "endpoint_id": endpoint_id,
                "provider_id": self.provider_id,
                "paired": False,
                "attached": record.detached_at is None if record is not None else False,
                "authorized": False,
            }
            return _ok("Channel endpoint authorization state", payload)
        payload = dict(endpoint.inspect_auth_state())
        payload.setdefault("endpoint_id", endpoint_id)
        payload.setdefault("provider_id", self.provider_id)
        return _ok("Channel endpoint authorization state", _sanitize_secret_payload(payload))

    def set_auth_material(
        self,
        endpoint_id: str,
        material: dict[str, Any],
        context: ChannelProviderContext,
    ) -> IntrospectionResult:
        endpoint = context.runtime.get_endpoint(endpoint_id)
        if endpoint is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="channel endpoint runtime not found",
                llm_text="channel endpoint runtime not found",
            )
        auth_state = endpoint.apply_auth_material(dict(material))
        try:
            context.repository.merge_binding_metadata(
                endpoint_id,
                {
                    "auth_keys": sorted(str(key) for key in material.keys()),
                    "paired": bool(endpoint.paired),
                },
            )
        except Exception:
            pass
        payload = _sanitize_secret_payload(dict(auth_state))
        payload.setdefault("endpoint_id", endpoint_id)
        payload.setdefault("provider_id", self.provider_id)
        payload.setdefault("accepted_keys", sorted(str(key) for key in material.keys()))
        return _ok("Channel endpoint auth material updated", payload)

    def inspect_backlog(self, endpoint_id: str, context: ChannelProviderContext) -> IntrospectionResult:
        record = context.repository.get(endpoint_id)
        endpoint = context.runtime.get_endpoint(endpoint_id)
        if record is None and endpoint is None:
            return _not_found(endpoint_id)
        if endpoint is None:
            payload = {
                "endpoint_id": endpoint_id,
                "provider_id": self.provider_id,
                "inbox_size": 0,
                "outbox_size": 0,
            }
            return _ok("Channel endpoint backlog state", payload)
        payload = dict(endpoint.inspect_backlog())
        payload.setdefault("endpoint_id", endpoint_id)
        payload.setdefault("provider_id", self.provider_id)
        return _ok("Channel endpoint backlog state", payload)

    def inspect_health(self, endpoint_id: str, context: ChannelProviderContext) -> IntrospectionResult:
        record = context.repository.get(endpoint_id)
        endpoint = context.runtime.get_endpoint(endpoint_id)
        if record is None and endpoint is None:
            return _not_found(endpoint_id)
        if endpoint is None:
            payload = {
                "endpoint_id": endpoint_id,
                "provider_id": self.provider_id,
                "attached": record.detached_at is None if record is not None else False,
                "enabled": bool(record.enabled) if record is not None else False,
                "healthy": False,
                "reason": "runtime_endpoint_missing",
            }
            return _ok("Channel endpoint health", payload)
        payload = _sanitize_secret_payload(dict(endpoint.inspect_health()))
        payload.setdefault("endpoint_id", endpoint_id)
        payload.setdefault("provider_id", self.provider_id)
        payload.setdefault("attached", bool(endpoint.attached))
        payload.setdefault("enabled", bool(endpoint.enabled))
        return _ok("Channel endpoint health", payload)


@dataclass
class ChannelEndpointProviderManager:
    runtime: ChannelRuntime
    repository: ChannelEndpointRepository
    runtime_root: Path
    providers: dict[str, ChannelProvider] = field(default_factory=dict)
    endpoint_type_to_provider: dict[str, str] = field(default_factory=dict)
    scan_errors: list[str] = field(default_factory=list)
    plugin_host: Any = None
    discovered_runtime_providers: dict[str, DiscoveredChannelProvider] = field(default_factory=dict)
    runtime_provider_handles: dict[str, RuntimeChannelProviderHandle] = field(default_factory=dict)
    runtime_provider_load_errors: list[str] = field(default_factory=list)
    shutdown_errors: list[str] = field(default_factory=list)

    @property
    def runtime_provider_ids(self) -> set[str]:
        return set(self.discovered_runtime_providers)

    def context(self) -> ChannelProviderContext:
        return ChannelProviderContext(
            runtime=self.runtime,
            repository=self.repository,
            runtime_root=self.runtime_root,
        )

    def register_provider(self, provider: ChannelProvider) -> None:
        provider_id = str(provider.provider_id or "").strip()
        if not provider_id:
            raise ValueError("channel provider_id is required")
        endpoint_types = tuple(
            dict.fromkeys(
                normalized
                for endpoint_type in provider.endpoint_types
                if (normalized := str(endpoint_type or "").strip())
            )
        )
        if not endpoint_types:
            raise ValueError(f"channel provider has no endpoint types: {provider_id}")
        for endpoint_type in endpoint_types:
            owner_id = self.endpoint_type_to_provider.get(endpoint_type)
            if owner_id is not None and owner_id != provider_id:
                raise ValueError(
                    f"channel endpoint type already registered: {endpoint_type} ({owner_id})"
                )
        old_provider = self.providers.get(provider_id)
        old_types = tuple(old_provider.endpoint_types) if old_provider is not None else ()
        for endpoint_type in old_types:
            normalized = str(endpoint_type or "").strip()
            if normalized not in endpoint_types and self.endpoint_type_to_provider.get(normalized) == provider_id:
                self.endpoint_type_to_provider.pop(normalized, None)
        self.providers[provider_id] = provider
        for endpoint_type in endpoint_types:
            self.endpoint_type_to_provider[endpoint_type] = provider_id

    def unregister_provider(self, provider_id: str) -> ChannelProvider | None:
        normalized = str(provider_id or "").strip()
        provider = self.providers.pop(normalized, None)
        if provider is None:
            return None
        for endpoint_type, owner_id in list(self.endpoint_type_to_provider.items()):
            if owner_id == normalized:
                self.endpoint_type_to_provider.pop(endpoint_type, None)
        return provider

    def list_providers(self) -> list[dict[str, Any]]:
        providers: list[dict[str, Any]] = []
        provider_ids = set(self.providers) | set(self.discovered_runtime_providers)
        for provider_id in sorted(provider_ids):
            provider = self.providers.get(provider_id)
            discovered = self.discovered_runtime_providers.get(provider_id)
            manifest = discovered.manifest if discovered is not None else None
            endpoint_types = (
                tuple(provider.endpoint_types)
                if provider is not None
                else discovered.endpoint_types
                if discovered is not None
                else ()
            )
            row = {
                "provider_id": provider_id,
                "endpoint_types": list(endpoint_types),
                "reload_modules": list(getattr(provider, "reload_modules", ()) or ()) if provider else [],
                "source": "runtime_root" if provider_id in self.runtime_provider_ids else "registered",
                "code_loaded": provider_id in self.runtime_provider_handles or provider_id not in self.runtime_provider_ids,
            }
            if manifest is not None:
                row.update(
                    {
                        "version": manifest.version,
                        "filesystem_path": manifest.filesystem_path,
                        "entrypoint": manifest.entrypoint,
                    }
                )
            providers.append(row)
        return providers

    def provider_for_endpoint_type(self, endpoint_type: str) -> ChannelProvider | None:
        provider_id = self.endpoint_type_to_provider.get(str(endpoint_type or "").strip())
        if not provider_id:
            return None
        return self.providers.get(provider_id)

    def provider_for_endpoint(self, endpoint_id: str) -> ChannelProvider | None:
        record = self.repository.get(endpoint_id)
        if record is not None:
            return self.provider_for_endpoint_type(record.channel_kind)
        endpoint = self.runtime.get_endpoint(endpoint_id)
        if endpoint is None:
            return None
        return self.provider_for_endpoint_type(endpoint.endpoint.channel_kind)

    def _provider_id_for_endpoint(self, endpoint_id: str) -> str:
        hub = self.runtime.get_endpoint_hub(endpoint_id)
        if hub is not None and hub.provider_id:
            return hub.provider_id
        record = self.repository.get(endpoint_id)
        if record is None:
            return ""
        return str(self.endpoint_type_to_provider.get(record.channel_kind) or "")

    def _ensure_hub(self, record: ChannelEndpointModel, provider_id: str) -> None:
        self.runtime.ensure_endpoint_hub(
            record.endpoint_id,
            provider_id=provider_id,
            channel_kind=record.channel_kind,
            binding_key=record.binding_key,
        )
        if is_recovery_socket_endpoint(record, None, self.runtime_root):
            self.runtime.set_recovery_endpoint(record.endpoint_id)

    def _ensure_provider_hubs(self, provider_id: str, endpoint_types: tuple[str, ...]) -> list[str]:
        endpoint_type_set = set(endpoint_types)
        endpoint_ids: list[str] = []
        for record in self.repository.list_all():
            if record.channel_kind not in endpoint_type_set:
                continue
            self._ensure_hub(record, provider_id)
            endpoint_ids.append(record.endpoint_id)
        return sorted(endpoint_ids)

    def hydrate_all(self, *, exclude_endpoint_ids: set[str] | None = None) -> list[str]:
        hydrated: list[str] = []
        excluded = set(exclude_endpoint_ids or ())
        for record in self.repository.list_all():
            if record.endpoint_id in excluded:
                continue
            provider_id = str(self.endpoint_type_to_provider.get(record.channel_kind) or "")
            if not provider_id:
                continue
            self._ensure_hub(record, provider_id)
            if not bool(record.enabled) or record.detached_at is not None:
                continue
            result = self.attach_endpoint(record.endpoint_id)
            if result.status == RuntimeStatus.OK:
                hydrated.append(record.endpoint_id)
        return hydrated

    def hydrate_provider(self, provider_id: str) -> list[str]:
        normalized = str(provider_id or "").strip()
        endpoint_types = self._provider_endpoint_types(normalized)
        if not endpoint_types:
            return []
        hydrated: list[str] = []
        for endpoint_type in endpoint_types:
            for record in self.repository.list_all(channel_kind=endpoint_type):
                self._ensure_hub(record, normalized)
                if not bool(record.enabled) or record.detached_at is not None:
                    continue
                result = self.attach_endpoint(record.endpoint_id)
                if result.status == RuntimeStatus.OK:
                    hydrated.append(record.endpoint_id)
        return hydrated

    def attach_endpoint(self, endpoint_id: str) -> IntrospectionResult:
        record = self.repository.get(endpoint_id)
        provider_id = self._provider_id_for_endpoint(endpoint_id)
        if record is None or not provider_id:
            return _provider_missing_for_endpoint(endpoint_id)
        self._ensure_hub(record, provider_id)
        try:
            provider = self._ensure_provider_loaded(provider_id)
        except Exception as exc:
            self.runtime.fail_endpoint_transition(endpoint_id, str(exc))
            try:
                self._unload_provider_if_idle(provider_id)
            except Exception:
                pass
            return _provider_lifecycle_error("attach", provider_id, endpoint_id, exc)
        self.runtime.begin_endpoint_transition(endpoint_id, provider_id=provider_id)
        try:
            result = provider.attach_endpoint(endpoint_id, self.context())
            if result.status != RuntimeStatus.OK:
                raise RuntimeError(result.text or str(result.status))
            if self.runtime.get_endpoint(endpoint_id) is None:
                raise RuntimeError("provider reported success without registering an endpoint")
            self.runtime.publish_endpoint_when_ready(endpoint_id)
            self.runtime.complete_endpoint_transition(endpoint_id)
            return result
        except Exception as exc:
            self.runtime.fail_endpoint_transition(endpoint_id, str(exc))
            return _provider_lifecycle_error("attach", provider_id, endpoint_id, exc)

    def detach_endpoint(self, endpoint_id: str) -> IntrospectionResult:
        provider_id = self._provider_id_for_endpoint(endpoint_id)
        provider = self.providers.get(provider_id)
        if provider is None:
            return _provider_missing_for_endpoint(endpoint_id)
        record = self.repository.get(endpoint_id)
        endpoint = self.runtime.get_endpoint(endpoint_id)
        if is_recovery_socket_endpoint(record, endpoint, self.runtime_root):
            return provider.detach_endpoint(endpoint_id, self.context())
        try:
            self.runtime.withdraw_endpoint(endpoint_id)
            self.runtime.begin_endpoint_transition(endpoint_id, provider_id=provider_id)
            result = provider.detach_endpoint(endpoint_id, self.context())
            if result.status != RuntimeStatus.OK:
                self.runtime.rollback_endpoint_transition(
                    endpoint_id,
                    attached=endpoint is not None,
                )
                if endpoint is not None:
                    self.runtime.publish_endpoint(endpoint_id)
                return result
            self.runtime.mark_endpoint_detached(endpoint_id)
            self._unload_provider_if_idle(provider_id)
            return result
        except Exception as exc:
            self.runtime.fail_endpoint_transition(endpoint_id, str(exc))
            return _provider_lifecycle_error("detach", provider_id, endpoint_id, exc)

    def restart_endpoint(self, endpoint_id: str) -> IntrospectionResult:
        provider_id = self._provider_id_for_endpoint(endpoint_id)
        provider = self.providers.get(provider_id)
        if provider is None:
            return _provider_missing_for_endpoint(endpoint_id)
        try:
            self.runtime.withdraw_endpoint(endpoint_id)
            self.runtime.begin_endpoint_transition(endpoint_id, provider_id=provider_id)
            result = provider.restart_endpoint(endpoint_id, self.context())
            if result.status != RuntimeStatus.OK:
                raise RuntimeError(result.text or str(result.status))
            self.runtime.publish_endpoint_when_ready(endpoint_id)
            self.runtime.complete_endpoint_transition(endpoint_id)
            return result
        except Exception as exc:
            self.runtime.fail_endpoint_transition(endpoint_id, str(exc))
            return _provider_lifecycle_error("restart", provider_id, endpoint_id, exc)

    def inspect_endpoint(self, endpoint_id: str) -> IntrospectionResult:
        provider = self.provider_for_endpoint(endpoint_id)
        if provider is None:
            return _provider_missing_for_endpoint(endpoint_id)
        return self._with_endpoint_hub(
            endpoint_id,
            provider.inspect_endpoint(endpoint_id, self.context()),
        )

    def inspect_auth_state(self, endpoint_id: str) -> IntrospectionResult:
        provider = self.provider_for_endpoint(endpoint_id)
        if provider is None:
            return _provider_missing_for_endpoint(endpoint_id)
        return provider.inspect_auth_state(endpoint_id, self.context())

    def set_auth_material(self, endpoint_id: str, material: dict[str, Any]) -> IntrospectionResult:
        provider = self.provider_for_endpoint(endpoint_id)
        if provider is None:
            return _provider_missing_for_endpoint(endpoint_id)
        return provider.set_auth_material(endpoint_id, material, self.context())

    def inspect_backlog(self, endpoint_id: str) -> IntrospectionResult:
        provider = self.provider_for_endpoint(endpoint_id)
        if provider is None:
            return _provider_missing_for_endpoint(endpoint_id)
        return self._with_endpoint_hub(
            endpoint_id,
            provider.inspect_backlog(endpoint_id, self.context()),
        )

    def inspect_health(self, endpoint_id: str) -> IntrospectionResult:
        provider = self.provider_for_endpoint(endpoint_id)
        if provider is None:
            return _provider_missing_for_endpoint(endpoint_id)
        return self._with_endpoint_hub(
            endpoint_id,
            provider.inspect_health(endpoint_id, self.context()),
        )

    def _with_endpoint_hub(
        self,
        endpoint_id: str,
        result: IntrospectionResult,
    ) -> IntrospectionResult:
        payload = dict(result.structured or {})
        payload["endpoint_hub"] = self.runtime.inspect_endpoint_hub(endpoint_id)
        return IntrospectionResult(
            status=result.status,
            text=result.text,
            structured=payload,
            llm_text=render_titled_structured_for_llm(result.text, payload),
        )

    def load_runtime_providers(self) -> dict[str, Any]:
        result = self.rescan_providers()
        runtime_result = dict(result.get("runtime_result") or {})
        runtime_result.setdefault("runtime_provider_ids", sorted(self.runtime_provider_ids))
        runtime_result.setdefault(
            "runtime_provider_load_errors", list(self.runtime_provider_load_errors)
        )
        return runtime_result

    def rescan_providers(self) -> dict[str, Any]:
        before = sorted(set(self.providers) | set(self.discovered_runtime_providers))
        scan = self._scan_runtime_provider_manifests()
        enabled = scan["enabled"]
        disabled = scan["disabled"]
        seen_paths = scan["seen_paths"]
        errors = list(scan["errors"])
        added: list[str] = []
        unchanged: list[str] = []
        removed: list[str] = []
        disabled_removed: list[str] = []
        hydrated: list[str] = []
        discovered_endpoints: list[str] = []

        for provider_id in sorted(enabled):
            manifest = enabled[provider_id]
            if provider_id in self.discovered_runtime_providers:
                discovered = self.discovered_runtime_providers[provider_id]
                known_hubs = set(self._provider_hub_ids(provider_id))
                endpoint_ids = self._ensure_provider_hubs(
                    provider_id,
                    discovered.endpoint_types,
                )
                new_endpoint_ids = sorted(set(endpoint_ids) - known_hubs)
                discovered_endpoints.extend(new_endpoint_ids)
                for endpoint_id in new_endpoint_ids:
                    record = self.repository.get(endpoint_id)
                    if (
                        record is not None
                        and bool(record.enabled)
                        and record.detached_at is None
                    ):
                        result = self.attach_endpoint(endpoint_id)
                        if result.status == RuntimeStatus.OK:
                            hydrated.append(endpoint_id)
                        else:
                            errors.append(
                                f"{endpoint_id}: {result.status}: {result.text}"
                            )
                unchanged.append(provider_id)
                continue
            try:
                candidate = self._build_runtime_provider_handle(manifest)
                endpoint_types = tuple(candidate.provider.endpoint_types)
                self._register_discovered_provider(
                    DiscoveredChannelProvider(manifest=manifest, endpoint_types=endpoint_types)
                )
                endpoint_ids = self._ensure_provider_hubs(provider_id, endpoint_types)
                discovered_endpoints.extend(endpoint_ids)
                should_attach = any(
                    record.endpoint_id in endpoint_ids
                    and bool(record.enabled)
                    and record.detached_at is None
                    for record in self.repository.list_all()
                )
                if should_attach:
                    self._activate_runtime_provider_handle(candidate)
                    eligible_endpoint_ids = {
                        record.endpoint_id
                        for record in self.repository.list_all()
                        if record.endpoint_id in endpoint_ids
                        and bool(record.enabled)
                        and record.detached_at is None
                    }
                    provider_hydrated = self.hydrate_provider(provider_id)
                    hydrated.extend(provider_hydrated)
                    for endpoint_id in sorted(
                        eligible_endpoint_ids - set(provider_hydrated)
                    ):
                        errors.append(
                            f"{endpoint_id}: provider attach did not complete"
                        )
                else:
                    for cleanup_error in _dispose_runtime_provider_handle(
                        candidate,
                        runtime=self.runtime,
                    ):
                        errors.append(f"{provider_id}: {cleanup_error}")
                added.append(provider_id)
            except Exception as exc:
                errors.append(
                    f"{manifest.filesystem_path}: {exc.__class__.__name__}: {exc}"
                )

        for provider_id in sorted(self.discovered_runtime_providers):
            if provider_id in enabled:
                continue
            discovered = self.discovered_runtime_providers.get(provider_id)
            if discovered is None:
                continue
            expected_manifest = str(
                Path(discovered.manifest.filesystem_path)
                / CHANNEL_PROVIDER_MANIFEST_FILENAME
            )
            if provider_id not in disabled and expected_manifest in seen_paths:
                # The manifest exists but could not be parsed. Preserve the
                # known physical provider instead of interpreting damage as removal.
                continue
            try:
                reason = "provider_disabled" if provider_id in disabled else "provider_removed"
                self._remove_discovered_provider(provider_id, reason=reason)
                if provider_id in disabled:
                    disabled_removed.append(provider_id)
                else:
                    removed.append(provider_id)
            except Exception as exc:
                errors.append(f"{provider_id}: {exc.__class__.__name__}: {exc}")

        self.runtime_provider_load_errors = errors
        self.scan_errors = list(errors)
        after = sorted(set(self.providers) | set(self.discovered_runtime_providers))
        runtime_result = {
            "runtime_provider_dirs": [str(path) for path in _runtime_provider_dirs(self.runtime_root)],
            "runtime_provider_manifests": sorted(seen_paths),
            "runtime_provider_ids": sorted(self.runtime_provider_ids),
            "loaded_runtime_provider_ids": sorted(self.runtime_provider_handles),
            "added_runtime_provider_ids": sorted(added),
            "changed_runtime_provider_ids": [],
            "unchanged_runtime_provider_ids": sorted(unchanged),
            "removed_runtime_provider_ids": sorted(removed),
            "disabled_runtime_provider_ids": sorted(disabled),
            "disabled_removed_runtime_provider_ids": sorted(disabled_removed),
            "hydrated_runtime_endpoint_ids": sorted(dict.fromkeys(hydrated)),
            "discovered_runtime_endpoint_ids": sorted(dict.fromkeys(discovered_endpoints)),
            "runtime_provider_load_errors": list(errors),
        }
        return {
            "providers_before": before,
            "providers_after": after,
            "added_provider_ids": sorted(added),
            "changed_provider_ids": [],
            "unchanged_provider_ids": sorted(unchanged),
            "removed_provider_ids": sorted(removed + disabled_removed),
            "provider_count": len(after),
            "endpoint_type_map": dict(sorted(self.endpoint_type_to_provider.items())),
            "hydrated_endpoint_ids": sorted(dict.fromkeys(hydrated)),
            "discovered_endpoint_ids": sorted(dict.fromkeys(discovered_endpoints)),
            "restored_endpoint_ids": [],
            "plugin_result": {},
            "runtime_result": runtime_result,
            "runtime_provider_ids": sorted(self.runtime_provider_ids),
            "runtime_provider_load_errors": list(errors),
            "scan_errors": list(self.scan_errors),
        }

    def reload_provider(self, provider_id: str) -> IntrospectionResult:
        normalized = str(provider_id or "").strip()
        discovered = self.discovered_runtime_providers.get(normalized)
        if discovered is None:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="channel provider does not support runtime reload",
                structured={"provider_id": normalized, "reason": "provider_not_runtime_owned"},
                llm_text="channel provider does not support runtime reload",
            )
        manifest_path = (
            Path(discovered.manifest.filesystem_path) / CHANNEL_PROVIDER_MANIFEST_FILENAME
        )
        endpoint_ids = self._provider_hub_ids(normalized)
        previously_attached = {
            endpoint_id
            for endpoint_id in endpoint_ids
            if self.runtime.get_endpoint(endpoint_id) is not None
        }
        for endpoint_id in endpoint_ids:
            self.runtime.withdraw_endpoint(endpoint_id)
        stop_errors: list[str] = []
        try:
            _stopped, stop_errors = self._stop_provider_transports(
                normalized,
                reason="provider_reload",
            )
            unload_errors = self._unload_runtime_provider(normalized)
            if unload_errors:
                raise RuntimeError("; ".join(unload_errors))
            manifest = _read_runtime_provider_manifest(manifest_path)
            if manifest.provider_id != normalized:
                raise RuntimeError(
                    "channel provider manifest id changed during reload: "
                    f"expected {normalized!r}, found {manifest.provider_id!r}"
                )
            if not manifest.enabled:
                raise RuntimeError(f"channel provider is disabled: {normalized}")
            candidate = self._build_runtime_provider_handle(manifest)
            replacement = DiscoveredChannelProvider(
                manifest=manifest,
                endpoint_types=tuple(candidate.provider.endpoint_types),
            )
            self._replace_discovered_provider(normalized, replacement)
            self._activate_runtime_provider_handle(candidate)
            attached: list[str] = []
            removed_endpoint_ids = sorted(
                endpoint_id
                for endpoint_id in previously_attached
                if self.runtime.get_endpoint_hub(endpoint_id) is None
            )
            for endpoint_id in sorted(previously_attached - set(removed_endpoint_ids)):
                result = self.attach_endpoint(endpoint_id)
                if result.status != RuntimeStatus.OK:
                    raise RuntimeError(result.text)
                attached.append(endpoint_id)
        except Exception as exc:
            for endpoint_id in endpoint_ids:
                self.runtime.withdraw_endpoint(endpoint_id)
            self._stop_provider_transports(normalized, reason="provider_reload_failed")
            self._unload_runtime_provider(normalized)
            for endpoint_id in endpoint_ids:
                if self.runtime.get_endpoint_hub(endpoint_id) is not None:
                    self.runtime.fail_endpoint_transition(endpoint_id, str(exc))
            return IntrospectionResult(
                status=RuntimeStatus.ERROR,
                text=f"channel provider reload failed: {exc}",
                structured={
                    "provider_id": normalized,
                    "reloaded": False,
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                },
                llm_text=f"channel provider reload failed: {exc}",
            )
        return _ok(
            "Channel provider reloaded",
            {
                "provider_id": normalized,
                "reloaded": True,
                "restored_endpoint_ids": attached,
                "removed_endpoint_ids": removed_endpoint_ids,
                "transport_stop_errors": stop_errors,
            },
        )

    def _scan_runtime_provider_manifests(self) -> dict[str, Any]:
        primary_dir = self.runtime_root / RUNTIME_CHANNEL_PROVIDER_DIR
        primary_dir.mkdir(parents=True, exist_ok=True)
        enabled: dict[str, RuntimeChannelProviderManifest] = {}
        disabled: set[str] = set()
        seen_provider_ids: set[str] = set()
        seen_paths: set[str] = set()
        errors: list[str] = []
        for providers_dir in _runtime_provider_dirs(self.runtime_root):
            for manifest_path in _iter_runtime_provider_manifest_paths(providers_dir):
                seen_paths.add(str(manifest_path))
                try:
                    manifest = _read_runtime_provider_manifest(manifest_path)
                    if manifest.provider_id in seen_provider_ids:
                        enabled.pop(manifest.provider_id, None)
                        disabled.discard(manifest.provider_id)
                        raise ValueError(f"duplicate channel provider_id: {manifest.provider_id}")
                    seen_provider_ids.add(manifest.provider_id)
                    if manifest.enabled:
                        enabled[manifest.provider_id] = manifest
                    else:
                        disabled.add(manifest.provider_id)
                except Exception as exc:
                    errors.append(f"{manifest_path}: {exc.__class__.__name__}: {exc}")
        return {
            "enabled": enabled,
            "disabled": disabled,
            "seen_paths": seen_paths,
            "errors": errors,
        }

    def _build_runtime_provider_handle(
        self,
        manifest: RuntimeChannelProviderManifest,
    ) -> RuntimeChannelProviderHandle:
        provider_id = str(manifest.provider_id or "").strip()
        if not provider_id:
            raise ValueError("provider_id is required")
        if provider_id in self.providers and provider_id not in self.runtime_provider_ids:
            raise ValueError(
                f"provider_id '{provider_id}' conflicts with an existing channel provider"
            )
        provider_dir = Path(manifest.filesystem_path).resolve()
        entrypoint_path = _runtime_provider_entrypoint_path(manifest)
        module_name = _runtime_provider_module_name(
            entrypoint_path,
            root=self.runtime_root,
            provider_id=provider_id,
        )
        modules_before = set(sys.modules)
        build_context = ChannelProviderBuildContext(
            runtime_root=self.runtime_root,
            provider_dir=provider_dir,
            manifest=manifest,
            manager=self,
        )
        try:
            module = _load_source_module(module_name, entrypoint_path)
            provider = _provider_from_module(module, context=build_context)
            actual_provider_id = str(getattr(provider, "provider_id", "") or "").strip()
            if actual_provider_id != provider_id:
                raise ValueError(
                    f"provider object id '{actual_provider_id}' does not match manifest "
                    f"provider_id '{provider_id}'"
                )
            endpoint_types = tuple(
                dict.fromkeys(
                    str(item or "").strip()
                    for item in getattr(provider, "endpoint_types", ())
                    if str(item or "").strip()
                )
            )
            if not endpoint_types:
                raise ValueError(
                    f"provider '{provider_id}' must declare at least one endpoint type"
                )
            for endpoint_type in endpoint_types:
                owner_id = self.endpoint_type_to_provider.get(endpoint_type)
                if owner_id and owner_id != provider_id:
                    raise ValueError(
                        f"endpoint type '{endpoint_type}' is already owned by provider '{owner_id}'"
                    )
        except Exception:
            _run_cleanup_callbacks(build_context.cleanup_callbacks, runtime=self.runtime)
            _remove_provider_modules(
                tuple(
                    name
                    for name in set(sys.modules) - modules_before
                    if name.startswith(module_name.rpartition(".")[0])
                )
            )
            raise
        module_names = tuple(
            sorted(
                name
                for name in set(sys.modules) - modules_before
                if name == module_name
                or name.startswith(f"{module_name.rpartition('.')[0]}.")
                or _module_loaded_from(name, provider_dir)
            )
        )
        return RuntimeChannelProviderHandle(
            manifest=manifest,
            provider=provider,
            module_names=module_names,
            cleanup_callbacks=build_context.cleanup_callbacks,
            lifecycle_context=build_context,
        )

    def _provider_endpoint_types(self, provider_id: str) -> tuple[str, ...]:
        provider = self.providers.get(provider_id)
        if provider is not None:
            return tuple(provider.endpoint_types)
        discovered = self.discovered_runtime_providers.get(provider_id)
        return discovered.endpoint_types if discovered is not None else ()

    def _provider_hub_ids(self, provider_id: str) -> list[str]:
        return sorted(
            hub.endpoint_id
            for hub in self.runtime.list_endpoint_hubs()
            if hub.provider_id == provider_id
        )

    def _register_discovered_provider(self, discovered: DiscoveredChannelProvider) -> None:
        provider_id = discovered.manifest.provider_id
        for endpoint_type in discovered.endpoint_types:
            owner_id = self.endpoint_type_to_provider.get(endpoint_type)
            if owner_id and owner_id != provider_id:
                raise ValueError(
                    f"endpoint type '{endpoint_type}' is already owned by provider '{owner_id}'"
                )
        self.discovered_runtime_providers[provider_id] = discovered
        for endpoint_type in discovered.endpoint_types:
            self.endpoint_type_to_provider[endpoint_type] = provider_id

    def _replace_discovered_provider(
        self,
        provider_id: str,
        replacement: DiscoveredChannelProvider,
    ) -> None:
        old = self.discovered_runtime_providers.get(provider_id)
        old_types = set(old.endpoint_types if old is not None else ())
        new_types = set(replacement.endpoint_types)
        for endpoint_type in old_types - new_types:
            if self.endpoint_type_to_provider.get(endpoint_type) == provider_id:
                self.endpoint_type_to_provider.pop(endpoint_type, None)
        self._register_discovered_provider(replacement)
        self._ensure_provider_hubs(provider_id, replacement.endpoint_types)
        for hub in tuple(self.runtime.list_endpoint_hubs()):
            if hub.provider_id == provider_id and hub.channel_kind not in new_types:
                self.runtime.remove_endpoint_hub(hub.endpoint_id)

    def _activate_runtime_provider_handle(self, handle: RuntimeChannelProviderHandle) -> None:
        provider_id = handle.manifest.provider_id
        context = handle.lifecycle_context
        if context is None:
            raise RuntimeError(f"provider '{provider_id}' has no lifecycle context")
        try:
            cleanup = _invoke_provider_lifecycle(
                handle.provider,
                "attach",
                context,
                runtime=self.runtime,
            )
            if callable(cleanup):
                context.register_cleanup(cleanup)
            handle.attached = True
            self.register_provider(handle.provider)
            self.runtime_provider_handles[provider_id] = handle
        except Exception:
            _dispose_runtime_provider_handle(handle, runtime=self.runtime)
            raise

    def _ensure_provider_loaded(self, provider_id: str) -> ChannelProvider:
        provider = self.providers.get(provider_id)
        if provider is not None:
            return provider
        discovered = self.discovered_runtime_providers.get(provider_id)
        if discovered is None:
            raise RuntimeError(f"channel provider is not physically available: {provider_id}")
        handle = self._build_runtime_provider_handle(discovered.manifest)
        if tuple(handle.provider.endpoint_types) != discovered.endpoint_types:
            _dispose_runtime_provider_handle(handle, runtime=self.runtime)
            raise RuntimeError(
                f"provider '{provider_id}' endpoint contract changed; run provider reload"
            )
        self._activate_runtime_provider_handle(handle)
        return handle.provider

    def _stop_provider_transports(self, provider_id: str, *, reason: str) -> tuple[list[str], list[str]]:
        stopped: list[str] = []
        errors: list[str] = []
        for endpoint_id in self._provider_hub_ids(provider_id):
            self.runtime.withdraw_endpoint(endpoint_id)
            self.runtime.begin_endpoint_transition(endpoint_id, provider_id=provider_id)
            try:
                if self.runtime.remove_endpoint(endpoint_id):
                    stopped.append(endpoint_id)
                self.runtime.mark_endpoint_detached(endpoint_id, reason=reason)
            except Exception as exc:
                self.runtime.discard_endpoint_transport(endpoint_id)
                self.runtime.fail_endpoint_transition(endpoint_id, str(exc))
                errors.append(f"{endpoint_id}: {exc}")
        return stopped, errors

    def _unload_runtime_provider(self, provider_id: str) -> list[str]:
        handle = self.runtime_provider_handles.pop(provider_id, None)
        if handle is None:
            return []
        self.unregister_provider(provider_id)
        discovered = self.discovered_runtime_providers.get(provider_id)
        if discovered is not None:
            for endpoint_type in discovered.endpoint_types:
                self.endpoint_type_to_provider[endpoint_type] = provider_id
        return _dispose_runtime_provider_handle(handle, runtime=self.runtime)

    def _unload_provider_if_idle(self, provider_id: str) -> None:
        if provider_id not in self.runtime_provider_handles:
            return
        if any(
            self.runtime.get_endpoint(endpoint_id) is not None
            for endpoint_id in self._provider_hub_ids(provider_id)
        ):
            return
        errors = self._unload_runtime_provider(provider_id)
        if errors:
            raise RuntimeError("; ".join(errors))

    def _remove_discovered_provider(self, provider_id: str, *, reason: str) -> None:
        discovered = self.discovered_runtime_providers.get(provider_id)
        if discovered is None:
            return
        endpoint_ids = self._provider_hub_ids(provider_id)
        for endpoint_id in endpoint_ids:
            self.runtime.withdraw_endpoint(endpoint_id)
        _stopped, stop_errors = self._stop_provider_transports(provider_id, reason=reason)
        unload_errors = self._unload_runtime_provider(provider_id)
        for endpoint_type in discovered.endpoint_types:
            if self.endpoint_type_to_provider.get(endpoint_type) == provider_id:
                self.endpoint_type_to_provider.pop(endpoint_type, None)
        self.discovered_runtime_providers.pop(provider_id, None)
        for endpoint_id in endpoint_ids:
            self.runtime.remove_endpoint_hub(endpoint_id)
        lifecycle_errors = stop_errors + unload_errors
        if lifecycle_errors:
            raise RuntimeError("; ".join(lifecycle_errors))

    def _clear_runtime_providers(self) -> None:
        for provider_id in sorted(tuple(self.discovered_runtime_providers)):
            self._remove_discovered_provider(provider_id, reason="provider_manager_clear")

    async def stop_async(self) -> None:
        errors: list[str] = []
        for hub in self.runtime.list_endpoint_hubs():
            try:
                self.runtime.withdraw_endpoint(hub.endpoint_id)
            except Exception as exc:
                errors.append(
                    f"withdraw {hub.endpoint_id}: {exc.__class__.__name__}: {exc}"
                )
        try:
            await self.runtime.stop_async()
        except Exception as exc:
            errors.append(f"transports: {exc.__class__.__name__}: {exc}")
        for provider_id in sorted(tuple(self.runtime_provider_handles)):
            handle = self.runtime_provider_handles.pop(provider_id)
            self.unregister_provider(provider_id)
            discovered = self.discovered_runtime_providers.get(provider_id)
            if discovered is not None:
                for endpoint_type in discovered.endpoint_types:
                    self.endpoint_type_to_provider[endpoint_type] = provider_id
            errors.extend(await _dispose_runtime_provider_handle_async(handle))
        self.shutdown_errors = errors


def build_default_channel_provider_manager(
    *,
    runtime: ChannelRuntime,
    repository: ChannelEndpointRepository,
    runtime_root: Path,
) -> ChannelEndpointProviderManager:
    manager = ChannelEndpointProviderManager(
        runtime=runtime,
        repository=repository,
        runtime_root=runtime_root,
    )
    socket_factory = SocketChannelEndpointFactory()
    manager.register_provider(
        FactoryChannelProvider(
            provider_id="socket",
            endpoint_types=(socket_factory.channel_kind,),
            factory=socket_factory,
            reload_modules=socket_factory.reload_modules,
        )
    )
    return manager


def _runtime_provider_dirs(runtime_root: Path) -> tuple[Path, ...]:
    # Channel providers are external runtime-owned components. Keeping this
    # search rooted exclusively in the selected runtime prevents a packaged
    # site-packages copy and a deployed copy from drifting independently.
    return (runtime_root / RUNTIME_CHANNEL_PROVIDER_DIR,)


def _iter_runtime_provider_manifest_paths(providers_dir: Path) -> tuple[Path, ...]:
    if not providers_dir.exists():
        return ()
    paths: list[Path] = []
    root_manifest = providers_dir / CHANNEL_PROVIDER_MANIFEST_FILENAME
    if root_manifest.is_file():
        paths.append(root_manifest)
    for item in sorted(providers_dir.iterdir(), key=lambda path: path.name):
        if _is_provider_archive_directory(item.name):
            continue
        manifest_path = item / CHANNEL_PROVIDER_MANIFEST_FILENAME
        if item.is_dir() and manifest_path.is_file():
            paths.append(manifest_path)
    return tuple(paths)


def _is_provider_archive_directory(name: str) -> bool:
    normalized = str(name or "").lower()
    return (
        normalized.startswith(".")
        or ".bak" in normalized
        or ".backup" in normalized
        or "-backup-" in normalized
        or ".packaged-backup-" in normalized
    )


def _read_runtime_provider_manifest(manifest_path: Path) -> RuntimeChannelProviderManifest:
    payload = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    provider_id = str(payload.get("provider_id") or manifest_path.parent.name).strip()
    if not provider_id:
        raise ValueError("provider_id is required")
    reload_modules = payload.get("reload_modules", ())
    return RuntimeChannelProviderManifest(
        provider_id=provider_id,
        entrypoint=str(payload.get("entrypoint") or "provider.py"),
        version=str(payload.get("version") or "0.1.0"),
        enabled=bool(payload.get("enabled", payload.get("enabled_by_default", True))),
        filesystem_path=str(manifest_path.parent),
        reload_modules=tuple(str(item) for item in reload_modules if str(item).strip())
        if isinstance(reload_modules, list | tuple)
        else (),
        config=dict(payload),
    )


def _runtime_provider_entrypoint_path(manifest: RuntimeChannelProviderManifest) -> Path:
    provider_dir = Path(manifest.filesystem_path).resolve()
    raw_entrypoint = str(manifest.entrypoint or "").strip() or "provider.py"
    candidate = Path(raw_entrypoint)
    if candidate.is_absolute():
        entrypoint_path = candidate.resolve()
    elif candidate.suffix == ".py" or len(candidate.parts) > 1:
        entrypoint_path = (provider_dir / candidate).resolve()
    else:
        module_path = provider_dir.joinpath(*raw_entrypoint.split("."))
        entrypoint_path = module_path.with_suffix(".py").resolve()
        if not entrypoint_path.is_file():
            entrypoint_path = (module_path / "__init__.py").resolve()
    try:
        entrypoint_path.relative_to(provider_dir)
    except ValueError as exc:
        raise ValueError(f"provider entrypoint must be inside provider directory: {entrypoint_path}") from exc
    if not entrypoint_path.is_file():
        raise FileNotFoundError(entrypoint_path)
    return entrypoint_path


def _runtime_provider_module_name(
    entrypoint_path: Path,
    *,
    root: Path,
    provider_id: str,
) -> str:
    provider_stem = re.sub(r"[^0-9A-Za-z_]+", "_", provider_id)
    module_stem = re.sub(r"[^0-9A-Za-z_]+", "_", entrypoint_path.stem)
    return f"_pal_runtime_channel_provider_{provider_stem}.{module_stem}"


def _load_source_module(module_name: str, module_path: Path) -> types.ModuleType:
    provider_dir = module_path.parent.resolve()
    package_name, separator, _ = module_name.rpartition(".")
    if not separator:
        package_name = f"{module_name}_package"
        module_name = f"{package_name}.{module_path.stem}"
    package = sys.modules.get(package_name)
    created_package = package is None
    if package is None:
        package = types.ModuleType(package_name)
        package.__file__ = str(provider_dir)
        package.__package__ = package_name
        package.__path__ = [str(provider_dir)]  # type: ignore[attr-defined]
        package.__spec__ = importlib.util.spec_from_loader(
            package_name,
            loader=None,
            is_package=True,
        )
        sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not create import spec for channel provider: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        if created_package:
            sys.modules.pop(package_name, None)
        raise
    return module


def _resolve_provider_awaitable(value: Any, *, runtime: ChannelRuntime) -> Any:
    if not inspect.isawaitable(value):
        return value
    owner_loop = getattr(runtime, "_loop", None)
    if owner_loop is not None and owner_loop.is_running():
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is owner_loop:
            raise RuntimeError(
                "async provider lifecycle cannot block its owner event loop"
            )
        return asyncio.run_coroutine_threadsafe(value, owner_loop).result(timeout=10.0)
    return asyncio.run(value)


def _invoke_provider_lifecycle(
    provider: Any,
    lifecycle: str,
    context: ChannelProviderBuildContext,
    *,
    runtime: ChannelRuntime,
) -> Any:
    hook = getattr(provider, lifecycle, None)
    if not callable(hook):
        return None
    signature = inspect.signature(hook)
    accepts_context = any(
        parameter.kind
        in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        }
        for parameter in signature.parameters.values()
    )
    value = hook(context) if accepts_context else hook()
    return _resolve_provider_awaitable(value, runtime=runtime)


def _run_cleanup_callbacks(
    callbacks: list[Callable[[], Any]],
    *,
    runtime: ChannelRuntime,
) -> list[str]:
    errors: list[str] = []
    while callbacks:
        callback = callbacks.pop()
        try:
            _resolve_provider_awaitable(callback(), runtime=runtime)
        except Exception as exc:
            errors.append(f"cleanup: {exc.__class__.__name__}: {exc}")
    return errors


def _remove_provider_modules(module_names: tuple[str, ...]) -> None:
    importlib.invalidate_caches()
    for module_name in sorted(set(module_names), key=lambda item: (-item.count("."), item)):
        sys.modules.pop(module_name, None)


def _dispose_runtime_provider_handle(
    handle: RuntimeChannelProviderHandle,
    *,
    runtime: ChannelRuntime,
) -> list[str]:
    errors: list[str] = []
    context = handle.lifecycle_context
    if context is not None and handle.attached:
        try:
            _invoke_provider_lifecycle(
                handle.provider,
                "detach",
                context,
                runtime=runtime,
            )
        except Exception as exc:
            errors.append(f"detach: {exc.__class__.__name__}: {exc}")
    errors.extend(_run_cleanup_callbacks(handle.cleanup_callbacks, runtime=runtime))
    _remove_provider_modules(handle.module_names)
    handle.attached = False
    return errors


async def _resolve_provider_awaitable_async(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _dispose_runtime_provider_handle_async(
    handle: RuntimeChannelProviderHandle,
) -> list[str]:
    errors: list[str] = []
    context = handle.lifecycle_context
    if context is not None and handle.attached:
        hook = getattr(handle.provider, "detach", None)
        if callable(hook):
            try:
                signature = inspect.signature(hook)
                accepts_context = any(
                    parameter.kind
                    in {
                        inspect.Parameter.POSITIONAL_ONLY,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.VAR_POSITIONAL,
                    }
                    for parameter in signature.parameters.values()
                )
                await _resolve_provider_awaitable_async(
                    hook(context) if accepts_context else hook()
                )
            except Exception as exc:
                errors.append(f"detach: {exc.__class__.__name__}: {exc}")
    while handle.cleanup_callbacks:
        callback = handle.cleanup_callbacks.pop()
        try:
            await _resolve_provider_awaitable_async(callback())
        except Exception as exc:
            errors.append(f"cleanup: {exc.__class__.__name__}: {exc}")
    _remove_provider_modules(handle.module_names)
    handle.attached = False
    return errors


def channel_endpoint_data_root(runtime_root: Path, endpoint_id: str) -> Path:
    normalized = str(endpoint_id or "").strip()
    if not normalized:
        raise ValueError("channel endpoint_id is required for provider data")
    if normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
        raise ValueError(f"channel endpoint_id is not a safe data directory name: {normalized!r}")
    return Path(runtime_root) / RUNTIME_CHANNEL_DATA_DIR / normalized


def _provider_from_module(module: types.ModuleType, *, context: ChannelProviderBuildContext) -> ChannelProvider:
    factory = getattr(module, "build_channel_provider", None)
    if factory is None:
        factory = getattr(module, "build_provider", None)
    if callable(factory):
        provider = _call_runtime_provider_factory(factory, context=context)
    else:
        provider = getattr(module, "CHANNEL_PROVIDER", None)
        if provider is None:
            provider = getattr(module, "PROVIDER", None)
    if provider is None:
        raise TypeError("provider module must export build_channel_provider(context) or CHANNEL_PROVIDER")
    if isinstance(provider, type):
        provider = provider()
    if not hasattr(provider, "provider_id") or not hasattr(provider, "endpoint_types"):
        raise TypeError("channel provider must expose provider_id and endpoint_types")
    return provider


def _call_runtime_provider_factory(factory: Any, *, context: ChannelProviderBuildContext) -> ChannelProvider:
    signature = inspect.signature(factory)
    parameters = signature.parameters
    accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values())
    named_values = {
        "context": context,
        "manager": context.manager,
        "runtime_root": context.runtime_root,
        "provider_dir": context.provider_dir,
        "manifest": context.manifest,
    }
    kwargs = {
        key: value
        for key, value in named_values.items()
        if accepts_kwargs or key in parameters
    }
    if kwargs:
        return factory(**kwargs)
    required_positional = [
        param
        for param in parameters.values()
        if param.default is inspect.Parameter.empty
        and param.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    ]
    if len(required_positional) == 1:
        return factory(context)
    return factory()


def _module_loaded_from(module_name: str, root: Path) -> bool:
    module = sys.modules.get(module_name)
    if module is None:
        return False
    raw_file = getattr(module, "__file__", None)
    if not raw_file:
        return False
    try:
        module_path = Path(str(raw_file)).resolve()
        return module_path == root or module_path.is_relative_to(root)
    except Exception:
        return False


def _endpoint_snapshot(
    endpoint_id: str,
    record: ChannelEndpointModel | None,
    endpoint: ChannelEndpointBase | None,
) -> dict[str, Any]:
    endpoint_type = record.channel_kind if record is not None else endpoint.endpoint.channel_kind if endpoint is not None else ""
    binding_key = record.binding_key if record is not None else endpoint.endpoint.binding_key if endpoint is not None else ""
    enabled = bool(record.enabled) if record is not None else bool(endpoint.enabled) if endpoint is not None else False
    attached = (
        bool(endpoint.attached)
        if endpoint is not None
        else record.detached_at is None if record is not None
        else False
    )
    return {
        "endpoint_id": endpoint_id,
        "endpoint_type": endpoint_type,
        "channel_kind": endpoint_type,
        "binding_key": binding_key,
        "enabled": enabled,
        "attached": attached,
        "paired": bool(getattr(endpoint, "paired", False)) if endpoint is not None else False,
        "runtime_endpoint_present": endpoint is not None,
    }


def recovery_socket_path(runtime_root: Path) -> Path:
    return Path(runtime_root).expanduser() / "pal.sock"


def _normalized_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve(strict=False)


def is_recovery_socket_endpoint(
    record: ChannelEndpointModel | None,
    endpoint: ChannelEndpointBase | None,
    runtime_root: Path,
) -> bool:
    endpoint_type = (
        record.channel_kind
        if record is not None
        else endpoint.endpoint.channel_kind
        if endpoint is not None
        else ""
    )
    if str(endpoint_type or "").strip() != "socket":
        return False
    binding_key = (
        record.binding_key
        if record is not None
        else endpoint.endpoint.binding_key
        if endpoint is not None
        else ""
    )
    if not str(binding_key or "").strip():
        return False
    return _normalized_path(binding_key) == _normalized_path(recovery_socket_path(runtime_root))


def commit_channel_endpoint_attach(
    endpoint_id: str,
    endpoint: ChannelEndpointBase,
    context: ChannelProviderContext,
) -> ChannelEndpointModel:
    """Install a transport and commit its row behind the manager's hub fence."""
    old_endpoint = context.runtime.get_endpoint(endpoint_id)
    context.runtime.replace_endpoint(endpoint)
    try:
        record = context.repository.set_attached(endpoint_id, True)
        if record is None:
            raise RuntimeError(f"channel endpoint disappeared during attach: {endpoint_id}")
    except Exception:
        if old_endpoint is None:
            context.runtime.remove_endpoint(endpoint_id)
        else:
            context.runtime.replace_endpoint(old_endpoint)
        raise
    return record


def commit_channel_endpoint_detach(
    endpoint_id: str,
    context: ChannelProviderContext,
) -> tuple[ChannelEndpointModel | None, ChannelEndpointBase | None, bool]:
    """Stop the transport and commit its row after capability withdrawal."""
    endpoint = context.runtime.get_endpoint(endpoint_id)
    original_record = context.repository.get(endpoint_id)
    removed = context.runtime.remove_endpoint(endpoint_id) if endpoint is not None else False
    try:
        record = context.repository.set_attached(endpoint_id, False) if original_record is not None else None
        if original_record is not None and record is None:
            raise RuntimeError(f"channel endpoint disappeared during detach: {endpoint_id}")
    except Exception:
        if removed and endpoint is not None:
            context.runtime.replace_endpoint(endpoint)
        raise
    if endpoint is not None:
        endpoint.detach()
    return record, endpoint, removed


def _ok(text: str, payload: dict[str, Any]) -> IntrospectionResult:
    return IntrospectionResult(
        status=RuntimeStatus.OK,
        text=text,
        structured=payload,
        llm_text=render_titled_structured_for_llm(text, payload),
    )


def _not_found(endpoint_id: str) -> IntrospectionResult:
    return IntrospectionResult(
        status=RuntimeStatus.NOT_FOUND,
        text="channel endpoint not found",
        structured={"endpoint_id": endpoint_id},
        llm_text="channel endpoint not found",
    )


def _provider_missing(endpoint_id: str, endpoint_type: str) -> IntrospectionResult:
    return IntrospectionResult(
        status=RuntimeStatus.NOT_FOUND,
        text="channel provider not found",
        structured={"endpoint_id": endpoint_id, "endpoint_type": endpoint_type, "channel_kind": endpoint_type},
        llm_text="channel provider not found",
    )


def _provider_missing_for_endpoint(endpoint_id: str) -> IntrospectionResult:
    return IntrospectionResult(
        status=RuntimeStatus.NOT_FOUND,
        text="channel provider not found",
        structured={"endpoint_id": endpoint_id},
        llm_text="channel provider not found",
    )


def _provider_lifecycle_error(
    action: str,
    provider_id: str,
    endpoint_id: str,
    exc: Exception,
) -> IntrospectionResult:
    payload = {
        "action": action,
        "provider_id": provider_id,
        "endpoint_id": endpoint_id,
        "error_type": exc.__class__.__name__,
        "error": str(exc),
    }
    return IntrospectionResult(
        status=RuntimeStatus.ERROR,
        text=f"channel endpoint {action} failed: {exc}",
        structured=payload,
        llm_text=render_titled_structured_for_llm(
            f"Channel endpoint {action} failed",
            payload,
        ),
    )


def _sanitize_secret_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(payload)
    for key in ("token", "secret", "bot_token"):
        sanitized.pop(key, None)
    return sanitized


def _preserve_runtime_endpoint_state(old_endpoint: ChannelEndpointBase | None, new_endpoint: ChannelEndpointBase) -> None:
    if old_endpoint is None or old_endpoint is new_endpoint:
        return
    if getattr(old_endpoint, "paired", False):
        new_endpoint.paired = True
    pairing_metadata = dict(getattr(old_endpoint, "pairing_metadata", {}) or {})
    if pairing_metadata:
        new_endpoint.pairing_metadata.update(pairing_metadata)
    old_token = str(getattr(old_endpoint, "bot_token", "") or "").strip()
    new_token = str(getattr(new_endpoint, "bot_token", "") or "").strip()
    if old_token and hasattr(new_endpoint, "bot_token") and not new_token:
        setattr(new_endpoint, "bot_token", old_token)
    if hasattr(old_endpoint, "_authorized") and hasattr(new_endpoint, "_authorized"):
        setattr(
            new_endpoint,
            "_authorized",
            bool(getattr(old_endpoint, "_authorized", False)) or bool(getattr(new_endpoint, "_authorized", False)),
        )
    control_commands = list(getattr(old_endpoint, "_control_commands_manifest", []) or [])
    if control_commands and hasattr(new_endpoint, "_control_commands_manifest"):
        setattr(new_endpoint, "_control_commands_manifest", control_commands)
