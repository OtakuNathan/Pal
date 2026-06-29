from __future__ import annotations

import importlib
import inspect
import re
import sys
import tomllib
import types
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


@dataclass(frozen=True)
class ChannelProviderBuildContext:
    runtime_root: Path
    provider_dir: Path
    manifest: RuntimeChannelProviderManifest
    manager: Any


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
        record = context.repository.set_attached(endpoint_id, True)
        if record is None:
            return _not_found(endpoint_id)
        endpoint = self.create_endpoint(record, context)
        if endpoint is None:
            return _provider_missing(endpoint_id, record.channel_kind)
        old_endpoint = context.runtime.get_endpoint(endpoint_id)
        _preserve_runtime_endpoint_state(old_endpoint, endpoint)
        endpoint.attached = True
        context.runtime.replace_endpoint(endpoint)
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
        record = context.repository.set_attached(endpoint_id, False)
        if record is None and endpoint is None:
            return _not_found(endpoint_id)
        if endpoint is not None:
            endpoint.detach()
        removed = context.runtime.remove_endpoint(endpoint_id)
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
        _drop_module_import_cache(tuple(self.reload_modules))
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
        self.providers[provider_id] = provider
        for endpoint_type in provider.endpoint_types:
            normalized = str(endpoint_type or "").strip()
            if normalized:
                self.endpoint_type_to_provider[normalized] = provider_id

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

    def hydrate_all(self) -> list[str]:
        hydrated: list[str] = []
        for record in self.repository.list_all():
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
        return provider.restart_endpoint(endpoint_id, self.context())

    def inspect_endpoint(self, endpoint_id: str) -> IntrospectionResult:
        provider = self.provider_for_endpoint(endpoint_id)
        if provider is None:
            return _provider_missing_for_endpoint(endpoint_id)
        return provider.inspect_endpoint(endpoint_id, self.context())

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
        return provider.inspect_backlog(endpoint_id, self.context())

    def inspect_health(self, endpoint_id: str) -> IntrospectionResult:
        provider = self.provider_for_endpoint(endpoint_id)
        if provider is None:
            return _provider_missing_for_endpoint(endpoint_id)
        return provider.inspect_health(endpoint_id, self.context())

    def load_runtime_providers(self) -> dict[str, Any]:
        self._clear_runtime_providers()
        self.runtime_provider_load_errors = []
        manifests_seen: list[str] = []
        loaded_provider_ids: list[str] = []
        disabled_provider_ids: list[str] = []
        primary_dir = self.runtime_root / RUNTIME_CHANNEL_PROVIDER_DIR
        primary_dir.mkdir(parents=True, exist_ok=True)
        for providers_dir in _runtime_provider_dirs(self.runtime_root):
            for manifest_path in _iter_runtime_provider_manifest_paths(providers_dir):
                manifests_seen.append(str(manifest_path))
                try:
                    manifest = _read_runtime_provider_manifest(manifest_path)
                except Exception as exc:
                    self.runtime_provider_load_errors.append(f"{manifest_path}: {exc.__class__.__name__}: {exc}")
                    continue
                if not manifest.enabled:
                    disabled_provider_ids.append(manifest.provider_id)
                    continue
                try:
                    self._load_runtime_provider_manifest(manifest)
                    loaded_provider_ids.append(manifest.provider_id)
                except Exception as exc:
                    self.runtime_provider_load_errors.append(
                        f"{manifest_path}: {exc.__class__.__name__}: {exc}"
                    )
        self.scan_errors = list(self.runtime_provider_load_errors)
        return {
            "runtime_provider_dirs": [str(path) for path in _runtime_provider_dirs(self.runtime_root)],
            "runtime_provider_manifests": manifests_seen,
            "runtime_provider_ids": sorted(self.runtime_provider_ids),
            "loaded_runtime_provider_ids": sorted(dict.fromkeys(loaded_provider_ids)),
            "disabled_runtime_provider_ids": sorted(dict.fromkeys(disabled_provider_ids)),
            "runtime_provider_load_errors": list(self.runtime_provider_load_errors),
        }

    def rescan_providers(self, *, attach_enabled_endpoints: bool = False) -> dict[str, Any]:
        before = sorted(self.providers)
        plugin_result: dict[str, Any] = {}
        plugin_host = self.plugin_host
        if plugin_host is not None:
            rescan_attach = getattr(plugin_host, "rescan_and_attach_new_first_party", None)
            if callable(rescan_attach):
                plugin_result = dict(rescan_attach())
            else:
                rescan = getattr(plugin_host, "rescan", None)
                if callable(rescan):
                    plugin_result = dict(rescan())
        runtime_result = self.load_runtime_providers()
        hydrated = self.hydrate_all() if attach_enabled_endpoints else []
        after = sorted(self.providers)
        return {
            "providers_before": before,
            "providers_after": after,
            "added_provider_ids": sorted(set(after) - set(before)),
            "removed_provider_ids": sorted(set(before) - set(after)),
            "provider_count": len(after),
            "endpoint_type_map": dict(sorted(self.endpoint_type_to_provider.items())),
            "hydrated_endpoint_ids": hydrated,
            "plugin_result": plugin_result,
            "runtime_result": runtime_result,
            "runtime_provider_ids": sorted(self.runtime_provider_ids),
            "runtime_provider_load_errors": list(self.runtime_provider_load_errors),
            "scan_errors": list(self.scan_errors),
        }

    def _clear_runtime_providers(self) -> None:
        provider_roots = [
            Path(manifest.filesystem_path).resolve()
            for manifest in self.runtime_provider_manifests.values()
            if manifest.filesystem_path
        ]
        for provider_id in sorted(self.runtime_provider_ids):
            self.unload_provider_endpoints(provider_id)
            self.unregister_provider(provider_id)
        importlib.invalidate_caches()
        for module_name in list(sys.modules):
            if module_name in self.runtime_module_names:
                sys.modules.pop(module_name, None)
                continue
            if any(_module_loaded_from(module_name, provider_root) for provider_root in provider_roots):
                sys.modules.pop(module_name, None)
        self.runtime_provider_ids.clear()
        self.runtime_module_names.clear()
        self.runtime_provider_manifests.clear()

    def _load_runtime_provider_manifest(self, manifest: RuntimeChannelProviderManifest) -> None:
        provider_id = str(manifest.provider_id or "").strip()
        if not provider_id:
            raise ValueError("provider_id is required")
        if provider_id in self.providers and provider_id not in self.runtime_provider_ids:
            raise ValueError(f"provider_id '{provider_id}' conflicts with an existing channel provider")
        provider_dir = Path(manifest.filesystem_path)
        entrypoint_path = _runtime_provider_entrypoint_path(manifest)
        module_name = _runtime_provider_module_name(entrypoint_path, root=self.runtime_root, provider_id=provider_id)
        module = _load_source_module(module_name, entrypoint_path)
        build_context = ChannelProviderBuildContext(
            runtime_root=self.runtime_root,
            provider_dir=provider_dir,
            manifest=manifest,
            manager=self,
        )
        provider = _provider_from_module(module, context=build_context)
        actual_provider_id = str(getattr(provider, "provider_id", "") or "").strip()
        if actual_provider_id != provider_id:
            raise ValueError(
                f"provider object id '{actual_provider_id}' does not match manifest provider_id '{provider_id}'"
            )
        endpoint_types = tuple(
            dict.fromkeys(str(item or "").strip() for item in getattr(provider, "endpoint_types", ()) if str(item or "").strip())
        )
        if not endpoint_types:
            raise ValueError(f"provider '{provider_id}' must declare at least one endpoint type")
        for endpoint_type in endpoint_types:
            owner_id = self.endpoint_type_to_provider.get(endpoint_type)
            if owner_id and owner_id != provider_id:
                raise ValueError(f"endpoint type '{endpoint_type}' is already owned by provider '{owner_id}'")
        self.register_provider(provider)
        self.runtime_provider_ids.add(provider_id)
        self.runtime_module_names.add(module_name)
        self.runtime_provider_manifests[provider_id] = manifest


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


