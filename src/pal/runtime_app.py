from __future__ import annotations

import asyncio
import contextlib
from copy import deepcopy
import shutil
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pal.bootstrap import StubRuntimeHandle, compose_runtime
from pal.channel import ChannelEndpointRepository
from pal.core.debug_dump import write_runtime_debug_dump
from pal.core.resident_checkpoint import (
    RESIDENT_LOGICAL_COROUTINE_ID,
    ResidentCheckpointError,
    ResidentCheckpointStore,
)
from pal.core.runtime_state import (
    RuntimeSnapshotCoordinator,
    RuntimeSnapshotIdentity,
    runtime_spec_hash,
)
from pal.foundation.log_paths import pal_debug_log_path
from pal.wizard.runtime import DEFAULT_DB_FILENAME, DEFAULT_PAL_ENTRYPOINT, ensure_recovery_socket_channel
from pal.wizard import PalRegistration, WizardService


def _ensure_plugin_layout(runtime_root: Path, wizard: WizardService, registration: PalRegistration) -> None:
    builtin_dir = runtime_root / "plugins" / "_builtin"
    community_dir = runtime_root / "plugins" / "community"
    old_plugins = runtime_root / "plugins"

    # Upgrade: if flat plugins/ exists without _builtin/community subdirs
    if old_plugins.exists() and not builtin_dir.exists():
        community_dir.mkdir(parents=True, exist_ok=True)
        for item in list(old_plugins.iterdir()):
            if item.is_dir() and item.name not in ("_builtin", "community"):
                target = community_dir / item.name
                if not target.exists():
                    shutil.move(str(item), str(target))

    # Always sync source builtin manifests into runtime_root/plugins/_builtin.
    # This makes newly added first-party plugins appear in existing runtimes
    # without treating plugin-owned config directories as plugin bundles.
    wizard.provision_builtin_plugins(registration)


def open_runtime(runtime_root: Path) -> StubRuntimeHandle:
    wizard = WizardService()
    db_path = runtime_root / DEFAULT_DB_FILENAME
    if db_path.exists():
        registration = wizard.provision_runtime(
            display_name="PalV2",
            runtime_root=runtime_root,
            db_filename=DEFAULT_DB_FILENAME,
            pal_entrypoint=DEFAULT_PAL_ENTRYPOINT,
        )
        database = wizard.create_database(registration)
        _ensure_plugin_layout(runtime_root, wizard, registration)
        channel_repository = ChannelEndpointRepository()
        had_channel_endpoints = bool(channel_repository.list_all())
        ensure_recovery_socket_channel(channel_repository, runtime_root)
        if not had_channel_endpoints:
            wizard.seed_defaults(registration)
    else:
        provisioned = wizard.provision_stub_runtime(runtime_root)
        registration = provisioned.registration
        database = provisioned.database
    return compose_runtime(
        wizard=wizard,
        registration=registration,
        database=database,
    )


