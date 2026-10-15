#!/usr/bin/env python3
"""ST7789 face sidecar — renders Pal's avatar on the colour panel.

Spawned and supervised by the st7789_face plugin, mirroring the proven
oled_emotion pattern: a tiny line protocol over a unix socket plus a render
loop that keeps the face animated (blink, breathe, expression particles).

IMPORTANT — single threaded on purpose.
Rendering goes through librsvg + libcairo via ctypes. Calling that from a
worker thread segfaults the interpreter inside rsvg_handle_render_document
(reproduced: 200 frames fine on the main thread, hard SIGSEGV on a worker
thread). That shows up indirectly as "sidecar did not start within timeout",
so the socket is polled non-blocking from the same loop that renders.

The panel is driven in its native portrait order (240x320, MADCTL=0x00) — no
buffer rotation. face_art.py does the drawing.

State machine, mirroring the desktop avatar sidecar:
base poses (standby/thinking/working/sleeping) persist; any other pose plays for
face_art.state_duration() and then hands control back to the base. A long
silence drifts standby -> sleeping, and anything arriving while asleep wakes the
face with a shock before it settles into the new pose (same semantics as
StateMachine.on_client_message() in the desktop avatar sidecar). A standby face
also fidgets now and then -- snack, cyber cola, slump, gloom -- the same idle
rotation the avatar runs.

Three kinds of command arrive on the socket and they carry different authority:

  * ``system:<state>`` -- a sticky system pose (a live failure, published by
    Core and forwarded by the plugin). It owns the panel until
    ``system:clear``; ambient traffic only moves the base pose underneath it.
  * ``hold:<state>``   -- Pal asked for this animation by hand (show_face /
    show_expression). It owns the panel for its full duration.
  * ``<state>``        -- ambient traffic: Pal's turn events, i.e. "a message
    arrived" / "a tool is running".

Ambient traffic never cuts a held animation. It only moves the *base* pose
underneath it, so once the animation has played out the face settles into
whatever Pal is really doing. Without that split the tool call that *onsets* an
expression used to land one line later ("working", "thinking") and cut it off
mid-sip -- the desktop avatar is immune only because its StateMachine keeps a
separate _override_state for exactly this reason.

The backlight is deliberately left ON when this process exits: the panel keeps
its last frame in GRAM and the direct register write to BL persists, so a
crashed or restarting sidecar leaves a face on screen instead of a black hole.
"""

from __future__ import annotations

import json
import os
import random
import signal
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import face_art  # noqa: E402
from panel import Panel  # noqa: E402

WIDTH, HEIGHT = 240, 320


def _flag(flag: str, default: str = "") -> str:
    if flag in sys.argv:
        idx = sys.argv.index(flag)
        if idx + 1 < len(sys.argv):
            return sys.argv[idx + 1]
    return default


PLUGIN_DIR = Path(_flag("--plugin-dir", str(Path(__file__).resolve().parent)))
SIDECAR_DIR = Path(
    _flag("--sidecar-dir", str(PLUGIN_DIR.parent.parent.parent / "data" / "face_sidecar"))
)
FPS = float(_flag("--fps", "20"))
SPEED_HZ = int(_flag("--speed", "32000000"))

# Idle policy. Defaults match the desktop avatar's IDLE_TO_SLEEPING_SECONDS (1h),
# and the flags exist so the cycle can be exercised without waiting an hour.
IDLE_TO_SLEEPING_SECONDS = float(_flag("--idle-sleep-seconds", "3600"))
SLEEPING_WAKE_TIMEOUT = float(_flag("--sleep-wake-seconds", "3600"))

# Idle fidgets, mirroring IDLE_ACTIONS / playIdleAction() in the desktop avatar.
# A standby face occasionally reaches for a snack or the cyber cola, slumps, or
# gets gloomy; the same one never plays twice in a row and the gap is randomised
# so the panel does not look metronomic. The flags exist for exercising the
# cycle without waiting out the real 14-32s window.
IDLE_ACTIONS: tuple[str, ...] = ("snacking", "drinking", "bored", "gloomy")
IDLE_ACTION_MIN_SECONDS = float(_flag("--idle-action-min-seconds", "14"))
IDLE_ACTION_JITTER_SECONDS = float(_flag("--idle-action-jitter-seconds", "18"))

SOCKET_PATH = SIDECAR_DIR / "face.sock"
PID_PATH = SIDECAR_DIR / "face.pid"
LOG_PATH = SIDECAR_DIR / "face.log"