def _runtime_provider_module_name(entrypoint_path: Path, *, root: Path, provider_id: str) -> str:
    try:
        relative = entrypoint_path.resolve().relative_to(root.resolve())
    except ValueError:
        relative = Path(entrypoint_path.name)
    stem = re.sub(r"[^0-9A-Za-z_]+", "_", str(relative))
    provider_stem = re.sub(r"[^0-9A-Za-z_]+", "_", provider_id)
    return f"_pal_runtime_channel_provider_{provider_stem}_{stem}"


def _load_source_module(module_name: str, module_path: Path) -> types.ModuleType:
    source = module_path.read_text(encoding="utf-8")
    module = types.ModuleType(module_name)
    module.__file__ = str(module_path)
    module.__package__ = ""
    provider_dir = str(module_path.parent)
    inserted = False
    if provider_dir not in sys.path:
        sys.path.insert(0, provider_dir)
        inserted = True
    try:
        sys.modules[module_name] = module
        exec(compile(source, str(module_path), "exec"), module.__dict__)  # noqa: S102
    finally:
        if inserted:
            try:
                sys.path.remove(provider_dir)
            except ValueError:
                pass
    return module


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


def _drop_module_import_cache(prefixes: tuple[str, ...]) -> None:
    clean_prefixes = tuple(dict.fromkeys(str(prefix).strip() for prefix in prefixes if str(prefix).strip()))
    if not clean_prefixes:
        return
    importlib.invalidate_caches()
    for module_name in list(sys.modules):
        if any(module_name == prefix or module_name.startswith(f"{prefix}.") for prefix in clean_prefixes):
            sys.modules.pop(module_name, None)
