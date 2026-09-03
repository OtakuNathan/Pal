from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

from pal.core import PalCore
from pal.plugins import PluginHost
from pal.plugins.lifecycle import WriterPreferredRWGate
from pal.execution.runtime import _is_plugin_lifecycle_tool
from pal.shared import RuntimeStatus


def test_plugin_lifecycle_gate_recognizes_aliases_and_canonical_paths() -> None:
    for action in ("attach", "detach", "enable", "disable", "rescan", "rescan_and_attach_new_first_party"):
        assert _is_plugin_lifecycle_tool(f"plugin_{action}")
        assert _is_plugin_lifecycle_tool(f"op_plugin_mgmt_{action}")
    assert not _is_plugin_lifecycle_tool("mcp_attach")


def _write_plugin(
    root: Path,
    plugin_id: str,
    *,
    requires: tuple[str, ...] = (),
    enabled: bool = True,
) -> None:
    plugin_root = root / plugin_id
    plugin_root.mkdir(parents=True)
    dependencies = ", ".join(repr(item) for item in requires)
    (plugin_root / "plugin.toml").write_text(
        "\n".join(
            [
                f'plugin_id = "{plugin_id}"',
                f'entrypoint = "{plugin_id}_runtime"',
                'version = "1.0.0"',
                f"enabled_by_default = {'true' if enabled else 'false'}",
                'lifecycle_protocol = "raii.v1"',
                f'module_id = "{plugin_id}"',
                f"requires_plugins = [{dependencies}]",
                "requires_ports = []",
            ]
        ),
        encoding="utf-8",
    )
    (root / f"{plugin_id}_runtime.py").write_text(
        "\n".join(
            [
                "from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle",
                "",
                "class Provider:",
                "    def attach(self, *_args): raise AssertionError('host guessed provider attach')",
                "    def detach(self, *_args): raise AssertionError('host guessed provider detach')",
                "",
                "class Plugin:",
                f"    plugin_id = {plugin_id!r}",
                "    version = '1.0.0'",
                "    def __init__(self, ledger): self.ledger = ledger",
                "    def start(self, scope):",
                "        self.ledger.append((self.plugin_id, 'start', scope.core_context.module_registry.get(self.plugin_id)))",
                "        handle = ModuleHandle(module_id=self.plugin_id, tier=MODULE_TIER_DETACHABLE, detachable=True, introspection_provider=Provider())",
                "        scope.context.register_module(handle)",
                "        scope.defer(lambda: self.ledger.append((self.plugin_id, 'cleanup', None)))",
                "        return handle",
                "",
                "def build_plugin(ledger): return Plugin(ledger)",
            ]
        ),
        encoding="utf-8",
    )


def test_raii_dependency_cascade_uses_fresh_generations(tmp_path: Path) -> None:
    builtin_root = tmp_path / "builtin"
    _write_plugin(builtin_root, "base")
    _write_plugin(builtin_root, "dependent", requires=("base",))
    ledger: list[tuple[str, str, object]] = []
    core = PalCore()
    host = PluginHost(
        context=core.context,
        runtime_root=tmp_path,
        builtin_root=builtin_root,
        services={"ledger": ledger},
    )
    sys.path.insert(0, str(builtin_root))
    try:
        host.rescan()
        assert host.enable("dependent")["status"] == RuntimeStatus.OK
        old_base = host.generations["base"].instance
        old_dependent = host.generations["dependent"].instance
        assert all(item[2] is None for item in ledger if item[1] == "start")

        assert host.detach("base")["status"] == RuntimeStatus.OK
        assert "base" not in host.generations
        assert "dependent" not in host.generations
        assert [item[:2] for item in ledger if item[1] == "cleanup"][-2:] == [
            ("dependent", "cleanup"),
            ("base", "cleanup"),
        ]

        assert host.attach("base")["status"] == RuntimeStatus.OK
        assert host.generations["base"].instance is not old_base
        assert host.generations["dependent"].instance is not old_dependent
    finally:
        host.shutdown()
        sys.path.remove(str(builtin_root))


def test_reload_cascades_dependents_but_does_not_restore_manual_detach(tmp_path: Path) -> None:
    builtin_root = tmp_path / "builtin"
    _write_plugin(builtin_root, "base")
    _write_plugin(builtin_root, "dependent", requires=("base",))
    ledger: list[tuple[str, str, object]] = []
    core = PalCore()
    host = PluginHost(
        context=core.context,
        runtime_root=tmp_path,
        builtin_root=builtin_root,
        services={"ledger": ledger},
    )
    sys.path.insert(0, str(builtin_root))
    try:
        host.bootstrap()
        old_base = host.generations["base"].instance
        old_dependent = host.generations["dependent"].instance

        assert host.attach("base")["status"] == RuntimeStatus.OK
        assert host.generations["base"].instance is not old_base
        assert host.generations["dependent"].instance is not old_dependent
        assert [item[:2] for item in ledger if item[1] == "cleanup"][-2:] == [
            ("dependent", "cleanup"),
            ("base", "cleanup"),
        ]

        assert host.detach("dependent")["status"] == RuntimeStatus.OK
        assert host.attach("base")["status"] == RuntimeStatus.OK
        assert "dependent" not in host.generations
    finally:
        host.shutdown()
        sys.path.remove(str(builtin_root))


