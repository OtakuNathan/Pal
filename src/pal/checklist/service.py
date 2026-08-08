from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Literal

ChecklistStatus = Literal["pending", "in_progress", "completed"]
_VALID_STATUSES = frozenset({"pending", "in_progress", "completed"})


@dataclass
class ChecklistItem:
    step: str
    status: ChecklistStatus = "pending"


@dataclass(frozen=True)
class ChecklistSnapshot:
    plan: tuple[dict[str, Any], ...]
    done: int
    total: int
    markdown: str
    active: bool


@dataclass(frozen=True)
class CheckOutcome:
    changed: bool
    snapshot: ChecklistSnapshot | None
    found: bool


class ChecklistService:
    """In-memory scratchpad checklist for Pal's own multi-step work.

    Plain runtime memory: not persisted, not projected into L2/memory, and
    never routed by a manager. The active plan is a single slot because Pal
    executes one turn at a time; a new upsert replaces the old plan.

    The item shape borrows minion's checklist format
    (``{"plan": [{"step": ..., "status": ...}]}``) so the shapes stay
    interoperable, but this is deliberately NOT a cursor: no gating, no
    ordering enforcement, no external authority. Pal opens a list when work
    is fragmented but easy, ticks items off as they finish, verifies at the
    end, and clears it.
    """

    MAX_ITEMS = 64
    MAX_STEP_CHARS = 1000
    MAX_MARKDOWN_CHARS = 4000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: list[ChecklistItem] | None = None

    def upsert(self, plan: list[dict[str, Any]]) -> ChecklistSnapshot:
        with self._lock:
            if not plan:
                raise ValueError("checklist plan must contain at least one step")
            if len(plan) > self.MAX_ITEMS:
                raise ValueError(f"checklist plan exceeds {self.MAX_ITEMS} steps")
            items: list[ChecklistItem] = []
            for entry in plan:
                step = str(entry.get("step") or "").strip()
                if not step:
                    raise ValueError("checklist steps must be non-empty strings")
                if len(step) > self.MAX_STEP_CHARS:
                    raise ValueError(f"checklist step exceeds {self.MAX_STEP_CHARS} chars")
                status = str(entry.get("status") or "pending").strip()
                if status not in _VALID_STATUSES:
                    raise ValueError(f"invalid checklist status: {status!r}")
                items.append(ChecklistItem(step=step, status=status))  # type: ignore[arg-type]
            self._active = items
            return self._snapshot_locked()

    def check(self, step: str) -> CheckOutcome:
        step = str(step or "").strip()
        with self._lock:
            if self._active is None:
                return CheckOutcome(changed=False, snapshot=None, found=False)
            changed = False
            found = False
            for item in self._active:
                if item.step == step:
                    found = True
                    if item.status != "completed":
                        item.status = "completed"
                        changed = True
                    break
            return CheckOutcome(changed=changed, snapshot=self._snapshot_locked(), found=found)

    def show(self) -> ChecklistSnapshot | None:
        with self._lock:
            return self._snapshot_locked() if self._active is not None else None

    def clear(self) -> bool:
        with self._lock:
            if self._active is None:
                return False
            self._active = None
            return True

    def _snapshot_locked(self) -> ChecklistSnapshot:
        items = self._active or []
        plan = tuple({"step": item.step, "status": item.status} for item in items)
        done = sum(1 for item in items if item.status == "completed")
        return ChecklistSnapshot(
            plan=plan,
            done=done,
            total=len(items),
            markdown=self._render_markdown_locked(),
            active=self._active is not None,
        )

    def _render_markdown_locked(self) -> str:
        items = self._active or []
        if not items:
            return ""
        done = sum(1 for item in items if item.status == "completed")
        lines = [f"清单进度 {done}/{len(items)}"]
        for item in items:
            mark = "✅" if item.status == "completed" else "⬜"
            lines.append(f"{mark} {item.step}")
        return "\n".join(lines)
