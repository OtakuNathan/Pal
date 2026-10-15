"""Pal plugin: render the avatar on the ST7789 colour panel.

Wiring and panel config live in panel.py / avatar.py (shared with the
prototypes). This module is the Pal-facing half: it supervises the sidecar
subprocess and maps turn events onto the colour face panel.

Division of labour Nathan asked for: the colour panel shows the character, the
OLED keeps the control/status readout.
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Empty
from typing import TYPE_CHECKING, Any

from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.core.core_events import (
    ALL_CORE_TOPICS,
    FAILURE_FINISHED,
    FAILURE_STARTED,
    MEMORY_SLEEP,
    RUNTIME_SNAPSHOT,
    SAFE_MODE_FINISHED,
    SAFE_MODE_STARTED,
    TURN_END,
    TURN_START,
    TURN_TOOL_CALL_AFTER,
    TURN_TOOL_CALL_BEFORE,
    TURN_TOOL_CALL_FAILED,
)
from pal.execution.tool_facade import ToolGuidance
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    OPERATION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
    capability_node,
)
from pal.shared.result_rendering import render_titled_structured_for_llm
from pal.shared.inspection import inspection_snapshot

if TYPE_CHECKING:
    from pal.core.main_context import MainContext

# The raster skin's own state table (pal-raster-avatar.js). Keep in sync with
# face_art.STATES / face_art.ALIASES. Spelled out here so Pal's process never
# has to load librsvg just to validate an emotion name.
FACES: tuple[str, ...] = (
    "standby", "thinking", "working", "sleeping", "greeting", "curious",
    "love", "wink", "shock", "happy", "cheeky", "smirk", "snacking",
    "drinking", "sad", "confused", "panic", "angry", "error", "shy",
    "awkward", "crying", "bored", "gloomy", "celebrate", "agree",
)
ALIASES: dict[str, str] = {
    "sleepy": "sleeping", "err": "error", "laugh": "happy", "clap": "happy",
    "excited": "happy", "proud": "smirk",
    "complain": "angry", "stretching": "greeting",
    "embarrassed": "awkward", "smug": "smirk", "playful": "cheeky",
    "surprised": "shock", "wave": "greeting",
    "snack": "snacking", "drink": "drinking", "stretch": "stretching",
}
ACCEPTED: tuple[str, ...] = FACES + tuple(ALIASES)


def _normalise(name: str) -> str:
    key = str(name or "").strip().lower()
    seen = set()
    while key in ALIASES and key not in seen:
        seen.add(key)
        key = ALIASES[key]
    return key


# A hand-requested animation is sent as "hold:<state>". The sidecar plays a held
# animation for its full duration and lets ambient turn traffic (thinking /
# working) only move the base pose underneath it; a bare state name would be
# indistinguishable from a turn event and got cut mid-animation by the very tool
# call that requested it.
MANUAL_STATE_PREFIX = "hold:"

# A sticky system pose, cleared with ``system:clear``. The sidecar treats it as
# outranking both ambient traffic and a hand-requested animation -- the same way
# the desktop avatar draws an active failure over everything else.
SYSTEM_STATE_PREFIX = "system:"


def _send_command(cmd: str, socket_path: Path, *, timeout: float = 3.0) -> str:
    if not socket_path.exists():
        return f"ERR: sidecar not running (socket not found at {socket_path})"
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(timeout)
        conn.connect(str(socket_path))
        conn.sendall(cmd.encode())
        response = conn.recv(1024).decode().strip()
        conn.close()
        return response
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller verbatim
        return f"ERR: {exc}"


def _sidecar_is_alive(socket_path: Path) -> bool:
    if not socket_path.exists():
        return False
    return _send_command("status", socket_path, timeout=1.0).startswith("{")


@dataclass
class FaceSidecarManager:
    plugin_dir: Path
    process: subprocess.Popen | None = None

    @property
    def sidecar_script(self) -> Path:
        return self.plugin_dir / "sidecar.py"

    @property
    def sidecar_dir(self) -> Path:
        return self.plugin_dir.parent.parent.parent / "data" / "face_sidecar"

    @property
    def sidecar_socket(self) -> Path:
        return self.sidecar_dir / "face.sock"

    def ensure_running(self) -> str:
        if _sidecar_is_alive(self.sidecar_socket):
            return "already_running"
        self._stop_orphaned()
        if not self.sidecar_script.exists():
            return f"ERR: sidecar script not found at {self.sidecar_script}"
        self.sidecar_dir.mkdir(parents=True, exist_ok=True)
        stderr_log = self.sidecar_dir / "sidecar_stderr.log"
        self.process = subprocess.Popen(
            ["/usr/bin/python3", str(self.sidecar_script),
             "--plugin-dir", str(self.plugin_dir),
             "--sidecar-dir", str(self.sidecar_dir)],
            stdout=subprocess.DEVNULL,
            stderr=open(stderr_log, "a"),
        )
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if _sidecar_is_alive(self.sidecar_socket):
                return "started"
            time.sleep(0.1)
        return "ERR: sidecar did not start within timeout"

    def stop_sync(self) -> None:
        if _sidecar_is_alive(self.sidecar_socket):
            _send_command("quit", self.sidecar_socket, timeout=1.0)
        process = self.process
        if process is None:
            return
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            try:
                process.terminate()
                process.wait(timeout=1.0)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        finally:
            self.process = None

    async def shutdown_async(self) -> None:
        await asyncio.to_thread(self.stop_sync)

    def _stop_orphaned(self) -> None:
        if self.sidecar_socket.exists() and not _sidecar_is_alive(self.sidecar_socket):
            try:
                self.sidecar_socket.unlink()
            except Exception:
                pass


class FaceObservationWorker:
    """Drive the panel from Core's system observations.

    Core publishes turn activity, tool failures, failure handling and the
    resident sleeping transition on one bus. This worker owns the socket I/O on
    its own thread so Core's bus callbacks never block on the panel (see
    docs/pal_core_events.md). The panel's own precedence rules settle the rest:
    a held hand animation and a sticky failure pose both outrank this ambient
    traffic, which only moves the base pose underneath them.
    """

    def __init__(self, socket_path: Path, observations: Any) -> None:
        self._socket_path = socket_path
        self._observations = observations
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._base = "standby"
        self._system: str | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="st7789-face-observations", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None

    def _send(self, command: str) -> None:
        _send_command(command, self._socket_path, timeout=1.5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                topic, payload = self._observations.get(timeout=0.1)
            except Empty:
                continue
            except Exception:  # closed mailbox, or a transient failure
                if self._stop.is_set():
                    return
                time.sleep(0.1)
                continue
            try:
                self._handle(topic, payload)
            except Exception:  # noqa: BLE001 - one bad frame must not kill the worker
                continue

    def _set_base(self, state: str) -> None:
        # Ambient traffic: a bare name never cuts a held animation or a failure.
        if state == self._base:
            return
        self._base = state
        self._send(state)

    def _show_system(self, state: str) -> None:
        if state == self._system:
            return
        self._system = state
        self._send(f"{SYSTEM_STATE_PREFIX}{state}")

    def _clear_system(self) -> None:
        if self._system is None:
            return
        self._system = None
        self._send(f"{SYSTEM_STATE_PREFIX}clear")

    def _handle(self, topic: str, payload: Any) -> None:
        event = payload if isinstance(payload, dict) else {}
        if topic == RUNTIME_SNAPSHOT:
            # Authoritative state: the first item on subscribe, and the resync
            # after an overflow, so derive sleeping and failures from it.
            if bool(event.get("sleeping")):
                self._set_base("sleeping")
            elif self._base == "sleeping":
                self._set_base("standby")
            if event.get("failures"):
                self._show_system("error")
            elif self._system is not None:
                self._clear_system()
        elif topic == TURN_START:
            self._set_base("thinking")
        elif topic == TURN_TOOL_CALL_BEFORE:
            self._set_base("working")
        elif topic == TURN_TOOL_CALL_AFTER:
            self._set_base("thinking")
        elif topic == TURN_TOOL_CALL_FAILED:
            # A one-shot startle; it expires on its own back to the base pose.
            self._flash("panic")
        elif topic == TURN_END:
            if event.get("status") == "failed":
                self._send("shock")
            self._set_base("standby")
        elif topic == MEMORY_SLEEP:
            self._set_base("sleeping" if event.get("sleeping") else "standby")
        elif topic in (FAILURE_STARTED, SAFE_MODE_STARTED):
            self._show_system("error")
        elif topic in (FAILURE_FINISHED, SAFE_MODE_FINISHED):
            self._refresh_failures()

    def _flash(self, state: str) -> None:
        # Mirror the desktop avatar: a sleeping resident is not startled by a
        # background tool failure, and an active failure already owns the panel.
        if self._base == "sleeping" or self._system is not None:
            return
        self._send(state)

    def _refresh_failures(self) -> None:
        bus = getattr(self._observations, "bus", None)
        failures = bus.snapshot().get("failures") if bus is not None else None
        if failures:
            self._show_system("error")
        else:
            self._clear_system()


@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="module",
    kind="module",
    source="community:st7789_face",
    target_kind="module",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="community:st7789_face",
    target_kind="module",
)
@dataclass
class FaceIntrospectionProvider:
    module_id: str = "st7789_face"
    mounted: bool = True
    degraded: bool = False
    sidecar_manager: FaceSidecarManager | None = None
    observation_worker: FaceObservationWorker | None = None
    observations: Any | None = None

    def start_observations(self, scope: Any) -> None:
        """Subscribe to Core's system observations and start the drain worker.

        Uses the plugin-scope mailbox (see docs/pal_core_events.md); the worker
        owns all socket I/O, so Core's bus callbacks never block on the panel.
        """
        if self.observation_worker is not None or self.sidecar_manager is None:
            return
        self.observations = scope.subscribe_core_events(ALL_CORE_TOPICS, max_pending=128)
        self.observation_worker = FaceObservationWorker(
            socket_path=self.sidecar_manager.sidecar_socket,
            observations=self.observations,
        )
        self.observation_worker.start()

    def stop_observations(self) -> None:
        worker = self.observation_worker
        self.observation_worker = None
        if worker is not None:
            worker.stop()

    def show_face(self, call: IntrospectionCall) -> IntrospectionResult:
        emotion = str(call.args.get("emotion") or "").strip().lower()
        if emotion not in ACCEPTED:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text=f"invalid emotion: {emotion}",
                llm_text=f"Invalid emotion '{emotion}'. Valid: {list(FACES)}",
            )
        socket_path = self.sidecar_manager.sidecar_socket if self.sidecar_manager else None
        if socket_path is None:
            return IntrospectionResult(
                status=RuntimeStatus.ERROR,
                text="no sidecar manager",
                llm_text="Face panel sidecar not configured.",
            )
        result = _send_command(f"{MANUAL_STATE_PREFIX}{_normalise(emotion)}", socket_path)
        if result.startswith("OK"):
            return IntrospectionResult(
                status=RuntimeStatus.OK,
                text=f"face shown: {emotion}",
                structured={"emotion": emotion, "response": result},
                llm_text=f"Showing '{emotion}' on the colour panel.",
            )
        return IntrospectionResult(
            status=RuntimeStatus.ERROR,
            text=f"failed to show face: {result}",
            llm_text=f"Failed to show '{emotion}' on the colour panel: {result}",
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="status",
        aliases=("st7789_face_status",),
        guidance=ToolGuidance(
            search_domain="emotion", search_domain_aliases=("avatar", "expression"),
            search_domain_description="Expressions on desktop avatars and physical face panels.",
            search_objects=("face", "panel", "device", "state", "health"),
            search_terms=("inspect", "configuration", "capabilities", "supported", "expression", "face", "panel", "sidecar", "device", "status", "health"),
            purpose="Inspect the face panel's current expression, sidecar health, configuration and supported expressions together.",
            use_when="live panel health or current expression is needed",
            do_not_use_when="the request is to change the expression",
            failure_next_steps="check the sidecar stderr log under the runtime data dir",
        ),
    )
    def status(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        manager = self.sidecar_manager
        socket_path = manager.sidecar_socket if manager else None
        configuration = {
            "module_id": "st7789_face", "plugin_dir": str(manager.plugin_dir) if manager else None,
            "sidecar_socket": str(socket_path) if socket_path else None,
            "supported_expressions": list(FACES),
            "expression_aliases": dict(ALIASES),
        }

        def health():
            if socket_path is None:
                raise RuntimeError("Face panel sidecar is not configured")
            payload = json.loads(_send_command("status", socket_path))
            if not isinstance(payload, dict):
                raise ValueError("Face panel status must be an object")
            return payload

        payload = inspection_snapshot({"configuration": configuration}, sections={"health": health}, source="face_panel")
        return IntrospectionResult(status=RuntimeStatus.OK, text="face panel diagnostics", structured=payload,
            llm_text=render_titled_structured_for_llm("Face panel", payload))

    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        payload = {
            "module_id": "st7789_face",
            "plugin_dir": str(self.sidecar_manager.plugin_dir) if self.sidecar_manager else None,
            "sidecar_socket": str(self.sidecar_manager.sidecar_socket) if self.sidecar_manager else None,
            "faces": list(FACES),
            "sidecar_running": _sidecar_is_alive(self.sidecar_manager.sidecar_socket) if self.sidecar_manager else False,
        }
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="st7789 face module info",
            structured=payload,
            llm_text=render_titled_structured_for_llm("ST7789 face module", payload),
        )


def register_with_core(context: MainContext, *, plugin_dir: Path) -> ModuleHandle:
    sidecar_manager = FaceSidecarManager(plugin_dir=plugin_dir)
    provider = FaceIntrospectionProvider(
        sidecar_manager=sidecar_manager,
    )
    handle = ModuleHandle(
        module_id="st7789_face",
        tier=MODULE_TIER_DETACHABLE,
        detachable=True,
        introspection_provider=provider,
        shutdown_sync=sidecar_manager.stop_sync,
        shutdown_async=sidecar_manager.shutdown_async,
    )
    context.register_module(handle)
    return handle
