"""Prompt-cache reminder cards and their lifecycle presentation."""
from __future__ import annotations

from uuid import uuid4

from pal.control.contracts import ControlDelivery, ControlRoute, InteractionButtonSpec, InteractionMessageSpec
from pal.control.presentation import markdown_literal
from pal.control.interactions import (
    build_terminal_interaction,
    control_panel_interaction_id,
    delivery_for_interaction,
    delivery_for_reply,
)


def cache_policy_panel_delivery(
    route: ControlRoute | None, state: dict,
) -> ControlDelivery:
    """Use the same persistent control panel as model and thinking selection."""
    current = state["mode"]
    enabled = "enabled" if state["enabled"] else "disabled"
    text = (
        f"# Cache policy\nCurrent: **{current}** ({enabled})\n\n"
        "**reminder** — Manual Compact\n"
        "Notify you before the estimated cache deadline so you can Compact manually. "
        "**No paid heartbeats.**\n\n"
        "**keep-alive** — Automatic renewal\n"
        "- Renew an eligible warm cache on supported explicit-cache HTTP routes "
        "while idle using bounded paid requests, without changing L1 or thinking effort.\n"
        "- **At most 19 actions** within a price-based budget; the last slot is reserved "
        "for one automatic Compact if the warm prefix is still verified.\n"
        "- Stop on unconfirmed renewal. Unsupported routes or failed renewals fall back to reminders."
    )
    if current == "keep-alive":
        keep = state["keepalive"]
        text += (f"\n\n**Keep-alive status**\n"
                 f"Renewals: {keep.get('renewals', 0)}; action limit: {keep.get('max_actions', 0)}.\n"
                 f"Estimated cost: ${float(keep.get('estimated_cost', 0)):.6f}\n"
                 f"{markdown_literal(str(keep.get('reason', 'unavailable')))}.")
    if state["seconds_until_notice"] is not None:
        text += f"\nNext action in {state['seconds_until_notice']}s."
    if not state["enabled"]:
        text += "\n\n**Both policies are currently disabled.**"
    modes = ("reminder", "keep-alive")
    labels = tuple(f"> {mode}" if mode == current else mode for mode in modes)
    if route is None:
        return delivery_for_reply(None, text + "\n\n" + "\n".join(markdown_literal(label) for label in labels)
                                  + "\nUse /cache_policy [reminder|keep-alive].")
    buttons = tuple(
        (InteractionButtonSpec(label=label, action_key="control.command.run",
                               action_args={"command_name": "cache_policy", "argv": [mode]}),)
        for mode, label in zip(modes, labels)
    ) + ((InteractionButtonSpec(label="Back", action_key="control.panel.back"),),)
    return delivery_for_interaction(route, "interactive_update", InteractionMessageSpec(
        interaction_id=control_panel_interaction_id(route),
        interaction_kind="control_panel", route=route, text=text,
        buttons=buttons, expires_at=None,
    ))


def cache_warm_deadline_interaction_id(epoch: str) -> str:
    normalized = str(epoch or "").strip() or uuid4().hex[:12]
    return f"cache_warm_{normalized[:24]}"

def cache_warm_deadline_delivery(
    route: ControlRoute,
    *,
    epoch: str,
    prefix_tokens: int,
    lead_seconds: int,
    expires_at: str,
    estimated: bool = False,
) -> ControlDelivery:
    minutes = max(1, int((max(0, lead_seconds) + 59) // 60))
    text = (
        f"Your conversation cache contains approximately {max(0, prefix_tokens):,} tokens "
        f"and will expire in about {minutes} minutes. "
        "Compact now to reuse the warm cache and reduce the cost of rebuilding context."
    )
    if estimated:
        text = (
            f"The conversation of approximately {max(0, prefix_tokens):,} tokens is approaching "
            f"its estimated cache deadline, with about {minutes} minutes remaining. Compact if needed. "
            "This estimate is based on response completion time and the cache policy; "
            "cache reuse is not guaranteed."
        )
    interaction = InteractionMessageSpec(
        interaction_id=cache_warm_deadline_interaction_id(epoch),
        interaction_kind="cache_warm_deadline",
        route=route,
        text=text,
        buttons=(
            (
                InteractionButtonSpec(
                    label="Compact now",
                    action_key="control.action.dispatch",
                    action_args={"action_kind": "cache_warm_deadline_compact",
                                 "target_scope": "cache_reminder", "args": {"cache_epoch": epoch}},
                ),
            ),
            (
                InteractionButtonSpec(
                    label="Dismiss this reminder",
                    action_key="control.action.dispatch",
                    action_args={
                        "action_kind": "cache_warm_deadline_ignore",
                        "target_scope": "core",
                        "args": {"cache_epoch": epoch},
                    },
                ),
                InteractionButtonSpec(
                    label="Disable reminders",
                    action_key="control.action.dispatch",
                    action_args={
                        "action_kind": "cache_warm_deadline_disable",
                        "target_scope": "core",
                        "args": {"cache_epoch": epoch},
                    },
                ),
            ),
        ),
        expires_at=expires_at,
    )
    return delivery_for_interaction(
        route,
        "interactive_open",
        interaction,
    )

def cache_warm_deadline_expire_delivery(
    route: ControlRoute,
    *,
    epoch: str,
) -> ControlDelivery:
    interaction_id = cache_warm_deadline_interaction_id(epoch)
    return delivery_for_interaction(
        route,
        "interactive_expire",
        build_terminal_interaction(
            interaction_id=interaction_id,
            interaction_kind="cache_warm_deadline",
            route=route,
            text="",
        ),
        payload={"delete": True},
    )
