from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from pal.control import ControlAction
from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.foundation.sidecar import pack_sidecar_message, read_sidecar_message
from pal.minion.ipc import (
    MinionManagerClient,
    minion_port_path,
    open_manager_connection,
    python_subprocess_env,
)
from pal.minion.source import MinionControlEventHandler, MinionEventSource
from pal.minion.v2.capabilities import MinionV2PublicProvider
from pal.minion.v2.service import MinionV2WorkflowService
from pal.shared import EventKind

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


_MANAGER_GRACEFUL_DRAIN_TIMEOUT_SECONDS = 600.0


@dataclass(frozen=True)
class MinionSnapshot:
    mounted: bool = True
    degraded: bool = False
    manager_running: bool = False
    active_count: int = 0
    run_count: int = 0
    pending_event_count: int = 0


@dataclass
class MinionManagerProvider:
    runtime_root: Path
    context: MainContext | None = None
    process: subprocess.Popen[bytes] | None = None
    last_health: dict[str, Any] = field(default_factory=dict)
    last_error: str = ""
    event_notify: Callable[[], None] | None = None
    _buffered_events: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _seen_event_keys: set[str] = field(default_factory=set, init=False, repr=False)
    _buffer_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _event_thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _event_stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _lifecycle_lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _attached: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.runtime_root = Path(self.runtime_root)
        self.client = MinionManagerClient(self.runtime_root)
        self._lifecycle_client = MinionManagerClient(self.runtime_root, request_timeout_seconds=5.0)

    def attach_manager(self) -> dict[str, Any]:
        with self._lifecycle_lock:
            if self._attached:
                return self._require_manager()
            try:
                health = self._start_manager()
                self._attached = True
                self.last_health = health
                self.last_error = ""
                self._start_event_subscription()
                self.client.request_sync("v2_wake")
                return health
            except Exception as exc:
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                self._attached = False
                self._stop_manager_locked()
                raise

    def detach_manager(self) -> None:
        with self._lifecycle_lock:
            if not self._attached and self.process is None and self._event_thread is None:
                return
            self._attached = False
            try:
                self._stop_manager_locked()
            except Exception:
                self._attached = self._manager_is_responding()
                raise

    def wake_v2(self) -> None:
        self._require_manager()
        self.client.request_sync("v2_wake")

    def has_pending_events(self) -> bool:
        with self._buffer_lock:
            return bool(self._buffered_events)

    def drain_events_sync(self, *, limit: int = 20) -> dict[str, Any]:
        with self._buffer_lock:
            events = self._buffered_events[: max(1, int(limit))]
            del self._buffered_events[: len(events)]
            return {"events": events, "remaining": len(self._buffered_events)}

    async def handle_control_action_async(self, action: ControlAction) -> str:
        if action.action_kind == "minion_v2_human_decision":
            decision = str(action.args.get("decision") or "").strip().lower()
            if decision == "edit" and not str(action.args.get("edit_instruction") or "").strip():
                return "Reply with the exact architecture edit instruction, then submit it with minion_submit_human_decision."
            if decision == "clarify" and not str(action.args.get("clarification_response") or "").strip():
                return "Reply with the requested clarification, then submit it with minion_submit_human_decision."
            try:
                result = await asyncio.to_thread(
                    MinionV2WorkflowService(self.runtime_root).submit_human_decision,
                    {
                        "decision_token": str(action.args.get("decision_token") or ""),
                        "decision": decision,
                        "edit_instruction": str(action.args.get("edit_instruction") or ""),
                        "clarification_response": str(action.args.get("clarification_response") or ""),
                        "actor": str(action.args.get("actor_id") or "pal"),
                        "source_channel": str(action.args.get("active_channel_id") or "local"),
                    },
                )
                await asyncio.to_thread(self.wake_v2)
                return f"Minion architecture decision recorded ({result.get('state') or decision})."
            except Exception as exc:
                return f"Minion architecture decision was not applied: {exc}"
        if action.action_kind == "minion_approval_decision":
            payload = {
                "approval_id": str(action.args.get("approval_id") or action.target_id or ""),
                "decision": str(action.args.get("decision") or ""),
                "run_id": str(action.args.get("run_id") or ""),
                "minion_id": str(action.args.get("minion_id") or ""),
            }
            try:
                await asyncio.to_thread(self._require_manager)
                await asyncio.to_thread(self.client.send_decision_sync, payload)
                return "Minion approval decision recorded."
            except Exception as exc:
                return f"Minion approval decision failed: {exc}"
        if action.action_kind == "minion_question_answer":
            try:
                await asyncio.to_thread(self._require_manager)
                await asyncio.to_thread(self.client.send_clarification_sync, dict(action.args))
                return "Minion clarification recorded."
            except Exception as exc:
                return f"Minion clarification failed: {exc}"
        return ""

    def _start_manager(self) -> dict[str, Any]:
        if self.process is not None and self.process.poll() is None:
            try:
                owned_health = self._lifecycle_client.health_sync()
            except Exception:
                self._stop_process_only(kill_process_group=True)
            else:
                try:
                    health = self._validate_health(owned_health)
                    if self._pid_from_health(health) != self.process.pid:
                        raise RuntimeError("minion manager endpoint is not owned by this plugin attachment")
                    return health
                except Exception:
                    self._stop_process_only(kill_process_group=True)
                    self._retire_existing_manager(owned_health)
        elif self.process is not None:
            self._stop_process_only()
        try:
            existing_health = self._lifecycle_client.health_sync()
        except Exception:
            existing_health = None
        if existing_health is not None:
            # A manager not represented by self.process is not owned by this
            # plugin attachment. Retire it instead of silently adopting it.
            self._retire_existing_manager(existing_health)
        self._cleanup_stale_endpoint()
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "pal.minion.manager_main", "--runtime-root", str(self.runtime_root)],
            env=python_subprocess_env(),
            start_new_session=True,
        )
        for _ in range(150):
            if self.process.poll() is not None:
                self._stop_process_only()
                raise RuntimeError("minion manager exited during startup")
            try:
                health = self._validate_health(self._lifecycle_client.health_sync())
                if self._pid_from_health(health) != self.process.pid:
                    raise RuntimeError("minion manager endpoint is not owned by this plugin attachment")
                return health
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("minion manager failed to start")

    def _stop_manager(self, *, force: bool = False) -> None:
        _ = force
        self.detach_manager()

    def _stop_manager_locked(self) -> None:
        self._event_stop.set()
        manager_pid = self._manager_pid()
        graceful_requested = self._request_graceful_shutdown()
        process = self.process
        if process is not None and graceful_requested:
            with contextlib.suppress(Exception):
                process.wait(timeout=_MANAGER_GRACEFUL_DRAIN_TIMEOUT_SECONDS + 1.0)
        if graceful_requested:
            self._wait_for_pid_exit(
                manager_pid,
                timeout_seconds=_MANAGER_GRACEFUL_DRAIN_TIMEOUT_SECONDS + 1.0,
            )
        if self._pid_is_running(manager_pid):
            self._terminate_manager(manager_pid)
        self._stop_process_only(kill_process_group=True)
        self._wait_for_pid_exit(manager_pid, timeout_seconds=1.5)
        thread = self._event_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._event_thread = None
        if self._pid_is_running(manager_pid):
            raise RuntimeError("minion manager process did not stop during detach")
        if self._manager_is_responding():
            raise RuntimeError("minion manager did not stop during detach")
        self._cleanup_stale_endpoint()
        self.last_health = {}

    def _stop_process_only(self, *, kill_process_group: bool = False) -> None:
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            with contextlib.suppress(Exception):
                if kill_process_group:
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
                process.wait(timeout=1.0)
        if process.poll() is None:
            with contextlib.suppress(Exception):
                if kill_process_group:
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait(timeout=1.0)
        self.process = None

    def _require_manager(self) -> dict[str, Any]:
        with self._lifecycle_lock:
            if not self._attached:
                raise RuntimeError("minion sidecar is detached")
            health = self._validate_health(self.client.health_sync())
            self.last_health = health
            return health

    @staticmethod
    def _validate_health(health: dict[str, Any]) -> dict[str, Any]:
        if not bool(health.get("ok")) or str(health.get("health_source") or "") != "minion_v2_manager":
            raise RuntimeError("minion manager health check failed")
        if str(health.get("lifecycle_protocol") or "") != "plugin_raii.v1":
            raise RuntimeError("minion manager lifecycle protocol is incompatible")
        if bool(health.get("shutdown_requested")):
            raise RuntimeError("minion manager is shutting down")
        return dict(health)

    def _retire_existing_manager(self, health: dict[str, Any] | None = None) -> None:
        payload = dict(health or {})
        manager_pid = self._pid_from_health(payload)
        graceful_requested = self._request_graceful_shutdown()
        if graceful_requested:
            self._wait_for_pid_exit(
                manager_pid,
                timeout_seconds=_MANAGER_GRACEFUL_DRAIN_TIMEOUT_SECONDS + 1.0,
            )
        if self._pid_is_running(manager_pid):
            self._terminate_manager(manager_pid)
            self._wait_for_pid_exit(manager_pid, timeout_seconds=1.5)
        self._wait_for_manager_exit(timeout_seconds=0.5)
        if self._pid_is_running(manager_pid):
            raise RuntimeError("existing minion manager process did not stop")
        if self._manager_is_responding():
            raise RuntimeError("existing minion manager did not stop")
        self._cleanup_stale_endpoint()

    def _request_graceful_shutdown(self) -> bool:
        try:
            result = self._lifecycle_client.shutdown_sync(
                graceful=True,
                timeout_seconds=_MANAGER_GRACEFUL_DRAIN_TIMEOUT_SECONDS,
            )
        except Exception:
            return False
        return bool(result.get("ok"))

    def _manager_pid(self) -> int | None:
        health = dict(self.last_health or {})
        if not health:
            with contextlib.suppress(Exception):
                health = self._validate_health(self._lifecycle_client.health_sync())
        return self._pid_from_health(health)

    @staticmethod
    def _pid_from_health(health: dict[str, Any]) -> int | None:
        try:
            pid = int(health.get("manager_pid") or 0)
        except (TypeError, ValueError):
            return None
        return pid if pid > 1 and pid != os.getpid() else None

    def _manager_is_responding(self) -> bool:
        try:
            health = self._lifecycle_client.health_sync()
            return bool(health.get("ok")) and str(health.get("health_source") or "") == "minion_v2_manager"
        except Exception:
            return False

    def _wait_for_manager_exit(self, *, timeout_seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while self._manager_is_responding() and time.monotonic() < deadline:
            time.sleep(0.05)

    @classmethod
    def _wait_for_pid_exit(cls, manager_pid: int | None, *, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while cls._pid_is_running(manager_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        return not cls._pid_is_running(manager_pid)

    @staticmethod
    def _pid_is_running(manager_pid: int | None) -> bool:
        if manager_pid is None:
            return False
        proc_stat = Path(f"/proc/{manager_pid}/stat")
        if proc_stat.exists():
            with contextlib.suppress(OSError, IndexError):
                if proc_stat.read_text(encoding="utf-8").split()[2] == "Z":
                    return False
        try:
            os.kill(manager_pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @classmethod
    def _terminate_manager(cls, manager_pid: int | None) -> None:
        if manager_pid is None:
            return
        process_group: int | None = None
        with contextlib.suppress(ProcessLookupError, PermissionError):
            candidate = os.getpgid(manager_pid)
            if candidate != os.getpgrp():
                process_group = candidate
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if process_group is not None:
                os.killpg(process_group, signal.SIGTERM)
            else:
                os.kill(manager_pid, signal.SIGTERM)
        for _ in range(20):
            if not cls._pid_is_running(manager_pid):
                return
            time.sleep(0.05)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if process_group is not None:
                os.killpg(process_group, signal.SIGKILL)
            else:
                os.kill(manager_pid, signal.SIGKILL)
        for _ in range(20):
            if not cls._pid_is_running(manager_pid):
                return
            time.sleep(0.05)

    def _cleanup_stale_endpoint(self) -> None:
        for path in (self.client.socket_path, minion_port_path(self.runtime_root)):
            if path.exists():
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()

    def _start_event_subscription(self) -> None:
        if self._event_thread is not None and self._event_thread.is_alive():
            return
        self._event_stop.clear()
        self._event_thread = threading.Thread(
            target=self._event_subscription_thread,
            name="pal-minion-v2-events",
            daemon=True,
        )
        self._event_thread.start()

    def _event_subscription_thread(self) -> None:
        while not self._event_stop.is_set():
            try:
                asyncio.run(self._event_subscription_loop())
            except Exception as exc:
                if self._event_stop.is_set():
                    return
                self.last_error = f"event subscription failed: {exc.__class__.__name__}: {exc}"
                time.sleep(0.25)

    async def _event_subscription_loop(self) -> None:
        reader, writer = await open_manager_connection(self.runtime_root)
        request_id = f"sub_{uuid4().hex[:16]}"
        try:
            writer.write(
                pack_sidecar_message(
                    {"type": "request", "id": request_id, "method": "subscribe_events", "params": {}}
                )
            )
            await writer.drain()
            response = await read_sidecar_message(reader)
            if str(response.get("id") or "") != request_id or not bool(response.get("ok")):
                raise RuntimeError("minion event subscription rejected")
            while not self._event_stop.is_set():
                frame = await read_sidecar_message(reader)
                event = frame.get("event") if str(frame.get("type") or "") == "event" else None
                if isinstance(event, dict):
                    self._buffer_event(event)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def _buffer_event(self, event: dict[str, Any]) -> None:
        if str(event.get("event_kind") or "") == "progress":
            return
        key = "|".join(
            (
                str(event.get("event_kind") or ""),
                str(event.get("run_id") or ""),
                str(event.get("workflow_id") or event.get("invocation_id") or ""),
                str(dict(event.get("payload") or {}).get("manifest_sha") or ""),
            )
        )
        with self._buffer_lock:
            if key in self._seen_event_keys:
                return
            self._seen_event_keys.add(key)
            self._buffered_events.append(dict(event))
        if self.event_notify is not None:
            self.event_notify()


def register_with_core(
    context: MainContext,
    service: object | None = None,
    *,
    runtime_root: Path | None = None,
) -> ModuleHandle:
    _ = service
    resolved_root = Path(runtime_root or context.execution_runtime.runtime_root or Path.cwd() / ".pal-minion")
    manager = MinionManagerProvider(runtime_root=resolved_root, context=context)
    public = MinionV2PublicProvider(
        runtime_root=resolved_root,
        context=context,
        wake_manager=manager.wake_v2,
        attach_manager=manager.attach_manager,
        detach_manager=manager.detach_manager,
        manager_request=manager.client.request_sync,
    )
    manager.event_notify = lambda: getattr(context.port_registry.get("core:core"), "notify_ready", lambda: None)()
    source = MinionEventSource(provider=manager)
    handler = MinionControlEventHandler(provider=manager)
    event_kinds = (
        EventKind.APPROVAL_REQUEST,
        EventKind.MINION_TERMINAL,
        EventKind.MINION_STANDALONE_REVIEW_COMPLETED,
        EventKind.MINION_CLARIFICATION_REQUEST,
        EventKind.MINION_ARCHITECTURE_REVIEW_PENDING,
    )
    handle = ModuleHandle(
        module_id="minion",
        tier=MODULE_TIER_DETACHABLE,
        detachable=True,
        introspection_provider=public,
        supports_lifecycle_capabilities=True,
        event_sources=[source],
        event_handlers={kind: [handler] for kind in event_kinds},
        control_action_handlers={
            "minion_approval_decision": manager.handle_control_action_async,
            "minion_question_answer": manager.handle_control_action_async,
            "minion_v2_human_decision": manager.handle_control_action_async,
        },
        ports={"minion": manager, "minion_v2": public},
        shutdown_sync=manager._stop_manager,
    )
    context.register_module(handle)
    context.event_source_registry.attach("minion", source)
    for kind in event_kinds:
        context.event_handler_registry.register(kind, handler, module_id="minion")
    return handle


def inspect_minion(provider: MinionManagerProvider) -> MinionSnapshot:
    try:
        health = provider._require_manager()
        return MinionSnapshot(
            mounted=True,
            manager_running=bool(health.get("ok")),
            active_count=int(health.get("active_count") or 0),
            run_count=int(health.get("run_count") or 0),
            pending_event_count=int(health.get("pending_event_count") or 0),
        )
    except Exception:
        return MinionSnapshot(mounted=provider._attached, degraded=True)
