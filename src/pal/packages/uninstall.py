"""Resumable removal of community plugins through their existing lifecycle owner."""
import json
import os
from pathlib import Path
import shutil
import tomllib
import time
import uuid

from pal.packages.archive import valid_id
from pal.packages.process import PackageError, atomic_json, check_cancelled, install_lock, runtime_lease, error_text
from pal.plugins.settings import PluginSettings

_REMOVAL_STATES = {"uninstalling", "cleanup_failed", "uninstalled"}
# Shared/resident namespaces are never owned by a community plugin.
_PROTECTED_DATA = {"core", "channel", "security", "bunshin", "bunshin-role", "mcp", "lsp", "web_fetch"}


def removal_record(runtime_root, name):
    path = Path(runtime_root) / "packages/records" / f"plugin-{valid_id(name)}.json"
    if not path.exists():
        return None
    record = json.loads(path.read_text())
    return record if record.get("status") in _REMOVAL_STATES else None


def validate_data_paths(runtime_root, paths, *, other_paths=()):
    if paths is None:
        raise PackageError("Plugin has no [uninstall] data_paths declaration; use ordinary uninstall or add a complete ownership declaration")
    if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
        raise PackageError("uninstall.data_paths must be an array of relative paths")
    root = Path(runtime_root).resolve()
    result = []
    for raw in paths:
        p = Path(raw)
        if (p.is_absolute() or len(p.parts) < 2 or p.parts[0] != "data" or
                ".." in p.parts or any(c in raw for c in "*?[]") or p.parts[1] in _PROTECTED_DATA):
            raise PackageError(f"Unsafe plugin data path: {raw}")
        target = root / p
        current = root
        for part in p.parts:
            current = current / part
            if current.is_symlink():
                raise PackageError(f"Plugin data path traverses a symlink: {raw}")
        for other in other_paths:
            q = root / other
            if target == q or target in q.parents or q in target.parents:
                raise PackageError(f"Plugin data ownership overlaps another plugin: {raw}")
        result.append(p.as_posix())
    return list(dict.fromkeys(result))


def _other_data_paths(root, name):
    from pal.plugins.host import _source_plugins_root
    paths = []
    for base in (root / "plugins/community", root / "plugins/_builtin", _source_plugins_root()):
        for file in base.glob("*/plugin.toml"):
            payload = tomllib.loads(file.read_text())
            if payload.get("plugin_id") != name:
                paths.extend(payload.get("uninstall", {}).get("data_paths", []))
    # Retained data of an uninstalled plugin still has an owner.
    for file in (root / "packages/records").glob("plugin-*.json"):
        record = json.loads(file.read_text())
        if record.get("id") != name and not record.get("data_purged"):
            paths.extend(record.get("data_paths") or [])
    return paths


def _safe_install_target(root, name):
    target = root / "plugins/community" / name
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise PackageError("Plugin installation must not traverse symlinks")
    return target


