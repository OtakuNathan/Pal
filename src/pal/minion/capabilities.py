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
from pal.minion.harnesses import (
    MinionHarnessRegistry,
    MinionHarnessRegistryGeneration,
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
    harness_registry: MinionHarnessRegistry | None = None
    process: subprocess.Popen[bytes] | None = None
    last_health: dict[str, Any] = field(default_factory=dict)
    last_error: str = ""
    event_notify: Callable[[], None] | None = None
    _buffered_events: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _seen_event_keys: set[str] = field(default_factory=set, init=False, repr=False)
    _inflight_event_keys: set[str] = field(default_factory=set, init=False, repr=False)
    _locally_delivered_parts: dict[str, set[str]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _buffer_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _event_thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _event_stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _lifecycle_lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _attached: bool = field(default=False, init=False, repr=False)
    prompt_log_enabled: bool = field(default=False, init=False)
    _unsubscribe_harness_registry: Callable[[], None] | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self.runtime_root = Path(self.runtime_root)
        self.client = MinionManagerClient(self.runtime_root)
        self._lifecycle_client = MinionManagerClient(self.runtime_root, request_timeout_seconds=5.0)
        if self.harness_registry is not None:
            self._unsubscribe_harness_registry = (
                self.harness_registry.subscribe(
                    self._on_harness_registry_generation
                )
            )

    def attach_manager(self) -> dict[str, Any]:
        with self._lifecycle_lock:
            if self._attached:
                return self._require_manager()
            try:
                health = self._start_manager()
                self._attached = True
                self.last_health = health
                self.last_error = ""
                self._sync_harness_registry()
                core = (
                    self.context.port_registry.get("core:core")
                    if self.context is not None
                    else None
                )
                core_state = getattr(core, "state", None)
                if core_state is not None:
                    self.prompt_log_enabled = bool(
                        getattr(core_state, "prompt_log_enabled", False)
                    )
                log_policy = self.client.request_sync(
                    "set_prompt_log_enabled",
                    {"enabled": bool(self.prompt_log_enabled)},
                )
                health = {
                    **dict(health),
                    "prompt_log_enabled": bool(log_policy.get("enabled")),
                }
                self.last_health = health
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

    async def refresh_llm_endpoints(self) -> dict[str, Any]:
        await asyncio.to_thread(self._require_manager)
        return await self.client.refresh_llm_endpoints()

    async def set_prompt_log_enabled(self, enabled: bool) -> dict[str, Any]:
        """Apply Core's /log policy to subsequently started role processes."""

        self.prompt_log_enabled = bool(enabled)
        if not self._attached:
            return {
                "ok": True,
                "enabled": self.prompt_log_enabled,
                "manager_running": False,
            }
        return await self.client.request(
            "set_prompt_log_enabled",
            {"enabled": self.prompt_log_enabled},
        )

    def _on_harness_registry_generation(
        self,
        generation: MinionHarnessRegistryGeneration,
    ) -> None:
        if not self._attached:
            return
        try:
            self.client.replace_harness_registry_sync(generation.to_dict())
        except Exception as exc:
            self.last_error = (
                "harness registry sync failed: "
                f"{exc.__class__.__name__}: {exc}"
            )
            raise

    def _sync_harness_registry(self) -> None:
        if self.harness_registry is None:
            return
        self.client.replace_harness_registry_sync(
            self.harness_registry.snapshot().to_dict()
        )

    def has_pending_events(self) -> bool:
        with self._buffer_lock:
            events = list(self._buffered_events)
            inflight = set(self._inflight_event_keys)
        return any(
            self._buffered_event_key(item) not in inflight
            and self._prepare_delivery_event(item) is not None
            for item in events
        )

    def drain_events_sync(self, *, limit: int = 20) -> dict[str, Any]:
        with self._buffer_lock:
            candidates = list(self._buffered_events)
            inflight = set(self._inflight_event_keys)
        events: list[dict[str, Any]] = []
        for item in candidates:
            key = self._buffered_event_key(item)
            if key in inflight:
                continue
            prepared = self._prepare_delivery_event(item)
            if prepared is None:
                continue
            events.append(prepared)
            with self._buffer_lock:
                self._inflight_event_keys.add(key)
            if len(events) >= max(1, int(limit)):
                break
        with self._buffer_lock:
            remaining = len(self._buffered_events)
        return {"events": events, "remaining": remaining}

    def settle_event(
        self,
        event: dict[str, Any],
        *,
        accepted: bool,
        error: str = "",
    ) -> bool:
        """Commit or retry one event after Core has handled it.

        The manager outbox is acknowledged only after the concrete channel
        endpoint accepted the delivery.  A failed attempt releases the local
        in-flight reservation and leaves the durable outbox pending.
        """
        key = self._buffered_event_key(event)
        delivery_id = str(event.get("delivery_id") or "")
        if not accepted:
            if delivery_id:
                with contextlib.suppress(Exception):
                    self.client.request_sync(
                        "v2_defer_task_delivery",
                        {"delivery_id": delivery_id, "error": str(error or "delivery failed")},
                    )
            with self._buffer_lock:
                if not delivery_id:
                    self._buffered_events = [
                        item
                        for item in self._buffered_events
                        if self._buffered_event_key(item) != key
                    ]
                self._inflight_event_keys.discard(key)
                if not delivery_id:
                    self._seen_event_keys.discard(key)
            if self.event_notify is not None:
                with contextlib.suppress(Exception):
                    self.event_notify()
            return False
        try:
            if delivery_id:
                result = self.client.request_sync(
                    "v2_ack_task_delivery",
                    {"delivery_id": delivery_id},
                )
                # An already acknowledged row is still settled locally; the
                # manager is the source of truth for duplicate acknowledgements.
                if result.get("acknowledged") is False:
                    pass
        except Exception:
            # The provider accepted the message, but the durable ACK itself
            # was not confirmed.  Keep the payload for an at-least-once retry;
            # leaving it reserved here would strand it until the whole plugin
            # was restarted.  A duplicate is preferable to silent loss when
            # the manager may have committed the ACK just before the reply
            # failed.
            with self._buffer_lock:
                self._inflight_event_keys.discard(key)
            if self.event_notify is not None:
                with contextlib.suppress(Exception):
                    self.event_notify()
            return False
        with self._buffer_lock:
            self._buffered_events = [
                item
                for item in self._buffered_events
                if self._buffered_event_key(item) != key
            ]
            self._inflight_event_keys.discard(key)
            self._seen_event_keys.discard(key)
            if delivery_id:
                self._locally_delivered_parts.pop(delivery_id, None)
        return True

    def delivered_event_parts(self, event: dict[str, Any]) -> set[str]:
        delivery_id = str(event.get("delivery_id") or "").strip()
        if not delivery_id:
            return set()
        with self._buffer_lock:
            local = set(self._locally_delivered_parts.get(delivery_id, set()))
        result = self.client.request_sync(
            "v2_list_task_delivery_parts",
            {"delivery_id": delivery_id},
        )
        return local | {
            str(item)
            for item in list(result.get("parts") or ())
            if str(item).strip()
        }

    def settle_event_part(self, event: dict[str, Any], part_key: str) -> bool:
        delivery_id = str(event.get("delivery_id") or "").strip()
        normalized_part_key = str(part_key or "").strip()
        if not delivery_id or not normalized_part_key:
            return False
        # Keep the accepted part locally before the IPC acknowledgement. This
        # prevents an ACK retry in the same provider process from replaying a
        # channel side effect even if the manager reply is lost.
        with self._buffer_lock:
            self._locally_delivered_parts.setdefault(delivery_id, set()).add(
                normalized_part_key
            )
        result = self.client.request_sync(
            "v2_ack_task_delivery_part",
            {"delivery_id": delivery_id, "part_key": normalized_part_key},
        )
        return bool(result.get("acknowledged"))

    def _prepare_delivery_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        item = dict(event)
        delivery_id = str(item.get("delivery_id") or "")
        if not delivery_id:
            return item
        task_id = str(item.get("task_id") or "")
        binding_row = MinionV2WorkflowService(
            self.runtime_root
        ).repository.read_task_delivery(task_id)
        binding = dict((binding_row or {}).get("current") or {})
        route = self._live_route_for_binding(binding)
        if route is None:
            route = self._recovery_socket_route(delivery_id)
        if route is None:
            return None
        payload = dict(item.get("payload") or {})
        payload["route"] = route
        item["payload"] = payload
        return item

    def _live_route_for_binding(
        self,
        binding: dict[str, Any],
    ) -> dict[str, Any] | None:
        if self.context is None:
            return None
        runtime = self.context.port_registry.get("channel:channel")
        channel_id = str(binding.get("channel_id") or "")
        endpoint = runtime.get_endpoint(channel_id) if runtime is not None else None
        if endpoint is None or not endpoint.attached or not endpoint.enabled:
            return None
        inspect_health = getattr(endpoint, "inspect_health", None)
        health = inspect_health() if callable(inspect_health) else {}
        if isinstance(health, dict) and health.get("healthy") is False:
            return None
        target = dict(binding.get("reply_target") or {})
        sessions = getattr(endpoint, "sessions", None)
        if isinstance(sessions, dict):
            session_id = str(target.get("session_id") or "")
            session = sessions.get(session_id)
            if session is None or bool(getattr(session, "closed", False)):
                return None
            target["request_id"] = (
                f"task-notification:{uuid4().hex}"
            )
        if not target:
            return None
        return {
            "endpoint_id": channel_id,
            "channel_kind": str(binding.get("channel_kind") or endpoint.endpoint.channel_kind),
            "reply_target": target,
            "control_scope_key": str(
                binding.get("control_scope_key")
                or target.get("control_scope_key")
                or f"{endpoint.endpoint.channel_kind}:{channel_id}"
            ),
        }

    def _recovery_socket_route(self, delivery_id: str) -> dict[str, Any] | None:
        if self.context is None:
            return None
        runtime = self.context.port_registry.get("channel:channel")
        endpoints = runtime.list_endpoints() if runtime is not None else ()
        expected_path = (Path(self.runtime_root) / "pal.sock").resolve(strict=False)
        for endpoint in endpoints:
            if str(endpoint.endpoint.channel_kind or "") != "socket":
                continue
            socket_path = Path(
                getattr(endpoint, "socket_path", None) or endpoint.endpoint.binding_key
            ).expanduser().resolve(strict=False)
            if socket_path != expected_path or not endpoint.attached or not endpoint.enabled:
                continue
            inspect_health = getattr(endpoint, "inspect_health", None)
            health = inspect_health() if callable(inspect_health) else {}
            if isinstance(health, dict) and health.get("healthy") is False:
                continue
            sessions = getattr(endpoint, "sessions", {})
            live = [
                session
                for session in sessions.values()
                if not bool(getattr(session, "closed", False))
            ]
            if not live:
                return None
            session = live[-1]
            request_id = f"task-notification:{delivery_id}"
            target = {
                "session_id": str(session.session_id),
                "request_id": request_id,
                "control_scope_key": (
                    f"socket:{endpoint.endpoint.endpoint_id}:{session.session_id}"
                ),
            }
            return {
                "endpoint_id": endpoint.endpoint.endpoint_id,
                "channel_kind": "socket",
                "reply_target": target,
                "control_scope_key": target["control_scope_key"],
            }
        return None

    @staticmethod
    def _buffered_event_key(event: dict[str, Any]) -> str:
        return str(event.get("delivery_id") or "") or "|".join(
            (
                str(event.get("event_kind") or ""),
                str(event.get("workflow_id") or ""),
                str(event.get("run_id") or ""),
            )
        )

    async def handle_control_action_async(self, action: ControlAction) -> str:
        if action.action_kind == "minion_v2_human_decision":
            decision = str(action.args.get("decision") or "").strip().lower()
            if decision == "edit" and not str(
                action.args.get("edit_instruction") or ""
            ).strip():
                return "Reply with the exact architecture edit instruction, then submit it with minion_submit_human_decision."
            try:
                result = await asyncio.to_thread(
                    MinionV2WorkflowService(self.runtime_root).submit_human_decision,
                    {
                        "decision_token": str(action.args.get("decision_token") or ""),
                        "decision": decision,
                        **(
                            {
                                "edit_instruction": str(
                                    action.args.get("edit_instruction") or ""
                                ),
                            }
                            if decision == "edit"
                            else {}
                        ),
                        "actor": str(action.args.get("actor_id") or "pal"),
                        "source_channel": "control",
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
        # A manager/process boundary ends the local delivery attempt, not the
        # durable event.  Let a reattached provider retry buffered events.
        with self._buffer_lock:
            self._inflight_event_keys.clear()

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
        key = self._buffered_event_key(event)
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
    harness_registry: MinionHarnessRegistry | None = None,
) -> ModuleHandle:
    _ = service
    resolved_root = Path(runtime_root or context.execution_runtime.runtime_root or Path.cwd() / ".pal-minion")
    manager = MinionManagerProvider(
        runtime_root=resolved_root,
        context=context,
        harness_registry=harness_registry,
    )
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
    if manager._unsubscribe_harness_registry is not None:
        handle.cleanup_callbacks.append(
            manager._unsubscribe_harness_registry
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
