from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pal.behavior.decorators import affordance
from pal.behavior.contracts import AFFORDANCE_ACTIVATION_DELIBERATIVE, AFFORDANCE_VISIBILITY_RESIDENT
from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
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

if TYPE_CHECKING:
    from pal.core.main_context import MainContext

SOCKET_PATH = Path.home() / ".pal" / "oled_daemon" / "oled.sock"

VALID_EMOTIONS = [
    "happy", "sad", "angry", "crying", "curious",
    "shock", "sleepy", "thinking", "wink", "error",
    "standby", "working",
]


def _send_emotion(emotion: str) -> str:
    if not SOCKET_PATH.exists():
        return f"ERR: daemon not running (socket not found at {SOCKET_PATH})"
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3.0)
        s.connect(str(SOCKET_PATH))
        s.sendall(emotion.encode())
        resp = s.recv(1024).decode().strip()
        s.close()
        return resp
    except Exception as e:
        return f"ERR: {e}"


@affordance(
    affordance_id="oled_emotion_express",
    title="OLED 情绪与状态表达",
    scenario_text="Pal wants to express an emotion, mood, or working state through the OLED display on the device",
    prompt_hint=(
        "Pal has an SSD1306 OLED display capable of showing facial emotion and state animations. "
        "Use the oled_emotion.show_emotion capability to display expressions. "
        "Available: happy, sad, angry, crying, curious, shock, sleepy, thinking, wink, error, standby, working.\n\n"
        "Usage rules:\n"
        "1. Emotions: When you genuinely feel an emotion that matches an available expression, show it. "
        "Be natural — don't over-display, but don't suppress either.\n"
        "2. States: When you are actively thinking/reasoning, show 'thinking'. "
        "When you are executing tools or performing tasks, show 'working'. "
        "These are mandatory — always reflect your current working state on the display.\n"
        "3. Each call plays the animation once (~2s) then returns to standby. "
        "Don't call repeatedly in a loop; once per state change is enough."
    ),
    visibility_mode=AFFORDANCE_VISIBILITY_RESIDENT,
    activation_kind=AFFORDANCE_ACTIVATION_DELIBERATIVE,
    capability_refs=("oled_emotion.show_emotion",),
    activation_terms=(
        "emotion", "feel", "mood", "happy", "sad", "angry", "excited",
        "curious", "surprised", "shocked", "thinking", "sleepy", "wink",
        "expression", "face", "oled", "display emotion", "working state",
    ),
    priority=90,
)
@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:oled_emotion",
    target_kind="module",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:oled_emotion",
    target_kind="module",
)
@dataclass
class OledEmotionIntrospectionProvider:
    module_id: str = "oled_emotion"
    mounted: bool = True
    degraded: bool = False

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        action_name="show_emotion",
        description="Display an emotion animation on the SSD1306 OLED via the daemon process",
        args_schema={
            "type": "object",
            "properties": {
                "emotion": {
                    "type": "string",
                    "enum": VALID_EMOTIONS,
                    "description": "Emotion to display",
                },
            },
            "required": ["emotion"],
        },
    )
    def show_emotion(self, call: IntrospectionCall) -> IntrospectionResult:
        emotion = str(call.args.get("emotion") or "").strip().lower()
        if emotion not in VALID_EMOTIONS:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text=f"invalid emotion: {emotion}",
                llm_text=f"Invalid emotion '{emotion}'. Valid: {VALID_EMOTIONS}",
            )
        result = _send_emotion(emotion)
        if result.startswith("OK"):
            return IntrospectionResult(
                status=RuntimeStatus.OK,
                text=f"emotion displayed: {emotion}",
                structured={"emotion": emotion, "response": result},
                llm_text=f"Displayed '{emotion}' on OLED.",
            )
        else:
            return IntrospectionResult(
                status=RuntimeStatus.ERROR,
                text=f"failed to display emotion: {result}",
                llm_text=f"Failed to display '{emotion}' on OLED: {result}",
            )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="status",
        description="Check OLED emotion daemon status",
    )
    def status(self, call: IntrospectionCall) -> IntrospectionResult:
        resp = _send_emotion("status")
        try:
            payload = json.loads(resp)
        except Exception:
            payload = {"raw": resp}
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="oled daemon status",
            structured=payload,
            llm_text=render_titled_structured_for_llm("OLED daemon status", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="show",
        description="Show OLED emotion module info",
    )
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        payload = {
            "module_id": "oled_emotion",
            "socket_path": str(SOCKET_PATH),
            "valid_emotions": VALID_EMOTIONS,
            "daemon_running": SOCKET_PATH.exists(),
        }
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="oled emotion module info",
            structured=payload,
            llm_text=render_titled_structured_for_llm("OLED emotion module", payload),
        )


def register_with_core(context: MainContext) -> ModuleHandle:
    provider = OledEmotionIntrospectionProvider()
    handle = ModuleHandle(
        module_id="oled_emotion",
        tier=MODULE_TIER_DETACHABLE,
        detachable=True,
        introspection_provider=provider,
        supports_lifecycle_capabilities=True,
    )
    context.register_module(handle)
    return handle