# Prefix marking a hand-requested animation; see the module docstring.
HOLD_PREFIX = "hold:"
HOLD_VERBS = ("hold", "play")

# Prefix marking a sticky system pose (a live failure). ``system:error`` owns
# the panel until ``system:clear`` arrives, outranking both ambient traffic and
# a hand-requested animation -- the same precedence the desktop avatar gives an
# active failure over its overrides.
SYSTEM_PREFIX = "system:"
SYSTEM_VERBS = ("system",)
SYSTEM_CLEAR_WORDS = ("", "clear", "none", "off")


def _parse_state(data: str) -> tuple[str, str] | None:
    """Split one state command into (state, mode), or None if unrecognised.

    ``hold:drinking`` (or ``play:drinking``) is Pal asking by hand; a bare
    ``drinking`` is ambient traffic; ``system:error`` is a sticky system pose
    and ``system:clear`` drops it. Mode is one of "manual", "ambient" or
    "system"; a system clear comes back as ("", "system").
    """
    verb, separator, target = data.partition(":")
    key = verb.strip().lower()
    if separator and key in SYSTEM_VERBS:
        name = target.strip()
        if name.lower() in SYSTEM_CLEAR_WORDS:
            return "", "system"
        if not face_art.is_valid(name):
            return None
        return face_art.normalise_state(name), "system"
    if separator and key in HOLD_VERBS:
        name = target.strip()
        if not face_art.is_valid(name):
            return None
        return face_art.normalise_state(name), "manual"
    if not face_art.is_valid(data):
        return None
    # Hand back the canonical name so callers never see a wire alias.
    return face_art.normalise_state(data), "ambient"