def test_bootstrap_never_publishes_plugin_with_disabled_dependency(tmp_path: Path) -> None:
    builtin_root = tmp_path / "builtin"
    _write_plugin(builtin_root, "base", enabled=False)
    _write_plugin(builtin_root, "dependent", requires=("base",))
    core = PalCore()
    host = PluginHost(
        context=core.context,
        runtime_root=tmp_path,
        builtin_root=builtin_root,
        services={"ledger": []},
    )
    sys.path.insert(0, str(builtin_root))
    try:
        host.bootstrap()

        assert "base" not in host.generations
        assert "dependent" not in host.generations
        record = host.first_party_records["dependent"]
        assert record.last_load_status == "load_failed"
        assert record.last_error == "dependency disabled or missing: base"
    finally:
        host.shutdown()
        sys.path.remove(str(builtin_root))


def test_third_party_id_conflict_cannot_replace_first_party_manifest(tmp_path: Path) -> None:
    builtin_root = tmp_path / "builtin"
    _write_plugin(builtin_root, "base")
    community_root = tmp_path / "plugins" / "community"
    _write_plugin(community_root, "base", requires=("shadow",))
    core = PalCore()

    class Repository:
        def __init__(self) -> None:
            self.rows = {}

        def upsert_discovered(self, **values):
            row = self.rows.get(values["plugin_id"])
            if row is None:
                from types import SimpleNamespace

                row = SimpleNamespace(
                    **values,
                    enabled=values["enabled_by_default"],
                    attached=False,
                    config_blob={},
                    last_load_status="discovered",
                    last_error=None,
                )
                self.rows[values["plugin_id"]] = row
            return row

        def get(self, plugin_id):
            return self.rows.get(plugin_id)

        def set_load_status(self, plugin_id, *, status, error_text=None):
            row = self.rows[plugin_id]
            row.last_load_status = status
            row.last_error = error_text
            return row

    repository = Repository()
    host = PluginHost(
        context=core.context,
        runtime_root=tmp_path,
        builtin_root=builtin_root,
        services={"ledger": []},
        third_party_repository=repository,  # type: ignore[arg-type]
    )

    host.rescan()

    assert host.manifests["base"].filesystem_path == str(builtin_root / "base")
    assert host.manifests["base"].requires_plugins == ()
    conflict = host.third_party_repository.get("base")
    assert conflict is not None
    assert conflict.last_load_status == "load_failed"
    assert conflict.last_error == "plugin_id conflicts with first-party plugin"


def test_writer_preferred_gate_blocks_new_readers_behind_lifecycle_write() -> None:
    gate = WriterPreferredRWGate()
    first_reader_entered = threading.Event()
    release_first_reader = threading.Event()
    order: list[str] = []

    def first_reader() -> None:
        with gate.read():
            order.append("reader-1")
            first_reader_entered.set()
            release_first_reader.wait(2)

    def writer() -> None:
        with gate.write():
            order.append("writer")
            time.sleep(0.02)

    def second_reader() -> None:
        with gate.read():
            order.append("reader-2")

    reader_thread = threading.Thread(target=first_reader)
    writer_thread = threading.Thread(target=writer)
    second_reader_thread = threading.Thread(target=second_reader)
    reader_thread.start()
    assert first_reader_entered.wait(1)
    writer_thread.start()
    time.sleep(0.02)
    second_reader_thread.start()
    release_first_reader.set()
    for thread in (reader_thread, writer_thread, second_reader_thread):
        thread.join(2)
        assert not thread.is_alive()
    assert order == ["reader-1", "writer", "reader-2"]


def test_cancelled_async_waiter_balances_late_gate_admission() -> None:
    gate = WriterPreferredRWGate()

    async def scenario() -> None:
        async def waiting_writer() -> None:
            async with gate.write_async():
                raise AssertionError("cancelled writer entered its body")

        with gate.read():
            task = asyncio.create_task(waiting_writer())
            await asyncio.sleep(0.02)
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:  # pragma: no cover - cancellation is the contract under test
            raise AssertionError("writer cancellation was swallowed")

        # A leaked late acquisition would deadlock this read.
        async with gate.read_async():
            return

    asyncio.run(asyncio.wait_for(scenario(), timeout=2.0))
