"""Prompt-cache reminder cards and their lifecycle presentation."""
from __future__ import annotations

from uuid import uuid4

from pal.control.contracts import ControlDelivery, ControlRoute, InteractionButtonSpec, InteractionMessageSpec
from pal.control.interactions import delivery_for_interaction, build_terminal_interaction


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
