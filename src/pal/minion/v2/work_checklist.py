from __future__ import annotations

from typing import Any, Mapping, Sequence


CHECKLIST_STATUSES = frozenset(
    {"pending", "in_progress", "completed"}
)


def normalize_work_checklist(
    value: Any,
    *,
    require_nonempty: bool,
    owner: str,
    fixed_steps: Sequence[str] = (),
) -> dict[str, Any]:
    label = str(owner or "Work").strip() or "Work"
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} checklist must be an object")
    raw_plan = value.get("plan")
    if not isinstance(raw_plan, list):
        raise ValueError(f"{label} checklist plan must be an array")
    if require_nonempty and not raw_plan:
        raise ValueError(
            f"{label} checklist must contain at least one step"
        )
    plan: list[dict[str, str]] = []
    for index, raw_item in enumerate(raw_plan):
        if not isinstance(raw_item, Mapping):
            raise ValueError(
                f"{label} checklist plan[{index}] must be an object"
            )
        unexpected = sorted(set(raw_item) - {"step", "status"})
        if unexpected:
            raise ValueError(
                f"{label} checklist plan[{index}] has unknown fields: "
                + ", ".join(unexpected)
            )
        step = str(raw_item.get("step") or "").strip()
        status = str(raw_item.get("status") or "").strip()
        if not step:
            raise ValueError(
                f"{label} checklist plan[{index}].step must be non-empty"
            )
        if status not in CHECKLIST_STATUSES:
            raise ValueError(
                f"{label} checklist plan[{index}].status must be pending, "
                "in_progress, or completed"
            )
        plan.append({"step": step, "status": status})
    steps = [item["step"] for item in plan]
    duplicates = sorted(
        {step for step in steps if steps.count(step) > 1}
    )
    if duplicates:
        raise ValueError(
            f"{label} checklist steps must be unique: "
            + ", ".join(duplicates)
        )
    in_progress = [
        item["step"]
        for item in plan
        if item["status"] == "in_progress"
    ]
    if len(in_progress) > 1:
        raise ValueError(
            f"{label} checklist allows at most one in_progress item: "
            + ", ".join(in_progress)
        )
    expected = tuple(str(item) for item in fixed_steps)
    if expected and tuple(steps) != expected:
        raise ValueError(
            f"{label} checklist must preserve the fixed ordered steps"
        )
    return {"plan": plan}


def render_work_checklist(
    checklist: Mapping[str, Any],
    *,
    owner: str,
    purpose: str,
) -> str:
    lines = [
        f"{owner} checklist ({purpose}):",
    ]
    for item in list(checklist.get("plan") or []):
        entry = dict(item or {})
        lines.append(
            f"- {entry.get('status')}: {entry.get('step')}"
        )
    return "\n".join(lines)


def unfinished_work_checklist_steps(
    checklist: Mapping[str, Any],
) -> list[str]:
    return [
        str(dict(item or {}).get("step") or "")
        for item in list(checklist.get("plan") or [])
        if str(dict(item or {}).get("status") or "") != "completed"
    ]
