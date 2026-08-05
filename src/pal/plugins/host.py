from __future__ import annotations

import contextlib
import importlib
import inspect
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pal.core.lifecycle_owner import ModuleLifecycleOwnerResult, lifecycle_owner_not_found
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


def _module_cache_prefixes(
    entrypoint: str,
    *,
    plugin_id: str,
    first_party: bool,
    extra_prefixes: tuple[str, ...] = (),
) -> tuple[str, ...]:
    normalized = str(entrypoint or "").strip()
    if not normalized:
        base: tuple[str, ...] = ()
    elif first_party:
        builtin_prefix = f"pal.plugins_builtin.{plugin_id}"
        if normalized == builtin_prefix or normalized.startswith(f"{builtin_prefix}."):
            base = (builtin_prefix,)
        else:
            base = (normalized,)
    else:
        root = normalized.split(".", 1)[0]
        if root and root != "pal":
            base = (root,)
        else:
            base = (normalized,)
    prefixes: list[str] = []
    for prefix in [*base, *extra_prefixes]:
        clean = str(prefix or "").strip()
        if clean and clean not in prefixes:
            prefixes.append(clean)
    return tuple(prefixes)


def _record_reload_prefixes(record: PluginRecord) -> tuple[str, ...]:
    configured = record.config.get("reload_modules") if isinstance(record.config, dict) else ()
    if not isinstance(configured, list | tuple):
        return ()
    prefixes: list[str] = []
    for item in configured:
        prefix = str(item or "").strip()
        if prefix and prefix not in prefixes:
            prefixes.append(prefix)
    return tuple(prefixes)


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
    module_to_plugin: dict[str, str] = field(default_factory=dict)
    owner_id: str = "plugins"
    first_party_disabled: set[str] = field(default_factory=set)
    scan_errors: list[str] = field(default_factory=list)
    last_scan_status: str = PLUGIN_STATUS_DISCOVERED

    def __post_init__(self) -> None:
        if self.builtin_root is None:
            self.builtin_root = self.runtime_root / "plugins" / "_builtin"
        self.context.lifecycle_owner_registry.register_owner(self)

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
            if not record.enabled and plugin_id in self.first_party_disabled:
                return _plugin_disabled_result(plugin_id)
            status = self._attach_first_party(plugin_id, refresh=True)
            return {
                "status": status,
                "plugin_id": plugin_id,
                "enabled": bool(record.enabled),
                "attached": bool(record.attached),
                "temporary_attach": bool(record.attached) and not bool(record.enabled),
            }
        if plugin_id in self.third_party_handles:
            return {"status": self._attach_community(plugin_id, refresh=True), "plugin_id": plugin_id}
        row = self.third_party_repository.get(plugin_id)
        if row is None:
            return {"status": RuntimeStatus.NOT_FOUND, "plugin_id": plugin_id}
        status = self._load_and_attach_community(plugin_id, refresh=True)
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

    # --- module lifecycle owner ---

    def owns_module(self, module_id: str) -> bool:
        return str(module_id or "").strip() in self.module_to_plugin

    def detach_module(self, module_id: str) -> ModuleLifecycleOwnerResult:
        plugin_id = self.module_to_plugin.get(str(module_id or "").strip())
        if not plugin_id:
            return lifecycle_owner_not_found(module_id, self.owner_id)
        result = self.detach(plugin_id)
        return self._owner_result(module_id, plugin_id, result, fresh_instance=False)

    def attach_module(self, module_id: str) -> ModuleLifecycleOwnerResult:
        plugin_id = self.module_to_plugin.get(str(module_id or "").strip())
        if not plugin_id:
            return lifecycle_owner_not_found(module_id, self.owner_id)
        result = self.attach(plugin_id)
        return self._owner_result(module_id, plugin_id, result, fresh_instance=result.get("status") == RuntimeStatus.OK)

    def reload_module(self, module_id: str) -> ModuleLifecycleOwnerResult:
        return self.attach_module(module_id)

    def enable(self, plugin_id: str) -> dict[str, Any]:
        if plugin_id in self.first_party_records:
            record = self.first_party_records[plugin_id]
            was_disabled = plugin_id in self.first_party_disabled
            previous_enabled = bool(record.enabled)
            self.first_party_disabled.discard(plugin_id)
            record.enabled = True
            if record.attached:
                return {"status": RuntimeStatus.OK, "plugin_id": plugin_id, "enabled": True}
            status = self._load_and_attach_first_party(plugin_id)
            if status != RuntimeStatus.OK:
                record.enabled = previous_enabled
                if was_disabled:
                    self.first_party_disabled.add(plugin_id)
            return {"status": status, "plugin_id": plugin_id, "enabled": bool(record.enabled)}
        original = self.third_party_repository.get(plugin_id)
        if original is None:
            return {"status": RuntimeStatus.NOT_FOUND, "plugin_id": plugin_id}
        previous_enabled = bool(original.enabled)
        row = self.third_party_repository.set_enabled(plugin_id, True)
        if row is None:
            return {"status": RuntimeStatus.NOT_FOUND, "plugin_id": plugin_id}
        status = (
            self._load_and_attach_community(plugin_id)
            if plugin_id not in self.third_party_handles
            else self._attach_community(plugin_id)
        )
        if status != RuntimeStatus.OK:
            self.third_party_repository.set_enabled(plugin_id, previous_enabled)
        return {
            "status": status,
            "plugin_id": plugin_id,
            "enabled": True if status == RuntimeStatus.OK else previous_enabled,
        }

    def disable(self, plugin_id: str) -> dict[str, Any]:
        if plugin_id in self.first_party_records:
            record = self.first_party_records[plugin_id]
            if record.attached:
                status = self._detach_first_party(plugin_id)
                if status != RuntimeStatus.OK:
                    return {"status": status, "plugin_id": plugin_id, "enabled": bool(record.enabled)}
            self.first_party_disabled.add(plugin_id)
            record.enabled = False
            record.last_load_status = PLUGIN_STATUS_DISABLED
            return {"status": RuntimeStatus.OK, "plugin_id": plugin_id, "enabled": False}
        row = self.third_party_repository.get(plugin_id)
        if row is None:
            return {"status": RuntimeStatus.NOT_FOUND, "plugin_id": plugin_id}
        if plugin_id in self.third_party_handles:
            status = self._detach_community(plugin_id)
            if status != RuntimeStatus.OK:
                return {"status": status, "plugin_id": plugin_id, "enabled": bool(row.enabled)}
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
                    config={"reload_modules": list(manifest.reload_modules)},
                )
            else:
                record.entrypoint = manifest.entrypoint
                record.version = manifest.version
                record.filesystem_path = manifest.filesystem_path
                record.config["reload_modules"] = list(manifest.reload_modules)
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
            reload_modules=tuple(str(e) for e in payload.get("reload_modules", []) if str(e).strip())
            if isinstance(payload.get("reload_modules"), list)
            else (),
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
        self._bind_plugin_module(plugin_id, handle)
        record.last_load_status = "loaded"
        record.last_error = None
        self.first_party_handles[plugin_id] = handle
        return RuntimeStatus.OK

    def _attach_first_party(self, plugin_id: str, *, refresh: bool = False) -> str:
        record = self.first_party_records.get(plugin_id)
        if record is None:
            return RuntimeStatus.NOT_FOUND
        try:
            if refresh:
                self._forget_first_party_handle(plugin_id)
            if plugin_id not in self.first_party_handles:
                status = self._instantiate_first_party(plugin_id)
                if status != RuntimeStatus.OK:
                    return status
            handle = self.first_party_handles[plugin_id]
            self._do_attach(handle)
        except Exception as exc:
            failed_handle = self.first_party_handles.get(plugin_id)
            if failed_handle is not None and not failed_handle.mounted:
                self.first_party_handles.pop(plugin_id, None)
                self._forget_module_handle(failed_handle)
            record.attached = bool(failed_handle is not None and failed_handle.mounted)
            record.last_load_status = PLUGIN_STATUS_LOAD_FAILED
            record.last_error = f"{exc.__class__.__name__}: {exc}"
            return RuntimeStatus.ERROR
        record.attached = True
        record.last_load_status = PLUGIN_STATUS_ATTACHED
        record.last_error = None
        return RuntimeStatus.OK

    def _detach_first_party(self, plugin_id: str) -> str:
        handle = self.first_party_handles.get(plugin_id)
        record = self.first_party_records.get(plugin_id)
        if handle is None or record is None:
            return RuntimeStatus.NOT_FOUND
        try:
            self._do_detach(handle)
        except Exception as exc:
            record.last_error = f"{exc.__class__.__name__}: {exc}"
            return RuntimeStatus.ERROR
        record.attached = False
        record.last_load_status = PLUGIN_STATUS_DETACHED
        record.last_error = None
        return RuntimeStatus.OK

    # --- community (third-party) lifecycle ---

    def _load_and_attach_community(self, plugin_id: str, *, refresh: bool = False) -> str:
        if not refresh:
            status = self._instantiate_community(plugin_id)
            if status != RuntimeStatus.OK:
                return status
        return self._attach_community(plugin_id, refresh=refresh)

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
        self._bind_plugin_module(plugin_id, handle)
        self.third_party_handles[plugin_id] = handle
        return RuntimeStatus.OK

    def _attach_community(self, plugin_id: str, *, refresh: bool = False) -> str:
        try:
            if refresh:
                self._forget_community_handle(plugin_id)
            if plugin_id not in self.third_party_handles:
                status = self._instantiate_community(plugin_id)
                if status != RuntimeStatus.OK:
                    return status
            handle = self.third_party_handles[plugin_id]
            self._do_attach(handle)
        except Exception as exc:
            failed_handle = self.third_party_handles.get(plugin_id)
            if failed_handle is not None and not failed_handle.mounted:
                self.third_party_handles.pop(plugin_id, None)
                self._forget_module_handle(failed_handle)
            self.third_party_repository.set_load_status(
                plugin_id, status=PLUGIN_STATUS_LOAD_FAILED,
                error_text=f"{exc.__class__.__name__}: {exc}",
            )
            return RuntimeStatus.ERROR
        self.third_party_repository.set_attached(plugin_id, True)
        self.third_party_repository.set_load_status(plugin_id, status=PLUGIN_STATUS_ATTACHED, error_text=None)
        return RuntimeStatus.OK

    def _detach_community(self, plugin_id: str) -> str:
        handle = self.third_party_handles.get(plugin_id)
        if handle is None:
            return RuntimeStatus.NOT_FOUND
        try:
            self._do_detach(handle)
        except Exception as exc:
            self.third_party_repository.set_load_status(
                plugin_id,
                status=PLUGIN_STATUS_LOAD_FAILED,
                error_text=f"{exc.__class__.__name__}: {exc}",
            )
            return RuntimeStatus.ERROR
        self.third_party_repository.set_attached(plugin_id, False)
        self.third_party_repository.set_load_status(plugin_id, status=PLUGIN_STATUS_DETACHED, error_text=None)
        return RuntimeStatus.OK

    def _forget_first_party_handle(self, plugin_id: str) -> None:
        record = self.first_party_records.get(plugin_id)
        handle = self.first_party_handles.get(plugin_id)
        if handle is not None:
            self._do_detach(handle)
            self.first_party_handles.pop(plugin_id, None)
            self._forget_module_handle(handle)
        if record is not None:
            self._drop_plugin_import_cache(
                record.entrypoint,
                plugin_id=plugin_id,
                first_party=True,
                extra_prefixes=_record_reload_prefixes(record),
            )

    def _forget_community_handle(self, plugin_id: str) -> None:
        handle = self.third_party_handles.get(plugin_id)
        if handle is not None:
            self._do_detach(handle)
            self.third_party_handles.pop(plugin_id, None)
            self._forget_module_handle(handle)
        row = self.third_party_repository.get(plugin_id)
        if row is not None:
            self._drop_plugin_import_cache(
                row.entrypoint,
                plugin_id=plugin_id,
                first_party=False,
                plugin_dir=Path(row.filesystem_path) if row.filesystem_path else None,
            )

    def _bind_plugin_module(self, plugin_id: str, handle: ModuleHandle) -> None:
        module_id = str(handle.module_id)
        record = self.first_party_records.get(plugin_id)
        previous_module_id = str(record.module_id) if record is not None and record.module_id else None
        if previous_module_id and previous_module_id != module_id:
            self.module_to_plugin.pop(previous_module_id, None)
        for existing_module_id, existing_plugin_id in list(self.module_to_plugin.items()):
            if existing_plugin_id == plugin_id and existing_module_id != module_id:
                self.module_to_plugin.pop(existing_module_id, None)
        self.module_to_plugin[module_id] = plugin_id
        if record is not None:
            record.module_id = module_id
        self.context.lifecycle_owner_registry.bind_module(module_id, self.owner_id)

    def _owner_result(
        self,
        module_id: str,
        plugin_id: str,
        result: dict[str, Any],
        *,
        fresh_instance: bool,
    ) -> ModuleLifecycleOwnerResult:
        status = str(result.get("status") or RuntimeStatus.ERROR)
        return ModuleLifecycleOwnerResult(
            status=status,
            module_id=module_id,
            owner_id=self.owner_id,
            fresh_instance=fresh_instance and status == RuntimeStatus.OK,
            reload_modules=self._reload_modules_for_plugin(plugin_id),
            error=str(result.get("error") or result.get("last_error") or "") or None,
            payload={"plugin_id": plugin_id, "plugin_result": dict(result)},
        )

    def _reload_modules_for_plugin(self, plugin_id: str) -> tuple[str, ...]:
        record = self.first_party_records.get(plugin_id)
        if record is not None:
            return _record_reload_prefixes(record)
        row = self.third_party_repository.get(plugin_id)
        if row is None or not isinstance(row.config_blob, dict):
            return ()
        configured = row.config_blob.get("reload_modules")
        if not isinstance(configured, list | tuple):
            return ()
        return tuple(dict.fromkeys(str(item).strip() for item in configured if str(item).strip()))

    def _forget_module_handle(self, handle: ModuleHandle) -> None:
        self.context.unregister_module(handle)

    def _drop_plugin_import_cache(
        self,
        entrypoint: str,
        *,
        plugin_id: str,
        first_party: bool,
        extra_prefixes: tuple[str, ...] = (),
        plugin_dir: Path | None = None,
    ) -> None:
        importlib.invalidate_caches()
        prefixes = _module_cache_prefixes(
            entrypoint,
            plugin_id=plugin_id,
            first_party=first_party,
            extra_prefixes=extra_prefixes,
        )
        root = plugin_dir.resolve() if plugin_dir is not None else None
        for module_name in list(sys.modules):
            if any(module_name == prefix or module_name.startswith(f"{prefix}.") for prefix in prefixes):
                sys.modules.pop(module_name, None)
                continue
            if root is not None and _module_loaded_from(module_name, root):
                sys.modules.pop(module_name, None)

    # --- shared attach/detach logic ---

    def _do_attach(self, handle: ModuleHandle) -> None:
        provider = handle.introspection_provider
        try:
            if provider is not None and hasattr(provider, "attach"):
                provider.attach(IntrospectionCall(name=f"{handle.module_id}.lifecycle.attach"))
            handle.mounted = True
            handle.degraded = False
            self._restore_provider_refs(handle)
            self._restore_prompt_fragment_providers(handle)
            self._restore_event_sources(handle)
            self._restore_event_handlers(handle)
            self._restore_control_action_handlers(handle)
            self._publish_module_capabilities(handle.module_id)
        except Exception:
            self._rollback_failed_attach(handle)
            raise

    def _do_detach(self, handle: ModuleHandle) -> None:
        provider = handle.introspection_provider
        if provider is not None and hasattr(provider, "detach"):
            # The provider owns its resources and must close them before the
            # globally visible projections are removed.  On failure the old
            # generation and bookkeeping remain intact and retryable.
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
        self.context.event_handler_registry.detach_module(handle.module_id)
        self.context.control_action_registry.unregister_module(handle.module_id)
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

    def _restore_control_action_handlers(self, handle: ModuleHandle) -> None:
        for action_kind, handler in handle.control_action_handlers.items():
            self.context.control_action_registry.register(handle.module_id, action_kind, handler)

    # --- capability management ---

    def _publish_module_capabilities(self, module_id: str) -> list[str]:
        handle = self.context.module_registry.require(module_id)
        if handle.introspection_provider is None:
            return []
        if handle.mounted_subtree is None or not handle.mounted_subtree.mounted:
            self.context.execution_runtime.hydrate_module_handle(handle)
        published = self.context.execution_runtime.mount_subtree(handle)
        try:
            handle.published_capabilities = published
            self._register_behavior_declarations(handle)
            return published
        except Exception:
            self.context.execution_runtime.unmount_subtree(handle)
            self._unregister_behavior_declarations(module_id)
            handle.published_capabilities = []
            raise

    def _withdraw_module_capabilities(self, module_id: str) -> list[str]:
        names = list(self.context.capability_registry.by_module.get(module_id, ()))
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

    def _restore_event_handlers(self, handle: ModuleHandle) -> None:
        for event_kind, handlers in handle.event_handlers.items():
            for handler in handlers:
                self.context.event_handler_registry.register(event_kind, handler, module_id=handle.module_id)

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

    def _rollback_failed_attach(self, handle: ModuleHandle) -> None:
        with contextlib.suppress(Exception):
            self._withdraw_module_capabilities(handle.module_id)
        with contextlib.suppress(Exception):
            self.context.prompt_fragment_registry.unregister_module(handle.module_id)
        with contextlib.suppress(Exception):
            self.context.event_source_registry.detach_module(handle.module_id)
        with contextlib.suppress(Exception):
            self.context.event_handler_registry.detach_module(handle.module_id)
        with contextlib.suppress(Exception):
            self.context.control_action_registry.unregister_module(handle.module_id)
        for provider_id in list(handle.provider_refs):
            with contextlib.suppress(Exception):
                self.context.execution_runtime.unregister_provider_ref(provider_id)
            with contextlib.suppress(Exception):
                if self.context.execution_runtime.l3_plugin_registry.get(provider_id) is not None:
                    self.context.execution_runtime.l3_plugin_registry.plugins.pop(provider_id, None)
        provider = handle.introspection_provider
        if provider is not None and hasattr(provider, "detach"):
            with contextlib.suppress(Exception):
                provider.detach(IntrospectionCall(name=f"{handle.module_id}.lifecycle.detach"))
        for cleanup in reversed(list(handle.cleanup_callbacks)):
            with contextlib.suppress(Exception):
                cleanup()
        handle.cleanup_callbacks.clear()
        handle.mounted = False
        handle.degraded = True


def _plugin_disabled_result(plugin_id: str) -> dict[str, Any]:
    return {
        "status": RuntimeStatus.FORBIDDEN,
        "plugin_id": plugin_id,
        "reason": "plugin_disabled",
        "summary": f"plugin is disabled: {plugin_id}",
        "next_action": "plugin_enable",
        "recoverable": True,
        "hint": f"Call plugin_enable with plugin_id={plugin_id} to enable and attach it.",
    }