class FaceSidecar:
    def __init__(self) -> None:
        self._stop = False
        self._state = "standby"
        self._base = "standby"
        self._state_since = time.monotonic()
        self._last_activity = time.monotonic()
        self._waking = False
        self._pending: str | None = None
        # Deadline for a hand-requested animation; ambient traffic cannot move
        # the displayed state until it passes (0.0 means "no hold").
        self._hold_until = 0.0
        # A sticky system pose (live failure) that outranks everything until
        # system:clear; None means no system state is being shown.
        self._system_state: str | None = None
        self._last_idle_action: str | None = None
        self._next_idle_action_at = time.monotonic() + self._idle_action_delay()
        self._frames = 0
        self._last_error = ""
        self.panel: Panel | None = None
        self.face: face_art.RasterFace | None = None
        self._server: socket.socket | None = None

    # ---- plumbing ---------------------------------------------------
    def _log(self, message: str) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        print(line, flush=True)
        try:
            with LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:
            pass

    def _clear_stale(self) -> None:
        if PID_PATH.exists():
            try:
                old = int(PID_PATH.read_text().strip())
                if old != os.getpid() and old > 0:
                    os.kill(old, signal.SIGTERM)
                    time.sleep(0.3)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        if SOCKET_PATH.exists():
            try:
                SOCKET_PATH.unlink()
            except Exception:
                pass

    # ---- state ------------------------------------------------------
    def set_state(self, name: str, *, hold: float = 0.0) -> None:
        """Show ``name``; ``hold`` seconds of immunity for a hand-made request."""
        state = face_art.normalise_state(name)
        now = time.monotonic()
        if state in face_art.BASE_STATES:
            # A persistent pose should not restart its own animation on repeats.
            if state == self._state:
                return
            self._base = state
            self._last_activity = now
            if self._waking:
                # Record the new pose but let the wake-up shock finish playing;
                # _expire_expression() hands over as soon as it ends.
                return
        self._state = state
        self._state_since = now
        self._last_activity = now
        # Only a hand-requested animation leaves a hold behind: every other
        # transition clears it, so a stale deadline can never block turn events.
        self._hold_until = now + hold if hold else 0.0

    def _hold_active(self, now: float | None = None) -> bool:
        """True while a hand-requested animation still owns the panel."""
        return self._hold_until > (time.monotonic() if now is None else now)

    def receive_state(self, name: str, *, manual: bool = False) -> None:
        """Handle one incoming state command — i.e. "a message just arrived".

        ``manual`` marks a hand-requested animation. It preempts everything and
        cannot be cut; ambient traffic only moves the base pose underneath it.
        """
        self._arm_idle_action()
        state = face_art.normalise_state(name)
        now = time.monotonic()

        if self._system_state is not None:
            # A live failure owns the panel. Keep the base pose current so the
            # face settles correctly once it clears, but show nothing else.
            if state in face_art.BASE_STATES:
                self._base = state
                self._last_activity = now
            return

        if manual and state not in face_art.BASE_STATES:
            # Pal asked for this by hand, so it plays out in full. A newer
            # request restarts it from the top, which is the one thing allowed
            # to replace it. A base request is not an animation, so it falls
            # through to the base handling below.
            if self._base == "sleeping":
                # An explicit animation means Pal is awake.
                self._base = "standby"
            self._waking = False
            self._pending = None
            self.set_state(state, hold=face_art.state_duration(state))
            return

        if self._hold_active(now):
            # Keep the base pose current so the face settles into whatever Pal
            # is really doing once the animation ends, but never cut it short.
            if state in face_art.BASE_STATES:
                self._base = state
                self._last_activity = now
            return

        if self._base == "sleeping" and state != "sleeping":
            # Asleep: shock first, then settle into whatever actually arrived.
            # A bare expression leaves the base at standby; Pal's own turn
            # events push thinking/working a moment later when it really is busy.
            self._base = state if state in face_art.BASE_STATES else "standby"
            self._pending = None if state in face_art.BASE_STATES else state
            self._waking = True
            self.set_state("shock")
            return
        if state in face_art.BASE_STATES:
            if self._waking:
                self._pending = None
            self.set_state(state)
            return
        # A fresh expression preempts a wake-up shock in progress.
        self._waking = False
        self._pending = None
        self.set_state(state)

    def receive_system_state(self, name: str) -> None:
        """Show or clear a sticky system pose (a live failure).

        ``name`` empty means "clear". The pose outranks ambient traffic and a
        hand-requested animation, mirroring the desktop avatar, where an active
        failure is drawn over everything else until the failure list empties.
        """
        self._arm_idle_action()
        if not name:
            if self._system_state is None:
                return
            self._system_state = None
            self.set_state(self._base)
            return
        state = face_art.normalise_state(name)
        if state == self._system_state:
            return
        self._system_state = state
        self._waking = False
        self._pending = None
        self.set_state(state)

    def _expire_expression(self) -> None:
        """Return to the base pose once an expressive state has played out.

        Mirrors finish() -> select(base) in pal-raster-animation.js, so a wink
        or a wave settles back to whatever Pal was doing instead of sticking on
        screen forever. The wake-up shock is the one expression allowed to hand
        over to another expression first.
        """
        if self._system_state is not None:
            return
        if self._state in face_art.BASE_STATES:
            return
        if time.monotonic() - self._state_since < face_art.state_duration(self._state):
            return
        self._hold_until = 0.0
        if self._waking:
            self._waking = False
            pending = self._pending
            self._pending = None
            if pending is not None:
                self.set_state(pending)
                return
        self.set_state(self._base)

    # ---- idle fidgets ------------------------------------------------
    def _idle_action_delay(self) -> float:
        return IDLE_ACTION_MIN_SECONDS + random.random() * IDLE_ACTION_JITTER_SECONDS

    def _arm_idle_action(self) -> None:
        """Push the next fidget out; any real activity re-arms the gap."""
        self._next_idle_action_at = time.monotonic() + self._idle_action_delay()

    def _maybe_idle_action(self) -> None:
        """Play one idle fidget while the face is sitting in standby.

        Only standby fidgets, and the same action never plays twice in a row.
        The idle clock is deliberately left untouched: a fidget is the panel
        amusing itself, not the user being active, so it must not keep the face
        from ever drifting off to sleep.
        """
        if self._base != "standby" or self._state != "standby":
            return
        if time.monotonic() < self._next_idle_action_at:
            return
        choices = [name for name in IDLE_ACTIONS if name != self._last_idle_action]
        action = random.choice(choices or list(IDLE_ACTIONS))
        self._last_idle_action = action
        self._arm_idle_action()
        activity = self._last_activity
        self.set_state(action)
        self._last_activity = activity

    def _tick_idle(self) -> None:
        """Drift into sleeping after a long silence, then re-arm standby later.

        Mirrors StateMachine.tick() in the desktop avatar sidecar: only a
        standby face falls asleep, and only a base pose can be idle at all.
        """
        if self._waking or self._state not in face_art.BASE_STATES:
            return
        idle = time.monotonic() - self._last_activity
        if self._base == "standby" and idle >= IDLE_TO_SLEEPING_SECONDS:
            self.set_state("sleeping")
        elif self._state == "sleeping" and idle >= SLEEPING_WAKE_TIMEOUT:
            self.set_state("standby")
        else:
            self._maybe_idle_action()

    def snapshot(self) -> dict:
        return {
            "state": self._state,
            "base": self._base,
            "system": self._system_state,
            "sleeping": self._state == "sleeping",
            "waking": self._waking,
            "idle_seconds": round(time.monotonic() - self._last_activity, 1),
            "last_idle_action": self._last_idle_action,
            "manual": self._hold_active(),
            "hold_seconds_left": round(max(0.0, self._hold_until - time.monotonic()), 1),
            "next_idle_action_in": round(
                max(0.0, self._next_idle_action_at - time.monotonic()), 1
            ),
            "frames": self._frames,
            "fps_target": FPS,
            "spi_hz": SPEED_HZ,
            "size": f"{WIDTH}x{HEIGHT}",
            "idle_to_sleeping_seconds": IDLE_TO_SLEEPING_SECONDS,
            "last_error": self._last_error,
        }

    # ---- rendering --------------------------------------------------
    def _render_one(self, start: float) -> None:
        now = time.monotonic()
        try:
            self.panel.blit(
                self.face.render_rgb565(self._state, now - self._state_since, now - start)
            )
            self._frames += 1
            self._last_error = ""
        except Exception as exc:  # a dead panel must not kill the loop
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._log(f"frame failed: {self._last_error}")
            time.sleep(0.5)

    # ---- socket (polled from the render loop) ------------------------
    def _open_socket(self) -> None:
        SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
        self._clear_stale()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(SOCKET_PATH))
        server.listen(4)
        server.setblocking(False)
        self._server = server
        self._log(f"socket listening: {SOCKET_PATH}")

    def _drain_socket(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except BlockingIOError:
                return
            except Exception as exc:
                self._log(f"accept failed: {exc}")
                return
            try:
                conn.settimeout(0.2)
                data = conn.recv(1024).decode().strip()
                if data:
                    self._log(f"received: {data}")
                if data in ("quit", "exit", "stop"):
                    self._stop = True
                    conn.sendall(b"OK: shutting down\n")
                elif data == "status":
                    conn.sendall((json.dumps(self.snapshot()) + "\n").encode())
                elif (command := _parse_state(data)) is not None:
                    state, mode = command
                    if mode == "system":
                        self.receive_system_state(state)
                    else:
                        self.receive_state(state, manual=(mode == "manual"))
                    conn.sendall(b"OK\n")
                else:
                    conn.sendall(
                        f"ERR: unknown emotion '{data}'. Valid: {list(face_art.STATES)}\n".encode()
                    )
            except socket.timeout:
                pass
            except Exception as exc:
                self._log(f"command failed: {exc}")
            finally:
                conn.close()

    # ---- lifecycle --------------------------------------------------
    def run(self) -> None:
        SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
        PID_PATH.write_text(str(os.getpid()))

        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_stop", True))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "_stop", True))

        self.panel = Panel(width=WIDTH, height=HEIGHT, speed_hz=SPEED_HZ)
        self.panel.initialise()
        self.panel.set_backlight(True)
        self.face = face_art.RasterFace(WIDTH, HEIGHT)
        self._open_socket()
        self._log(
            f"panel up: {WIDTH}x{HEIGHT}, spi={SPEED_HZ / 1e6:g} MHz, fps={FPS} "
            f"(single-threaded, raster art; sleep after {IDLE_TO_SLEEPING_SECONDS:g}s idle)"
        )

        start = time.monotonic()
        while not self._stop:
            self._drain_socket()
            if self._stop:
                break
            self._expire_expression()
            self._tick_idle()
            self._render_one(start)
            slack = (start + self._frames / FPS) - time.monotonic()
            if slack > 0:
                time.sleep(slack)

        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                pass
        for path in (SOCKET_PATH, PID_PATH):
            try:
                path.unlink()
            except Exception:
                pass
        # Leave the panel lit: the last frame survives in GRAM and the BL
        # register write persists, so a restart is not a black screen.
        self._log(f"sidecar stopped after {self._frames} frames; panel left lit")


def main() -> int:
    FaceSidecar().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