@dataclass
class PalRuntimeApp:
    handle: StubRuntimeHandle
    loop_iterations: int = 0
    last_tick_monotonic: float = 0.0
    last_tick_utc: str = ""
    last_processed_count: int = 0
    last_debug_dump_utc: str = ""
    last_debug_dump_error: str = ""
    last_checkpoint_status: str = ""
    last_checkpoint_error: str = ""
    _checkpoint_sequence: int = field(default=0, init=False, repr=False)
    _shutdown_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    async def run(self) -> None:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        debug_signal = getattr(signal, "SIGUSR1", None)

        def request_stop() -> None:
            stop_event.set()
            if self._shutdown_task is None:
                self._shutdown_task = asyncio.create_task(
                    self._checkpoint_for_shutdown_async(),
                    name="pal.shutdown_checkpoint",
                )

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, request_stop)
            except NotImplementedError:
                pass
        if debug_signal is not None:
            try:
                loop.add_signal_handler(debug_signal, self._schedule_debug_dump)
            except (NotImplementedError, RuntimeError, ValueError):
                debug_signal = None
        await self._restore_checkpoint_async()
        await self.handle.channel_runtime.start_async()
        self.handle.core.bind_async_wakeup_sources()
        publish_catalog = getattr(self.handle.core, "publish_control_catalog_async", None)
        if callable(publish_catalog):
            await publish_catalog()
        try:
            while not stop_event.is_set():
                self._mark_loop_tick()
                processed = await self.handle.core.run_until_idle_async(max_iterations=128)
                self.last_processed_count = len(processed)
                if not processed:
                    await self._wait_for_runtime_wakeup(stop_event)
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(RuntimeError, ValueError):
                    loop.remove_signal_handler(sig)
            if debug_signal is not None:
                try:
                    loop.remove_signal_handler(debug_signal)
                except (RuntimeError, ValueError):
                    pass
            if self._shutdown_task is None:
                self._shutdown_task = asyncio.create_task(
                    self._checkpoint_for_shutdown_async(),
                    name="pal.shutdown_checkpoint",
                )
            with contextlib.suppress(Exception):
                await self._shutdown_task
            await self.handle.stop_async()

    async def _restore_checkpoint_async(self) -> None:
        store = ResidentCheckpointStore(self._runtime_root())
        try:
            snapshot = store.read()
            if snapshot is None:
                self.last_checkpoint_status = "none"
                return
            expected_spec = self._runtime_spec_hash()
            if str(snapshot.get("runtime_spec_hash") or "") != expected_spec:
                raise ResidentCheckpointError(
                    "resident checkpoint runtime spec does not match this runtime"
                )
            prepared = _interrupt_active_resident_turns(snapshot)
            await RuntimeSnapshotCoordinator(
                self.handle.core.context.module_registry
            ).restore(prepared)
            self._checkpoint_sequence = int(prepared.get("sequence") or 0)
            store.consume()
            self.last_checkpoint_status = "restored"
            self.last_checkpoint_error = ""
        except Exception as exc:
            self.last_checkpoint_status = "restore_failed"
            self.last_checkpoint_error = f"{type(exc).__name__}: {exc}"
            self.handle.core.state.diagnostics.append(
                {
                    "kind": "resident.checkpoint.restore_failed",
                    "error": self.last_checkpoint_error,
                }
            )

    async def _checkpoint_for_shutdown_async(self) -> None:
        core = self.handle.core
        core.state.resident_quiescing = True
        try:
            await core.turn_manager.interrupt_active_turn(
                reason="process_shutdown"
            )
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    core.state.resident_drained_event.wait(),
                    timeout=1.0,
                )
            # Shutdown continuity must not depend on an external model.  Save
            # the fixed L1 exactly once; the next process restores it before
            # admitting channel traffic and normal budget policy can compact
            # it later if that is actually required.
            await self._publish_checkpoint_async()
            self.last_checkpoint_status = "l1_saved"
            self.last_checkpoint_error = ""
        except Exception as exc:
            self.last_checkpoint_status = "save_failed"
            self.last_checkpoint_error = f"{type(exc).__name__}: {exc}"
            core.state.diagnostics.append(
                {
                    "kind": "resident.checkpoint.save_failed",
                    "error": self.last_checkpoint_error,
                }
            )

    async def _publish_checkpoint_async(self) -> None:
        self._checkpoint_sequence += 1
        identity = RuntimeSnapshotIdentity(
            logical_coroutine_id=RESIDENT_LOGICAL_COROUTINE_ID,
            workflow_id=RESIDENT_LOGICAL_COROUTINE_ID,
            stage_key="resident_exit",
            sequence=self._checkpoint_sequence,
            producer_fencing_token=1,
            runtime_spec_hash=self._runtime_spec_hash(),
        )
        snapshot = await RuntimeSnapshotCoordinator(
            self.handle.core.context.module_registry
        ).snapshot(identity)
        ResidentCheckpointStore(self._runtime_root()).publish(snapshot)

    def _runtime_spec_hash(self) -> str:
        return runtime_spec_hash(
            self.handle.core.context.module_registry,
            identity_parts={"host": RESIDENT_LOGICAL_COROUTINE_ID},
        )

    async def _wait_for_runtime_wakeup(self, stop_event: asyncio.Event) -> None:
        timeout = self.handle.core.next_wakeup_timeout_seconds()
        wake_task = asyncio.create_task(self.handle.core.wait_for_ready_async(timeout=timeout))
        stop_task = asyncio.create_task(stop_event.wait())
        try:
            done, pending = await asyncio.wait({wake_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            _ = done
            for task in pending:
                task.cancel()
        finally:
            for task in (wake_task, stop_task):
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    def _mark_loop_tick(self) -> None:
        self.loop_iterations += 1
        self.last_tick_monotonic = time.monotonic()
        self.last_tick_utc = datetime.now(timezone.utc).isoformat()

    def _schedule_debug_dump(self) -> None:
        task = asyncio.create_task(self._write_debug_dump_async(), name="pal.debug_dump")
        task.add_done_callback(self._record_debug_dump_failure)

    async def _write_debug_dump_async(self) -> None:
        write_runtime_debug_dump(
            self.handle,
            app_snapshot=self._debug_snapshot(),
            path=pal_debug_log_path(self._runtime_root()),
        )
        self.last_debug_dump_utc = datetime.now(timezone.utc).isoformat()
        self.last_debug_dump_error = ""

    def _record_debug_dump_failure(self, task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except Exception as exc:
            self.last_debug_dump_error = f"{type(exc).__name__}: {exc}"

    def _debug_snapshot(self) -> dict[str, object]:
        return {
            "loop_iterations": self.loop_iterations,
            "last_tick_monotonic": self.last_tick_monotonic,
            "last_tick_utc": self.last_tick_utc,
            "last_processed_count": self.last_processed_count,
            "last_debug_dump_utc": self.last_debug_dump_utc,
            "last_debug_dump_error": self.last_debug_dump_error,
            "last_checkpoint_status": self.last_checkpoint_status,
            "last_checkpoint_error": self.last_checkpoint_error,
        }

    def _runtime_root(self) -> Path:
        return Path(self.handle.registration.runtime.runtime_root)


def build_runtime_app(runtime_root: Path) -> PalRuntimeApp:
    return PalRuntimeApp(handle=open_runtime(runtime_root))


def _interrupt_active_resident_turns(snapshot: dict[str, object]) -> dict[str, object]:
    """Resident checkpoints restore context, never an in-flight continuation."""

    prepared = deepcopy(snapshot)
    modules = prepared.get("modules")
    if not isinstance(modules, dict):
        return prepared
    memory = modules.get("memory")
    if not isinstance(memory, dict):
        return prepared
    payload = memory.get("payload")
    if not isinstance(payload, dict):
        return prepared
    turns = payload.get("l1_turns")
    if not isinstance(turns, list):
        return prepared
    for raw_turn in turns:
        if not isinstance(raw_turn, dict) or raw_turn.get("state") != "active":
            continue
        raw_turn["state"] = "interrupted"
        metadata = raw_turn.get("metadata")
        normalized_metadata = dict(metadata) if isinstance(metadata, dict) else {}
        normalized_metadata["interrupt_reason"] = "resident process restart"
        raw_turn["metadata"] = normalized_metadata
    return prepared
