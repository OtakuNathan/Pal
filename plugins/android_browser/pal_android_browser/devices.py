"""Route explicit device ids to phone origins registered by companion pairing."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from .client import AndroidError, AndroidHostClient


class AndroidDeviceRouter:
    def __init__(self, runtime_root: Path, *, fallback: AndroidHostClient | None = None):
        self.registry_path = runtime_root / "data/android_companion/devices.json"
        self.fallback = fallback

    def _clients(self):
        if not self.registry_path.exists():
            return {}
        try:
            registry = json.loads(self.registry_path.read_text())
            if (registry.keys() != {"v", "devices"} or type(registry["v"]) is not int or registry["v"] != 1
                    or not isinstance(registry["devices"], list)):
                raise ValueError("unsupported registry")
            clients = {}
            for value in registry["devices"]:
                if set(value) != {"device_id", "name", "node_id", "dns_name", "rpc_token"}:
                    raise ValueError("invalid device record")
                identifier = str(UUID(value["device_id"]))
                host = value["dns_name"]
                if identifier in clients or not isinstance(host, str) or not host.endswith(".ts.net"):
                    raise ValueError("invalid or duplicate device identity")
                client = AndroidHostClient(url=f"http://{host}:8780", token=value["rpc_token"], protocol="pal.android.v1")
                clients[identifier] = (value["name"], client)
            return clients
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise AndroidError("Invalid paired Android device registry", details={"effect": "none", "receipt_protocol": "pal.android.v1"}) from exc

    def request(self, command: str, args: dict | None = None, *, device: str = "_") -> dict:
        clients = self._clients()
        if command == "devices":
            devices = {}
            if self.fallback is not None:
                try:
                    probe = AndroidHostClient(self.fallback.url, token=self.fallback.token, protocol=self.fallback.protocol, timeout=5)
                    devices.update((value["id"], value) for value in probe.request("devices")["value"])
                except AndroidError:
                    # An explicitly configured legacy backend remains addressable.
                    devices["legacy"] = {"id": "legacy", "name": "Configured phone", "online": False}
            for identifier, (name, client) in clients.items():
                try:
                    probe = AndroidHostClient(client.url, token=client.token, protocol=client.protocol, timeout=5)
                    values = probe.request("devices")["value"]
                    if len(values) != 1 or values[0]["id"] != identifier:
                        raise AndroidError("Paired phone identity changed")
                    devices[identifier] = {**values[0], "name": name}
                except AndroidError:
                    devices[identifier] = {"id": identifier, "name": name, "online": False}
            return {"ok": True, "value": list(devices.values())}
        if device in clients:
            return clients[device][1].request(command, args, device=device)
        if device == "_":
            if len(clients) == 1 and self.fallback is None:
                identifier = next(iter(clients))
                return clients[identifier][1].request(command, args, device=identifier)
            if not clients and self.fallback is not None:
                return self.fallback.request(command, args, device=device)
            raise AndroidError("Choose an explicit device id from android_devices before operating multiple phones",
                               details={"receipt_protocol": "pal.android.v1", "effect": "none"})
        # Native fallback verifies descriptor identity before any effect. Never
        # send a misspelled new id to the old Droidline default-device route.
        if self.fallback is not None and (self.fallback.protocol == "pal.android.v1" or device == "legacy"):
            return self.fallback.request(command, args, device="_" if device == "legacy" else device)
        raise AndroidError("Unknown paired Android device id", details={"receipt_protocol": "pal.android.v1", "effect": "none"})
