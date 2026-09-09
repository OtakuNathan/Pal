from __future__ import annotations

import tomllib

from pal.packages.process import PackageError
from pal.shared import RuntimeStatus


class RuntimeActivation:
    """Keep installation outside the lifecycle fence; switch through existing owners."""

    def __init__(self, host):
        self.host = host

    def gate(self):
        return self.host.context.execution_runtime.lifecycle_gate.write()

    def _channel(self):
        return self.host.context.require_port("channel:provider_manager")

    def before(self, kind: str, name: str) -> dict:
        if kind == "plugin":
            record = self.host._record(name)
            state = {"existed": record is not None, "attached": bool(record and record.attached),
                     "enabled": bool(record and record.enabled)}
            if state["attached"] and self.host.detach(name)["status"] != RuntimeStatus.OK:
                raise PackageError("Previous plugin could not be detached; installation was not switched")
            return state
        manager = self._channel()
        state = {"existed": name in manager.discovered_runtime_providers,
                 "endpoints": [endpoint_id for endpoint_id in manager._provider_hub_ids(name)
                               if manager.runtime.get_endpoint(endpoint_id) is not None]}
        if state["existed"]:
            _, errors = manager._stop_provider_transports(name, reason="package_install")
            if errors:
                raise PackageError(f"Previous provider could not be stopped: {errors}")
            errors = manager._unload_runtime_provider(name)
            if errors:
                raise PackageError(f"Previous provider could not be unloaded: {errors}")
        return state

    def after(self, kind: str, name: str, state: dict) -> str:
        if kind == "plugin":
            scan = self.host.rescan()
            relevant_errors = [error for error in scan.get("scan_errors", [])
                               if f"/{name}/plugin.toml" in error]
            if relevant_errors:
                raise PackageError(f"Plugin discovery failed: {relevant_errors}")
            record = self.host._record(name)
            if record is None:
                raise PackageError("Installed plugin was not discovered")
            should_attach = state["attached"] if state["existed"] else record.enabled
            if should_attach and self.host.attach(name)["status"] != RuntimeStatus.OK:
                raise PackageError(f"Plugin attach failed: {self.host._record(name).last_error}")
            return "attached" if should_attach else "installed_inactive"
        manager = self._channel()
        manifest_path = self.host.runtime_root / "channel/providers" / name / "provider.toml"
        if not tomllib.loads(manifest_path.read_text()).get("enabled", True):
            result = manager.rescan_providers()
            self._check_provider_scan(manager, name, result)
            return "installed_inactive"
        if name in manager.discovered_runtime_providers:
            result = manager.reload_provider(name)
            if result.status != RuntimeStatus.OK:
                raise PackageError(result.text)
        else:
            result = manager.rescan_providers()
            self._check_provider_scan(manager, name, result)
        if name not in manager.discovered_runtime_providers:
            raise PackageError("Installed provider was not discovered")
        for endpoint_id in state["endpoints"]:
            result = manager.attach_endpoint(endpoint_id)
            if result.status != RuntimeStatus.OK:
                raise PackageError(result.text)
        return "provider_registered"

    def restore(self, kind: str, name: str, state: dict) -> None:
        if state["existed"]:
            self.after(kind, name, state)
        elif kind == "plugin":
            self.host.rescan()
        else:
            manager = self._channel()
            self._check_provider_scan(manager, name, manager.rescan_providers())

    @staticmethod
    def _check_provider_scan(manager, name: str, result: dict) -> None:
        # The owner returns scan_errors, not errors. Ignore failures belonging
        # to unrelated providers, but include this provider's endpoint failures.
        prefixes = (f"{name}:", *(f"{endpoint}:" for endpoint in manager._provider_hub_ids(name)))
        errors = [error for error in result.get("scan_errors", [])
                  if f"/{name}/" in error or f"/{name}:" in error or error.startswith(prefixes)]
        if errors:
            raise PackageError(f"Provider discovery failed: {errors}")
