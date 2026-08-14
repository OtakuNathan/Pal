from __future__ import annotations

from typing import Any, Mapping


# Increment whenever the human-review rendering contract changes. Persisted cards
# are content-addressed delivery artifacts, not an eternal presentation cache.
HUMAN_REVIEW_RENDER_VERSION = 2


def human_review_card_is_current(
    payload: Mapping[str, Any] | None,
    *,
    manifest_sha: str,
) -> bool:
    """Return whether a persisted card uses the active renderer and manifest."""

    card = dict(payload or {})
    try:
        render_version = int(card.get("render_version") or 0)
    except (TypeError, ValueError):
        return False
    return (
        render_version == HUMAN_REVIEW_RENDER_VERSION
        and bool(manifest_sha)
        and str(card.get("manifest_sha") or "") == str(manifest_sha)
    )


def task_revision_review_markdown(task_ledger: Mapping[str, Any]) -> str:
    """Render ordered Manager-recorded user decisions for human review."""

    revisions = [
        dict(item or {}) for item in list(task_ledger.get("revisions") or [])
    ]
    if not revisions:
        return ""
    lines = ["## Task Revision History", ""]
    for revision in revisions:
        authority = dict(revision.get("authority") or {})
        sequence = int(revision.get("sequence") or 0)
        lines.extend(
            [
                f"### Revision {sequence}",
                "",
                f"- Origin: {authority.get('origin', '')}",
                f"- User decision: {authority.get('question', '')}",
                f"- Exact answer: {authority.get('answer', '')}",
                f"- Observed: {authority.get('observed_at', '')}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()
