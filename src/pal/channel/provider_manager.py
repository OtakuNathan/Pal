from __future__ import annotations

import asyncio
import importlib
import importlib.util
import hashlib
import inspect
import re
import sys
import tomllib
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

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
class RuntimeChannelProviderGeneration:
    manifest: RuntimeChannelProviderManifest
    provider: ChannelProvider
    fingerprint: str
    module_names: tuple[str, ...]
    cleanup_callbacks: list[Callable[[], Any]] = field(default_factory=list)
    lifecycle_context: ChannelProviderBuildContext | None = None


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
    runtime_provider_ids: set[str] = field(default_factory=set)
    runtime_module_names: set[str] = field(default_factory=set)
    runtime_provider_manifests: dict[str, RuntimeChannelProviderManifest] = field(default_factory=dict)
    runtime_provider_generations: dict[str, RuntimeChannelProviderGeneration] = field(default_factory=dict)
    runtime_provider_load_errors: list[str] = field(default_factory=list)

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
        for provider in sorted(self.providers.values(), key=lambda item: item.provider_id):
            manifest = self.runtime_provider_manifests.get(provider.provider_id)
            row = {
                "provider_id": provider.provider_id,
                "endpoint_types": list(provider.endpoint_types),
                "reload_modules": list(getattr(provider, "reload_modules", ()) or ()),
                "source": "runtime_root" if provider.provider_id in self.runtime_provider_ids else "registered",
            }
            if manifest is not None:
                generation = self.runtime_provider_generations.get(provider.provider_id)
                row.update(
                    {
                        "version": manifest.version,
                        "filesystem_path": manifest.filesystem_path,
                        "entrypoint": manifest.entrypoint,
                        "generation_fingerprint": (
                            generation.fingerprint if generation is not None else ""
                        ),
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

    def hydrate_all(self, *, exclude_endpoint_ids: set[str] | None = None) -> list[str]:
        hydrated: list[str] = []
        excluded = set(exclude_endpoint_ids or ())
        for record in self.repository.list_all():
            if record.endpoint_id in excluded:
                continue
            if not bool(record.enabled) or record.detached_at is not None:
                continue
            provider = self.provider_for_endpoint_type(record.channel_kind)
            if provider is None:
                continue
            result = provider.attach_endpoint(record.endpoint_id, self.context())
            if result.status == RuntimeStatus.OK:
                hydrated.append(record.endpoint_id)
        return hydrated

    def hydrate_provider(self, provider_id: str) -> list[str]:
        provider = self.providers.get(str(provider_id or "").strip())
        if provider is None:
            return []
        hydrated: list[str] = []
        for endpoint_type in provider.endpoint_types:
            for record in self.repository.list_all(channel_kind=endpoint_type):
                if not bool(record.enabled) or record.detached_at is not None:
                    continue
                result = provider.attach_endpoint(record.endpoint_id, self.context())
                if result.status == RuntimeStatus.OK:
                    hydrated.append(record.endpoint_id)
        return hydrated

    def detach_provider_endpoints(self, provider_id: str) -> list[str]:
        provider = self.providers.get(str(provider_id or "").strip())
        if provider is None:
            return []
        detached: list[str] = []
        for endpoint_type in provider.endpoint_types:
            for record in self.repository.list_all(channel_kind=endpoint_type):
                result = provider.detach_endpoint(record.endpoint_id, self.context())
                if result.status == RuntimeStatus.OK:
                    detached.append(record.endpoint_id)
        return detached

    def unload_provider_endpoints(self, provider_id: str) -> list[str]:
        provider = self.providers.get(str(provider_id or "").strip())
        if provider is None:
            return []
        endpoint_types = set(provider.endpoint_types)
        removed: list[str] = []
        for endpoint in self.runtime.list_endpoints():
            if endpoint.endpoint.channel_kind not in endpoint_types:
                continue
            endpoint.detach()
            if self.runtime.remove_endpoint(endpoint.endpoint.endpoint_id):
                removed.append(endpoint.endpoint.endpoint_id)
        return removed

    def attach_endpoint(self, endpoint_id: str) -> IntrospectionResult:
        provider = self.provider_for_endpoint(endpoint_id)
        if provider is None:
            return _provider_missing_for_endpoint(endpoint_id)
        self.runtime.bind_endpoint_provider(endpoint_id, provider.provider_id)
        return provider.attach_endpoint(endpoint_id, self.context())

    def detach_endpoint(self, endpoint_id: str) -> IntrospectionResult:
        provider = self.provider_for_endpoint(endpoint_id)
        if provider is None:
            return _provider_missing_for_endpoint(endpoint_id)
        return provider.detach_endpoint(endpoint_id, self.context())

    def restart_endpoint(self, endpoint_id: str) -> IntrospectionResult:
        provider = self.provider_for_endpoint(endpoint_id)
        if provider is None:
            return _provider_missing_for_endpoint(endpoint_id)
        self.runtime.bind_endpoint_provider(endpoint_id, provider.provider_id)
        return provider.restart_endpoint(endpoint_id, self.context())

    def inspect_endpoint(self, endpoint_id: str) -> IntrospectionResult:
        provider = self.provider_for_endpoint(endpoint_id)
        if provider is None:
            return _provider_missing_for_endpoint(endpoint_id)
        return self._with_delivery_slot(
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
        return self._with_delivery_slot(
            endpoint_id,
            provider.inspect_backlog(endpoint_id, self.context()),
        )

    def inspect_health(self, endpoint_id: str) -> IntrospectionResult:
        provider = self.provider_for_endpoint(endpoint_id)
        if provider is None:
            return _provider_missing_for_endpoint(endpoint_id)
        return self._with_delivery_slot(
            endpoint_id,
            provider.inspect_health(endpoint_id, self.context()),
        )

    def _with_delivery_slot(
        self,
        endpoint_id: str,
        result: IntrospectionResult,
    ) -> IntrospectionResult:
        payload = dict(result.structured or {})
        payload["delivery_slot"] = self.runtime.inspect_delivery_slot(endpoint_id)
        return IntrospectionResult(
            status=result.status,
            text=result.text,
            structured=payload,
            llm_text=render_titled_structured_for_llm(result.text, payload),
        )

    def load_runtime_providers(self) -> dict[str, Any]:
        result = self.rescan_providers(attach_enabled_endpoints=True)
        runtime_result = dict(result.get("runtime_result") or {})
        runtime_result.setdefault("runtime_provider_ids", sorted(self.runtime_provider_ids))
        runtime_result.setdefault(
            "runtime_provider_load_errors", list(self.runtime_provider_load_errors)
        )
        return runtime_result

    def rescan_providers(self, *, attach_enabled_endpoints: bool = False) -> dict[str, Any]:
        before = sorted(self.providers)
        scan = self._scan_runtime_provider_manifests()
        enabled = scan["enabled"]
        disabled = scan["disabled"]
        seen_paths = scan["seen_paths"]
        errors = list(scan["errors"])
        added: list[str] = []
        changed: list[str] = []
        unchanged: list[str] = []
        removed: list[str] = []
        disabled_removed: list[str] = []
        hydrated: list[str] = []
        restored: list[str] = []

        for provider_id in sorted(enabled):
            manifest = enabled[provider_id]
            try:
                fingerprint = _runtime_provider_source_fingerprint(manifest)
                old_generation = self.runtime_provider_generations.get(provider_id)
                if old_generation is not None and old_generation.fingerprint == fingerprint:
                    unchanged.append(provider_id)
                    continue
                candidate = self._build_runtime_provider_generation(
                    manifest,
                    fingerprint=fingerprint,
                )
                transition = self._activate_runtime_provider_generation(
                    candidate,
                    include_configured_endpoints=(
                        attach_enabled_endpoints or old_generation is None
                    ),
                )
                hydrated.extend(transition["hydrated_endpoint_ids"])
                restored.extend(transition["restored_endpoint_ids"])
                if old_generation is None:
                    added.append(provider_id)
                else:
                    changed.append(provider_id)
            except Exception as exc:
                errors.append(
                    f"{manifest.filesystem_path}: {exc.__class__.__name__}: {exc}"
                )

        for provider_id in sorted(self.runtime_provider_generations):
            if provider_id in enabled:
                continue
            generation = self.runtime_provider_generations.get(provider_id)
            if generation is None:
                continue
            expected_manifest = str(
                Path(generation.manifest.filesystem_path)
                / CHANNEL_PROVIDER_MANIFEST_FILENAME
            )
            if provider_id not in disabled and expected_manifest in seen_paths:
                # The manifest exists but could not be parsed. Preserve the
                # known-good generation instead of interpreting damage as removal.
                continue
            try:
                reason = "provider_disabled" if provider_id in disabled else "provider_removed"
                self._deactivate_runtime_provider_generation(provider_id, reason=reason)
                if provider_id in disabled:
                    disabled_removed.append(provider_id)
                else:
                    removed.append(provider_id)
            except Exception as exc:
                errors.append(f"{provider_id}: {exc.__class__.__name__}: {exc}")

        self.runtime_provider_load_errors = errors
        self.scan_errors = list(errors)
        after = sorted(self.providers)
        runtime_result = {
            "runtime_provider_dirs": [str(path) for path in _runtime_provider_dirs(self.runtime_root)],
            "runtime_provider_manifests": sorted(seen_paths),
            "runtime_provider_ids": sorted(self.runtime_provider_ids),
            "loaded_runtime_provider_ids": sorted(added + changed),
            "added_runtime_provider_ids": sorted(added),
            "changed_runtime_provider_ids": sorted(changed),
            "unchanged_runtime_provider_ids": sorted(unchanged),
            "removed_runtime_provider_ids": sorted(removed),
            "disabled_runtime_provider_ids": sorted(disabled),
            "disabled_removed_runtime_provider_ids": sorted(disabled_removed),
            "restored_runtime_endpoint_ids": sorted(dict.fromkeys(restored)),
            "hydrated_runtime_endpoint_ids": sorted(dict.fromkeys(hydrated)),
            "runtime_provider_load_errors": list(errors),
        }
        return {
            "providers_before": before,
            "providers_after": after,
            "added_provider_ids": sorted(added),
            "changed_provider_ids": sorted(changed),
            "unchanged_provider_ids": sorted(unchanged),
            "removed_provider_ids": sorted(removed + disabled_removed),
            "provider_count": len(after),
            "endpoint_type_map": dict(sorted(self.endpoint_type_to_provider.items())),
            "hydrated_endpoint_ids": sorted(dict.fromkeys(hydrated)),
            "restored_endpoint_ids": sorted(dict.fromkeys(restored)),
            "plugin_result": {},
            "runtime_result": runtime_result,
            "runtime_provider_ids": sorted(self.runtime_provider_ids),
            "runtime_provider_load_errors": list(errors),
            "scan_errors": list(self.scan_errors),
        }

    def reload_provider(self, provider_id: str) -> IntrospectionResult:
        normalized = str(provider_id or "").strip()
        generation = self.runtime_provider_generations.get(normalized)
        if generation is None:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="channel provider does not support runtime reload",
                structured={"provider_id": normalized, "reason": "provider_not_runtime_owned"},
                llm_text="channel provider does not support runtime reload",
            )
        manifest_path = (
            Path(generation.manifest.filesystem_path) / CHANNEL_PROVIDER_MANIFEST_FILENAME
        )
        try:
            manifest = _read_runtime_provider_manifest(manifest_path)
            if manifest.provider_id != normalized:
                raise RuntimeError(
                    "channel provider manifest id changed during reload: "
                    f"expected {normalized!r}, found {manifest.provider_id!r}"
                )
            if not manifest.enabled:
                raise RuntimeError(f"channel provider is disabled: {normalized}")
            candidate = self._build_runtime_provider_generation(
                manifest,
                fingerprint=_runtime_provider_source_fingerprint(manifest),
            )
            transition = self._activate_runtime_provider_generation(
                candidate,
                include_configured_endpoints=True,
            )
        except Exception as exc:
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
                **transition,
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

    def _build_runtime_provider_generation(
        self,
        manifest: RuntimeChannelProviderManifest,
        *,
        fingerprint: str,
    ) -> RuntimeChannelProviderGeneration:
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
            generation=f"{fingerprint[:12]}_{uuid4().hex[:12]}",
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
            _remove_generation_modules(
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
        return RuntimeChannelProviderGeneration(
            manifest=manifest,
            provider=provider,
            fingerprint=fingerprint,
            module_names=module_names,
            cleanup_callbacks=build_context.cleanup_callbacks,
            lifecycle_context=build_context,
        )

    def _activate_runtime_provider_generation(
        self,
        candidate: RuntimeChannelProviderGeneration,
        *,
        include_configured_endpoints: bool,
    ) -> dict[str, list[str]]:
        provider_id = candidate.manifest.provider_id
        old_generation = self.runtime_provider_generations.get(provider_id)
        old_provider = old_generation.provider if old_generation is not None else None
        old_types = set(getattr(old_provider, "endpoint_types", ()) or ())
        candidate_types = set(candidate.provider.endpoint_types)
        old_endpoints = {
            endpoint.endpoint.endpoint_id: endpoint
            for endpoint in self.runtime.list_endpoints()
            if endpoint.endpoint.channel_kind in old_types
        }
        target_records: dict[str, ChannelEndpointModel] = {}
        for endpoint_id in old_endpoints:
            record = self.repository.get(endpoint_id)
            if (
                record is not None
                and bool(record.enabled)
                and record.detached_at is None
                and record.channel_kind in candidate_types
            ):
                target_records[endpoint_id] = record
        if include_configured_endpoints:
            for record in self.repository.list_all():
                if (
                    record.channel_kind in candidate_types
                    and bool(record.enabled)
                    and record.detached_at is None
                ):
                    target_records[record.endpoint_id] = record
        affected_ids = sorted(set(old_endpoints) | set(target_records))
        self.runtime.begin_provider_reload(affected_ids, provider_id=provider_id)
        replaced_ids: list[str] = []
        removed_ids: list[str] = []
        previous_providers = dict(self.providers)
        previous_type_map = dict(self.endpoint_type_to_provider)
        try:
            lifecycle_context = candidate.lifecycle_context
            if lifecycle_context is None:
                raise RuntimeError(
                    f"provider '{provider_id}' candidate has no lifecycle context"
                )
            lifecycle_cleanup = _invoke_provider_lifecycle(
                candidate.provider,
                "attach",
                lifecycle_context,
                runtime=self.runtime,
            )
            if callable(lifecycle_cleanup):
                lifecycle_context.register_cleanup(lifecycle_cleanup)
            for endpoint_id in sorted(target_records):
                result = candidate.provider.attach_endpoint(endpoint_id, self.context())
                if result.status != RuntimeStatus.OK:
                    raise RuntimeError(
                        f"provider '{provider_id}' could not attach endpoint "
                        f"'{endpoint_id}': {result.text or result.status}"
                    )
                endpoint = self.runtime.get_endpoint(endpoint_id)
                if endpoint is None:
                    raise RuntimeError(
                        f"provider '{provider_id}' reported attach success without "
                        f"publishing endpoint '{endpoint_id}'"
                    )
                replaced_ids.append(endpoint_id)
            for endpoint_id in sorted(set(old_endpoints) - set(target_records)):
                if self.runtime.remove_endpoint(endpoint_id):
                    removed_ids.append(endpoint_id)
            self.unregister_provider(provider_id)
            self.register_provider(candidate.provider)
            self.runtime_provider_generations[provider_id] = candidate
            self.runtime_provider_ids.add(provider_id)
            self.runtime_provider_manifests[provider_id] = candidate.manifest
            self.runtime_module_names.update(candidate.module_names)
        except Exception as exc:
            self.providers.clear()
            self.providers.update(previous_providers)
            self.endpoint_type_to_provider.clear()
            self.endpoint_type_to_provider.update(previous_type_map)
            rollback_errors: list[str] = []
            for endpoint_id in reversed(replaced_ids):
                old_endpoint = old_endpoints.get(endpoint_id)
                try:
                    if old_endpoint is None:
                        self.runtime.remove_endpoint(endpoint_id)
                    else:
                        self.runtime.replace_endpoint(old_endpoint, manage_reload=False)
                except Exception as rollback_exc:
                    rollback_errors.append(
                        f"{endpoint_id}: {rollback_exc.__class__.__name__}: {rollback_exc}"
                    )
            for endpoint_id in removed_ids:
                old_endpoint = old_endpoints.get(endpoint_id)
                if old_endpoint is not None:
                    try:
                        self.runtime.replace_endpoint(old_endpoint, manage_reload=False)
                    except Exception as rollback_exc:
                        rollback_errors.append(
                            f"{endpoint_id}: {rollback_exc.__class__.__name__}: {rollback_exc}"
                        )
            _dispose_runtime_provider_generation(candidate, runtime=self.runtime)
            if rollback_errors:
                rollback_reason = "; ".join(rollback_errors)
                for endpoint_id in affected_ids:
                    self.runtime.fail_endpoint_reload(endpoint_id, rollback_reason)
                raise RuntimeError(
                    f"provider '{provider_id}' reload failed and rollback was incomplete: "
                    f"{rollback_reason}"
                ) from exc
            self.runtime.complete_provider_reload(sorted(old_endpoints))
            for endpoint_id in sorted(set(affected_ids) - set(old_endpoints)):
                self.runtime.mark_endpoint_detached(
                    endpoint_id,
                    reason=f"provider_reload_rolled_back: {exc}",
                )
            raise
        if old_generation is not None:
            _dispose_runtime_provider_generation(old_generation, runtime=self.runtime)
            self.runtime_module_names.difference_update(old_generation.module_names)
        self.runtime.complete_provider_reload(sorted(target_records))
        for endpoint_id in removed_ids:
            self.runtime.mark_endpoint_detached(
                endpoint_id,
                reason="provider_endpoint_type_removed",
            )
        return {
            "hydrated_endpoint_ids": sorted(set(target_records) - set(old_endpoints)),
            "restored_endpoint_ids": sorted(set(target_records) & set(old_endpoints)),
            "removed_endpoint_ids": removed_ids,
        }

    def _deactivate_runtime_provider_generation(self, provider_id: str, *, reason: str) -> None:
        generation = self.runtime_provider_generations.get(provider_id)
        if generation is None:
            return
        endpoint_types = set(generation.provider.endpoint_types)
        endpoint_ids = sorted(
            endpoint.endpoint.endpoint_id
            for endpoint in self.runtime.list_endpoints()
            if endpoint.endpoint.channel_kind in endpoint_types
        )
        self.runtime.begin_provider_reload(endpoint_ids, provider_id=provider_id)
        for endpoint_id in endpoint_ids:
            self.runtime.remove_endpoint(endpoint_id)
        self.unregister_provider(provider_id)
        self.runtime_provider_generations.pop(provider_id, None)
        self.runtime_provider_ids.discard(provider_id)
        self.runtime_provider_manifests.pop(provider_id, None)
        self.runtime_module_names.difference_update(generation.module_names)
        _dispose_runtime_provider_generation(generation, runtime=self.runtime)
        for endpoint_id in endpoint_ids:
            self.runtime.mark_endpoint_detached(endpoint_id, reason=reason)

    def _clear_runtime_providers(self) -> None:
        for provider_id in sorted(self.runtime_provider_ids):
            self._deactivate_runtime_provider_generation(
                provider_id,
                reason="provider_manager_clear",
            )

    def _load_runtime_provider_manifest(self, manifest: RuntimeChannelProviderManifest) -> None:
        candidate = self._build_runtime_provider_generation(
            manifest,
            fingerprint=_runtime_provider_source_fingerprint(manifest),
        )
        self._activate_runtime_provider_generation(
            candidate,
            include_configured_endpoints=True,
        )


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
        manifest_path = item / CHANNEL_PROVIDER_MANIFEST_FILENAME
        if item.is_dir() and manifest_path.is_file():
            paths.append(manifest_path)
    return tuple(paths)


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
    generation: str = "default",
) -> str:
    provider_stem = re.sub(r"[^0-9A-Za-z_]+", "_", provider_id)
    module_stem = re.sub(r"[^0-9A-Za-z_]+", "_", entrypoint_path.stem)
    generation_stem = re.sub(r"[^0-9A-Za-z_]+", "_", generation)
    return (
        f"_pal_runtime_channel_provider_{provider_stem}_{generation_stem}."
        f"{module_stem}"
    )


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


_PROVIDER_FINGERPRINT_IGNORED_NAMES = {
    ".git",
    "__pycache__",
    "build",
    "dist",
    ".pal-provider-install.json",
    ".desktop-avatar-install.json",
}


def _runtime_provider_source_fingerprint(
    manifest: RuntimeChannelProviderManifest,
) -> str:
    provider_dir = Path(manifest.filesystem_path).resolve()
    digest = hashlib.sha256()
    for path in sorted(
        (item for item in provider_dir.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(provider_dir).as_posix(),
    ):
        relative = path.relative_to(provider_dir)
        if any(
            part in _PROVIDER_FINGERPRINT_IGNORED_NAMES or part.endswith(".egg-info")
            for part in relative.parts
        ):
            continue
        if path.suffix in {".pyc", ".pyo"} or any(
            part.startswith(".tmp") or ".bak" in part for part in relative.parts
        ):
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


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
) -> None:
    while callbacks:
        callback = callbacks.pop()
        try:
            _resolve_provider_awaitable(callback(), runtime=runtime)
        except Exception:
            pass


def _remove_generation_modules(module_names: tuple[str, ...]) -> None:
    importlib.invalidate_caches()
    for module_name in sorted(set(module_names), key=lambda item: (-item.count("."), item)):
        sys.modules.pop(module_name, None)


def _dispose_runtime_provider_generation(
    generation: RuntimeChannelProviderGeneration,
    *,
    runtime: ChannelRuntime,
) -> None:
    context = generation.lifecycle_context
    if context is not None:
        try:
            _invoke_provider_lifecycle(
                generation.provider,
                "detach",
                context,
                runtime=runtime,
            )
        except Exception:
            pass
    _run_cleanup_callbacks(generation.cleanup_callbacks, runtime=runtime)
    _remove_generation_modules(generation.module_names)


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
    """Publish a started endpoint, then atomically commit its attached row."""
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
    """Stop the runtime endpoint before committing its detached row."""
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