def uninstall(service, name, *, purge_data=False):
    name = valid_id(name)
    root = service.runtime_root
    from pal.plugins.host import _source_plugins_root
    if ((root / "plugins/_builtin" / name).exists() or (_source_plugins_root() / name / "plugin.toml").exists()
            or (service.activation and name in service.activation.host.first_party_records)):
        raise PackageError("Built-in plugins cannot be uninstalled; use plugin_disable")
    with install_lock(root):
        gate = service.activation.gate() if service.activation else runtime_lease(root)
        with gate:
            target = _safe_install_target(root, name)
            if not target.exists() and (root / "channel/providers" / name).exists():
                raise PackageError("Provider uninstall is not supported; only community plugins can be uninstalled")
            path = service._record_path("plugin", name)
            previous = json.loads(path.read_text()) if path.exists() else {}
            pending = previous.get("status") in _REMOVAL_STATES
            if pending and previous.get("purge_data") and not previous.get("data_purged"):
                purge_data = True
            manifest = target / "plugin.toml"
            payload = tomllib.loads(manifest.read_text()) if manifest.exists() else {}
            if payload and payload.get("plugin_id") != name:
                raise PackageError("Plugin installation identity does not match directory")
            data_paths = previous.get("data_paths") if pending else payload.get("uninstall", {}).get("data_paths")
            if purge_data:
                data_paths = validate_data_paths(root, data_paths, other_paths=_other_data_paths(root, name))
            settings = PluginSettings(root)
            host = service.activation.host if service.activation else None
            installed = settings.installation(name)
            if installed and Path(installed["filesystem_path"]).resolve() != target:
                raise PackageError("Registered plugin path is outside its community installation directory")
            if not target.exists() and not installed and not pending:
                return {"id": name, "status": "uninstalled", "already_uninstalled": True}
            if pending and previous.get("status") == "uninstalled" and (not purge_data or previous.get("data_purged")):
                return previous
            operation_id = previous.get("operation_id") if pending else uuid.uuid4().hex
            if not isinstance(operation_id, str) or len(operation_id) != 32 or any(c not in "0123456789abcdef" for c in operation_id):
                raise PackageError("Invalid uninstall operation identity")
            retired = root / "packages/previous/plugin" / name / ("uninstall-" + operation_id)
            for directory in (retired, *retired.parents):
                if directory == root:
                    break
                if directory.is_symlink():
                    raise PackageError("Uninstall retirement directory traverses a symlink")
            record = {**previous, "id": name, "kind": "plugin", "operation": "uninstall",
                      "operation_id": operation_id, "purge_data": purge_data, "data_paths": data_paths,
                      "retired_path": str(retired), "status": "uninstalling"}
            def save(stage, **updates):
                record.update(stage=stage, updated_at=time.time(), **updates)
                atomic_json(path, record)
            try:
                check_cancelled()
                if installed and (not pending or settings.get("plugin.retained:" + name) is None):
                    settings.set("plugin.retained:" + name, {
                        "enabled": bool(installed["enabled"]), "config": json.loads(installed["config_blob"]),
                    })
                save("detaching", error="")
                if host and (name in host.generations or host._record(name) is not None):
                    result = host.detach(name)
                    record["detach_result"] = result
                    if result["status"] != "ok" or name in host.generations:
                        raise PackageError(f"Plugin cleanup is incomplete: {result}")
                check_cancelled()
                save("retiring")
                if target.exists():
                    retired.parent.mkdir(parents=True, exist_ok=True)
                    if retired.exists():
                        raise PackageError("Both installed and retired directories exist; refusing to overwrite either")
                    os.replace(target, retired)
                save("forgetting")
                if host:
                    host.forget_uninstalled(name)
                settings.forget_installation(name)
                if purge_data:
                    save("purging")
                    # Revalidate after cleanup; plugin shutdown may have changed paths.
                    validate_data_paths(root, data_paths, other_paths=_other_data_paths(root, name))
                    cleanup = [root / p for p in data_paths]
                    cleanup.append(retired.parent)
                    environments = root / "packages/environments/plugin" / name
                    # Environments can be retained by another installed package.
                    referenced = False
                    for other in (root / "packages/records").glob("*.json"):
                        if other == path:
                            continue
                        env = json.loads(other.read_text()).get("environment")
                        if env and Path(env).resolve().is_relative_to(environments.resolve()):
                            referenced = True
                    if not referenced:
                        cleanup.append(environments)
                    else:
                        record["retained_environment"] = str(environments)
                    for item in cleanup:
                        check_cancelled()
                        for ancestor in (item, *item.parents):
                            if ancestor == root:
                                break
                            if ancestor.is_symlink():
                                raise PackageError("Cleanup path traverses a symlink")
                        record["pending_cleanup"] = str(item)
                        save("purging")
                        if item.is_dir():
                            shutil.rmtree(item)
                        elif item.exists():
                            item.unlink()
                    settings.delete("plugin.retained:" + name)
                    record["data_purged"] = True
                record.pop("pending_cleanup", None)
                save("complete", status="uninstalled", data_retained=not purge_data)
                return record
            except Exception as exc:
                save(record.get("stage", "detaching"), status="cleanup_failed", error=error_text(exc))
                raise
