from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Callable, Literal

from pydantic import Field, create_model

from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.execution.capability_compiler import compile_provider_subtree
from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import EffectOutcome, EffectReceipt, StrictToolModel, StructuredToolOutput, ToolGuidance
from pal.execution.tool_semantics import DIRECT_EXTERNAL_WRITE
from pal.shared import OPERATION_NAMESPACE, RuntimeStatus, capability_action, capability_node
from pal.shared.result_rendering import render_titled_structured_for_llm


EMOTION_ALIASES = ("show_emotion",)
EMOTION_GUIDANCE = ToolGuidance(
    search_domain="emotion", search_domain_aliases=("avatar", "expression"),
    search_domain_description="Expressions broadcast to mounted face and desktop renderers.",
    search_objects=('emotion', 'emotions', 'expression', 'expressions', 'avatar', 'face'),
    search_terms=('express', 'emote', 'reaction', 'smile', 'display', 'animation', 'panel', 'desktop'),
    purpose="Broadcast one emotion to every enabled mounted renderer; each unsupported renderer skips it unchanged.",
    use_when="A visual reaction fits the conversation. Choose from the union of renderer-supported emotions. Inspect accepted, skipped and failures; acceptance does not prove pixels rendered. Runtime owns duration and activity-state restoration.",
    do_not_use_when="Repetitive or unrelated reactions, or proof of internal feelings. Routine thinking/working is automatic. Error is for an actual failure; shock expresses surprise.",
    failure_next_steps="Inspect the affected renderer or channel health. Other receivers are still attempted; no receivers or all unsupported means nothing was accepted.",
)


def emotion_contract(values=()):
    emotion_type = Literal[tuple(values)] if values else str
    model = create_model("EmotionInput", __base__=StrictToolModel,
                         emotion=(emotion_type, Field(..., min_length=1, description="Emotion to broadcast. Each renderer handles its own aliases and animation timing.")))
    guidance = EMOTION_GUIDANCE.model_copy(update={"search_enum_fields": ("emotion",) if len(values) > 1 else ()})
    return model, guidance


@dataclass(frozen=True)
class EmotionReceiver:
    name: str
    emotions: frozenset[str]
    send: Callable[[str], None]


@dataclass(frozen=True)
class _Renderer:
    scope: object
    emotions: frozenset[str]
    receivers: Callable[[], list[EmotionReceiver]]


class EmotionRegistry:
    def __init__(self, context):
        self.context = context
        self.handle = None
        self._renderers: dict[str, _Renderer] = {}
        self._lock = threading.RLock()
        self._closed = False

    def register(self, scope, name: str, emotions, receivers: Callable[[], list[EmotionReceiver]]):
        """Own registration by the contributing plugin generation's cleanup scope."""
        values = frozenset(str(value).strip().lower() for value in emotions)
        if not name or not values or "" in values:
            raise ValueError("Renderer name and supported emotions are required")
        with self._lock:
            if self._closed or name in self._renderers:
                raise RuntimeError("Emotion registry is closed or renderer already registered")
            renderer = _Renderer(scope, values, receivers)
            self._renderers[name] = renderer

            def unregister():
                with self._lock:
                    if self._renderers.get(name) is renderer:
                        self._renderers.pop(name)
                        self._refresh()

            scope.defer(unregister)
            try:
                self._refresh()
            except Exception:
                self._renderers.pop(name, None)
                raise
            return unregister

    def _refresh(self):
        if self._closed or self.handle is None or not self.handle.mounted:
            return
        subtree = self.handle.mounted_subtree
        if subtree is not None and not subtree.mounted:
            # Dependency-closure withdrawal may precede renderer cleanup.
            # Removing a contribution must never republish a retiring owner.
            return
        # This owner replaces only its own compiled subtree. Plugin lifecycle
        # admission fences renderer registration and retirement against calls.
        runtime = self.context.execution_runtime
        runtime.hydrate_module_handle(self.handle)
        self.handle.published_capabilities = runtime.mount_subtree(self.handle)

    def close(self):
        with self._lock:
            self._closed = True
            self._renderers.clear()

    def vocabulary(self):
        with self._lock:
            return sorted({emotion for renderer in self._renderers.values() for emotion in renderer.emotions})

    def broadcast(self, emotion: str):
        with self._lock:
            renderers = list(self._renderers.items())
        accepted, skipped, failures = [], [], []
        targets = 0
        for name, renderer in renderers:
            handle = getattr(renderer.scope, "handle", None)
            if not renderer.scope.published or (handle is not None and not handle.mounted):
                continue
            try:
                receivers = renderer.receivers()
            except Exception as exc:
                failures.append({"renderer": name, "reason": str(exc)[:200] or type(exc).__name__})
                continue
            for receiver in receivers:
                targets += 1
                if emotion not in receiver.emotions:
                    skipped.append({"renderer": receiver.name, "reason": "unsupported"})
                    continue
                try:
                    receiver.send(emotion)
                except Exception as exc:
                    failures.append({"renderer": receiver.name, "reason": str(exc)[:200] or type(exc).__name__})
                else:
                    accepted.append(receiver.name)
        outcome = ("partial" if failures else "accepted") if accepted else (
            "failed" if failures else "unsupported" if targets else "unavailable")
        payload = {"emotion": emotion, "outcome": outcome, "accepted": accepted, "skipped": skipped, "failures": failures}
        effect = EffectOutcome.UNKNOWN if failures else EffectOutcome.APPLIED if accepted else EffectOutcome.NOT_STARTED
        return CapabilityResult(
            status=RuntimeStatus.OK if accepted else RuntimeStatus.ERROR,
            text="Emotion broadcast", structured=payload,
            llm_text=render_titled_structured_for_llm("Emotion broadcast", payload),
            effect_receipt=EffectReceipt(outcome=effect),
        )

    def build_mounted_subtree(self, *, module_id, lifecycle_scope, detachable):
        values = self.vocabulary()
        Input, guidance = emotion_contract(values)
        registry = self

        @capability_node(namespace=OPERATION_NAMESPACE, scope="module", kind="module",
                         source="builtin:emotion", target_kind="module")
        class BroadcastProvider:
            @capability_action(
                namespace=OPERATION_NAMESPACE, scope="module", action_name="show",
                aliases=EMOTION_ALIASES, InputModel=Input, OutputModel=StructuredToolOutput,
                guidance=guidance, execution=DIRECT_EXTERNAL_WRITE, examples=({"emotion": values[0] if values else "happy"},),
            )
            def show(self, call):
                return registry.broadcast(call.args["emotion"])

        return compile_provider_subtree(BroadcastProvider(), module_id=module_id,
                                        lifecycle_scope=lifecycle_scope, detachable=detachable)


def build_emotion_handle(context):
    registry = EmotionRegistry(context)
    handle = ModuleHandle(module_id="emotion", tier=MODULE_TIER_DETACHABLE, detachable=True,
                          introspection_provider=registry, ports={"emotion": registry}, shutdown_sync=registry.close)
    registry.handle = handle
    context.register_module(handle)
    return handle
