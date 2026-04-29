from __future__ import annotations

import importlib
import inspect
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pal.core.module_registry import ModuleHandle
from pal.plugins.contracts import (
    FirstPartyPluginBundle,
    PLUGIN_SOURCE_FIRST_PARTY,
    PLUGIN_SOURCE_THIRD_PARTY,
    PLUGIN_STATUS_ATTACHED,
    PLUGIN_STATUS_DETACHED,
    PLUGIN_STATUS_DISABLED,
    PLUGIN_STATUS_DISCOVERED,
    PLUGIN_STATUS_LOAD_FAILED,
    PLUGIN_STATUS_UNSUPPORTED,
    PluginBuildContext,
    PluginManifest,
    PluginRecord,
)
from pal.plugins.repository import PluginBundleRepository
from pal.shared import IntrospectionCall, RuntimeStatus

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


def _source_plugins_root() -> Path:
    return Path(__file__).resolve().parents[1] / "plugins_builtin"


@dataclass
class PluginHost:
    context: "MainContext"
    runtime_root: Path
    services: dict[str, Any] = field(default_factory=dict)
    third_party_repository: PluginBundleRepository = field(default_factory=PluginBundleRepository)
    builtin_root: Path | None = None
    first_party_records: dict[str, PluginRecord] = field(default_factory=dict)
    first_party_handles: dict[str, ModuleHandle] = field(default_factory=dict)
    third_party_handles: dict[str, ModuleHandle] = field(default_factory=dict)
    first_party_disabled: set[str] = field(default_factory=set)
    scan_errors: list[str] = field(default_factory=list)
    last_scan_status: str = PLUGIN_STATUS_DISCOVERED

    def __post_init__(self) -> None:
        if self.builtin_root is None:
            self.builtin_root = self.runtime_root / "plugins" / "_builtin"

    def third_party_root(self) -> Path:
        return self.runtime_root / "plugins" / "community"

    def bootstrap(self) -> None:
        self.rescan()
        for plugin_id, record in list(self.first_party_records.items()):
            if record.enabled:
                self._load_and_attach_first_party(plugin_id)
        for row in self.third_party_repository.list_all():
            if row.enabled:
                self._load_and_attach_community(row.plugin_id)

    def rescan(self) -> dict[str, Any]:
        self.scan_errors = []
        first_party = self._scan_first_party_manifests()
        third_party = self._scan_third_party_manifests()
        self.last_scan_status = RuntimeStatus.OK if not self.scan_errors else RuntimeStatus.ERROR
        return {
            "first_party_discovered": len(first_party),
            "third_party_discovered": len(third_party),
            "scan_errors": list(self.scan_errors),
        }

    def rescan_and_attach_new_first_party(self) -> dict[str, Any]:
        existing_first_party_ids = set(self.first_party_records)
        attached_before = {
            plugin_id
            for plugin_id, record in self.first_party_records.items()
            if record.attached
        }
        result = self.rescan()
        newly_discovered = [
            plugin_id
            for plugin_id in self.first_party_records
            if plugin_id not in existing_first_party_ids
        ]
        attached_now: list[str] = []
        attach_errors: dict[str, str] = {}
        for plugin_id in newly_discovered:
            record = self.first_party_records.get(plugin_id)
            if record is None or not record.enabled or plugin_id in attached_before:
                continue
            status = self._load_and_attach_first_party(plugin_id)
            if status == RuntimeStatus.OK:
                attached_now.append(plugin_id)
                continue
            attach_errors[plugin_id] = str(record.last_error or status)
        result.update(
            {
                "new_first_party_plugins": newly_discovered,
                "attached_new_first_party_plugins": attached_now,
                "attach_errors": attach_errors,
            }
        )
        return result

    def show_summary(self) -> dict[str, Any]:
        records = self.list_plugins()
        return {
            "first_party_count": len([item for item in records if item["source"] == PLUGIN_SOURCE_FIRST_PARTY]),
            "third_party_count": len([item for item in records if item["source"] == PLUGIN_SOURCE_THIRD_PARTY]),
            "attached_count": len([item for item in records if item["attached"]]),
            "enabled_count": len([item for item in records if item["enabled"]]),
            "scan_status": self.last_scan_status,
            "scan_errors": list(self.scan_errors),
            "builtin_root": str(self.builtin_root),
            "third_party_root": str(self.third_party_root()),
        }

    def list_plugins(self) -> list[dict[str, Any]]:
        items = [record.__dict__.copy() for record in self.first_party_records.values()]
        for row in self.third_party_repository.list_all():
            items.append(
                PluginRecord(
                    plugin_id=row.plugin_id,
                    source=PLUGIN_SOURCE_THIRD_PARTY,
                    entrypoint=row.entrypoint,
                    version=row.version,
                    filesystem_path=row.filesystem_path,
                    enabled=row.enabled,
                    attached=row.attached,
                    last_load_status=row.last_load_status,
                    last_error=row.last_error,
                    module_id=None,
                    config=dict(row.config_blob or {}),
                ).__dict__
            )
        return sorted(items, key=lambda item: (item["source"], item["plugin_id"]))

    def attach(self, plugin_id: str) -> dict[str, Any]:
        if plugin_id in self.first_party_records:
            record = self.first_party_records[plugin_id]
            if not record.enabled:
                return {"status": RuntimeStatus.FORBIDDEN, "plugin_id": plugin_id}
            return {"status": self._attach_first_party(plugin_id), "plugin_id": plugin_id}
        if plugin_id in self.third_party_handles:
            return {"status": self._attach_community(plugin_id), "plugin_id": plugin_id}
        row = self.third_party_repository.set_attached(plugin_id, True)
        if row is None:
            return {"status": RuntimeStatus.NOT_FOUND, "plugin_id": plugin_id}
        status = self._load_and_attach_community(plugin_id)
        return {"status": status, "plugin_id": plugin_id}

    def detach(self, plugin_id: str) -> dict[str, Any]:
        if plugin_id in self.first_party_records:
            return {"status": self._detach_first_party(plugin_id), "plugin_id": plugin_id}
        if plugin_id in self.third_party_handles:
            return {"status": self._detach_community(plugin_id), "plugin_id": plugin_id}
        row = self.third_party_repository.set_attached(plugin_id, False)
        if row is None:
            return {"status": RuntimeStatus.NOT_FOUND, "plugin_id": plugin_id}
        self.third_party_repository.set_load_status(plugin_id, status=PLUGIN_STATUS_DETACHED, error_text=None)
        return {"status": RuntimeStatus.OK, "plugin_id": plugin_id, "attached": False}

    def enable(self, plugin_id: str) -> dict[str, Any]:
        if plugin_id in self.first_party_records:
            self.first_party_disabled.discard(plugin_id)
            self.first_party_records[plugin_id].enabled = True
            if self.first_party_records[plugin_id].attached:
                return {"status": RuntimeStatus.OK, "plugin_id": plugin_id, "enabled": True}
            return {"status": self._load_and_attach_first_party(plugin_id), "plugin_id": plugin_id, "enabled": True}
        row = self.third_party_repository.set_enabled(plugin_id, True)
        if row is None:
            return {"status": RuntimeStatus.NOT_FOUND, "plugin_id": plugin_id}
        if plugin_id not in self.third_party_handles:
            self._load_and_attach_community(plugin_id)
        return {"status": RuntimeStatus.OK, "plugin_id": plugin_id, "enabled": True}

    def disable(self, plugin_id: str) -> dict[str, Any]:
        if plugin_id in self.first_party_records:
            self.first_party_disabled.add(plugin_id)
            self._detach_first_party(plugin_id)
            record = self.first_party_records[plugin_id]
            record.enabled = False
            record.last_load_status = PLUGIN_STATUS_DISABLED
            return {"status": RuntimeStatus.OK, "plugin_id": plugin_id, "enabled": False}
        if plugin_id in self.third_party_handles:
            self._detach_community(plugin_id)
        row = self.third_party_repository.set_enabled(plugin_id, False)
        if row is None:
            return {"status": RuntimeStatus.NOT_FOUND, "plugin_id": plugin_id}
        self.third_party_repository.set_attached(plugin_id, False)
        return {"status": RuntimeStatus.OK, "plugin_id": plugin_id, "enabled": False}

    # --- scanning ---

    def _scan_first_party_manifests(self) -> list[PluginManifest]:
        manifests: list[PluginManifest] = []
        if not self.builtin_root.exists():
            return manifests
        for manifest_path in sorted(self.builtin_root.glob("*/plugin.toml")):
            try:
                manifest = self._read_manifest(manifest_path)
            except Exception as exc:
                self.scan_errors.append(f"{manifest_path}:{exc}")
                continue
            record = self.first_party_records.get(manifest.plugin_id)
            if record is None:
                self.first_party_records[manifest.plugin_id] = PluginRecord(
                    plugin_id=manifest.plugin_id,
                    source=PLUGIN_SOURCE_FIRST_PARTY,
                    entrypoint=manifest.entrypoint,
                    version=manifest.version,
                    filesystem_path=manifest.filesystem_path,
                    enabled=manifest.enabled_by_default,
                    attached=False,
                    last_load_status=PLUGIN_STATUS_DISCOVERED,
                )
            else:
                record.entrypoint = manifest.entrypoint
                record.version = manifest.version
                record.filesystem_path = manifest.filesystem_path
            manifests.append(manifest)
        return manifests

    def _scan_third_party_manifests(self) -> list[PluginManifest]:
        manifests: list[PluginManifest] = []
        root = self.third_party_root()
        root.mkdir(parents=True, exist_ok=True)
        for manifest_path in sorted(root.glob("*/plugin.toml")):
            try:
                manifest = self._read_manifest(manifest_path)
            except Exception as exc:
                self.scan_errors.append(f"{manifest_path}:{exc}")
                continue
            if manifest.plugin_id in self.first_party_records:
                self.third_party_repository.upsert_discovered(
                    plugin_id=manifest.plugin_id,
                    entrypoint=manifest.entrypoint,
                    version=manifest.version,
                    filesystem_path=manifest.filesystem_path,
                    enabled_by_default=False,
                )
                self.third_party_repository.set_load_status(
                    manifest.plugin_id,
                    status=PLUGIN_STATUS_LOAD_FAILED,
                    error_text="plugin_id conflicts with first-party plugin",
                )
            else:
                self.third_party_repository.upsert_discovered(
                    plugin_id=manifest.plugin_id,
                    entrypoint=manifest.entrypoint,
                    version=manifest.version,
                    filesystem_path=manifest.filesystem_path,
                    enabled_by_default=manifest.enabled_by_default,
                )
            manifests.append(manifest)
        return manifests

    def _read_manifest(self, manifest_path: Path) -> PluginManifest:
        payload = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        subscribed = payload.get("subscribed_events", [])
        return PluginManifest(
            plugin_id=str(payload["plugin_id"]),
            entrypoint=str(payload["entrypoint"]),
            version=str(payload["version"]),
            enabled_by_default=bool(payload.get("enabled_by_default", True)),
            filesystem_path=str(manifest_path.parent),
            subscribed_events=tuple(str(e) for e in subscribed) if isinstance(subscribed, list) else (),
        )

    # --- first-party lifecycle ---

    def _load_and_attach_first_party(self, plugin_id: str) -> str:
        if plugin_id in self.first_party_disabled:
            return RuntimeStatus.FORBIDDEN
        status = self._instantiate_first_party(plugin_id)
        if status != RuntimeStatus.OK:
            return status
        return self._attach_first_party(plugin_id)

    def _instantiate_first_party(self, plugin_id: str) -> str:
        if plugin_id in self.first_party_handles:
            return RuntimeStatus.OK
        record = self.first_party_records.get(plugin_id)
        if record is None:
            return RuntimeStatus.NOT_FOUND
        try:
            module = importlib.import_module(record.entrypoint)
            factory = getattr(module, "build_plugin")
            plugin_dir = Path(record.filesystem_path) if record.filesystem_path else None
            instance = self._call_plugin_factory(factory, plugin_dir=plugin_dir)
            handle = instance.register_with_core(self.context)
        except Exception as exc:
            record.last_load_status = PLUGIN_STATUS_LOAD_FAILED
            record.last_error = f"{exc.__class__.__name__}: {exc}"
            return RuntimeStatus.ERROR
        record.module_id = handle.module_id
        record.last_load_status = "loaded"
        record.last_error = None
        self.first_party_handles[plugin_id] = handle
        return RuntimeStatus.OK

    def _attach_first_party(self, plugin_id: str) -> str:
        record = self.first_party_records.get(plugin_id)
        if record is None:
            return RuntimeStatus.NOT_FOUND
        if plugin_id not in self.first_party_handles:
            status = self._instantiate_first_party(plugin_id)
            if status != RuntimeStatus.OK:
                return status
        handle = self.first_party_handles[plugin_id]
        self._do_attach(handle)
        record.attached = True
        record.last_load_status = PLUGIN_STATUS_ATTACHED
        return RuntimeStatus.OK

    def _detach_first_party(self, plugin_id: str) -> str:
        handle = self.first_party_handles.get(plugin_id)
        record = self.first_party_records.get(plugin_id)
        if handle is None or record is None:
            return RuntimeStatus.NOT_FOUND
        self._do_detach(handle)
        record.attached = False
        record.last_load_status = PLUGIN_STATUS_DETACHED
        return RuntimeStatus.OK

    # --- community (third-party) lifecycle ---

    def _load_and_attach_community(self, plugin_id: str) -> str:
        status = self._instantiate_community(plugin_id)
        if status != RuntimeStatus.OK:
            return status
        return self._attach_community(plugin_id)

    def _instantiate_community(self, plugin_id: str) -> str:
        if plugin_id in self.third_party_handles:
            return RuntimeStatus.OK
        row = self.third_party_repository.get(plugin_id)
        if row is None:
            return RuntimeStatus.NOT_FOUND
        plugin_dir = Path(row.filesystem_path)
        if not plugin_dir.exists():
            self.third_party_repository.set_load_status(
                plugin_id, status=PLUGIN_STATUS_LOAD_FAILED,
                error_text=f"plugin directory not found: {plugin_dir}",
            )
            return RuntimeStatus.NOT_FOUND
        try:
            dir_str = str(plugin_dir)
            if dir_str not in sys.path:
                sys.path.insert(0, dir_str)
            module = importlib.import_module(row.entrypoint)
            factory = getattr(module, "build_plugin")
            instance = self._call_plugin_factory(factory, plugin_dir=plugin_dir)
            handle = instance.register_with_core(self.context)
        except Exception as exc:
            self.third_party_repository.set_load_status(
                plugin_id, status=PLUGIN_STATUS_LOAD_FAILED,
                error_text=f"{exc.__class__.__name__}: {exc}",
            )
            return RuntimeStatus.ERROR
        self.third_party_repository.set_load_status(plugin_id, status="loaded", error_text=None)
        self.third_party_handles[plugin_id] = handle
        return RuntimeStatus.OK

    def _attach_community(self, plugin_id: str) -> str:
        if plugin_id not in self.third_party_handles:
            status = self._instantiate_community(plugin_id)
            if status != RuntimeStatus.OK:
                return status
        handle = self.third_party_handles[plugin_id]
        self._do_attach(handle)
        self.third_party_repository.set_attached(plugin_id, True)
        self.third_party_repository.set_load_status(plugin_id, status=PLUGIN_STATUS_ATTACHED, error_text=None)
        return RuntimeStatus.OK

    def _detach_community(self, plugin_id: str) -> str:
        handle = self.third_party_handles.get(plugin_id)
        if handle is None:
            return RuntimeStatus.NOT_FOUND
        self._do_detach(handle)
        self.third_party_repository.set_attached(plugin_id, False)
        self.third_party_repository.set_load_status(plugin_id, status=PLUGIN_STATUS_DETACHED, error_text=None)
        return RuntimeStatus.OK

    # --- shared attach/detach logic ---

    def _do_attach(self, handle: ModuleHandle) -> None:
        provider = handle.introspection_provider
        if provider is not None and hasattr(provider, "attach"):
            provider.attach(IntrospectionCall(name=f"{handle.module_id}.lifecycle.attach"))
        handle.mounted = True
        handle.degraded = False
        self._restore_provider_refs(handle)
        self._restore_prompt_fragment_providers(handle)
        self._restore_event_sources(handle)
        self._publish_module_capabilities(handle.module_id)

    def _do_detach(self, handle: ModuleHandle) -> None:
        provider = handle.introspection_provider
        if provider is not None and hasattr(provider, "detach"):
            provider.detach(IntrospectionCall(name=f"{handle.module_id}.lifecycle.detach"))
        for cleanup in reversed(list(handle.cleanup_callbacks)):
            try:
                cleanup()
            except Exception:
                pass
        handle.cleanup_callbacks.clear()
        handle.mounted = False
        self._withdraw_module_capabilities(handle.module_id)
        self.context.prompt_fragment_registry.unregister_module(handle.module_id)
        self.context.event_source_registry.detach_module(handle.module_id)
        for provider_id in list(handle.provider_refs):
            self.context.execution_runtime.unregister_provider_ref(provider_id)
            if self.context.execution_runtime.l3_plugin_registry.get(provider_id) is not None:
                self.context.execution_runtime.l3_plugin_registry.plugins.pop(provider_id, None)

    # --- factory ---

    def _call_plugin_factory(self, factory, *, plugin_dir: Path | None = None) -> FirstPartyPluginBundle:
        signature = inspect.signature(factory)
        kwargs = {}
        build_context = PluginBuildContext(
            runtime_root=self.runtime_root,
            services=dict(self.services),
            plugin_dir=plugin_dir,
        )
        if "context" in signature.parameters:
            kwargs["context"] = build_context
        if "runtime_root" in signature.parameters:
            kwargs["runtime_root"] = self.runtime_root
        if "plugin_dir" in signature.parameters:
            kwargs["plugin_dir"] = plugin_dir
        for key, value in self.services.items():
            if key in signature.parameters:
                kwargs[key] = value
        return factory(**kwargs)

    # --- capability management ---

    def _publish_module_capabilities(self, module_id: str) -> list[str]:
        handle = self.context.module_registry.require(module_id)
        if handle.introspection_provider is None:
            return []
        published = self.context.execution_runtime.mount_subtree(handle)
        for descriptor_name in published:
            descriptor = self.context.execution_runtime.compiled_capability_index.records[descriptor_name]
            self.context.capability_registry.register(descriptor)
        handle.published_capabilities = published
        self._register_behavior_declarations(handle)
        return published

    def _withdraw_module_capabilities(self, module_id: str) -> list[str]:
        names = self.context.capability_registry.unregister_module(module_id)
        handle = self.context.module_registry.get(module_id)
        if handle is not None:
            self.context.execution_runtime.unmount_subtree(handle)
            self._unregister_behavior_declarations(handle.module_id)
            handle.published_capabilities = []
        return names

    def _restore_provider_refs(self, handle: ModuleHandle) -> None:
        for provider_id in handle.provider_refs:
            provider = handle.ports.get(f"provider:{provider_id}")
            if provider is None:
                continue
            self.context.execution_runtime.register_provider_ref(provider_id, provider)
            if hasattr(provider, "provider_id") and self.context.execution_runtime.l3_plugin_registry.get(provider_id) is None:
                self.context.execution_runtime.l3_plugin_registry.register(provider)

    def _restore_prompt_fragment_providers(self, handle: ModuleHandle) -> None:
        for provider in handle.prompt_fragment_providers:
            self.context.prompt_fragment_registry.register(provider)

    def _restore_event_sources(self, handle: ModuleHandle) -> None:
        for source in handle.event_sources:
            self.context.event_source_registry.attach(handle.module_id, source)

    def _register_behavior_declarations(self, handle: ModuleHandle) -> None:
        skill = self.context.port_registry.get("skill:skill")
        skill_register = getattr(skill, "register_declared_module", None)
        if callable(skill_register):
            skill_register(handle)
        behavior = self.context.port_registry.get("behavior:behavior")
        register = getattr(behavior, "register_declared_module", None)
        if callable(register):
            register(handle)

    def _unregister_behavior_declarations(self, module_id: str) -> None:
        behavior = self.context.port_registry.get("behavior:behavior")
        unregister = getattr(behavior, "unregister_declared_module", None)
        if callable(unregister):
            unregister(module_id)
        skill = self.context.port_registry.get("skill:skill")
        skill_unregister = getattr(skill, "unregister_declared_module", None)
        if callable(skill_unregister):
            skill_unregister(module_id)
