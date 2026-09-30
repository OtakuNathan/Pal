from __future__ import annotations
from pal.bunshin.runner_components.research_values import _web_research_capability_name
from pal.bunshin.runner_components.research_values import _web_research_budget_keys
from pal.bunshin.runner_components.research_values import _optional_nonnegative_int
from dataclasses import dataclass, field
from typing import Any
from pal.shared import BunshinInvocationPack


@dataclass
class ResearchBudget:
    pack: BunshinInvocationPack
    web_research_usage: dict[str, int] = field(default_factory=dict)

    def web_research_budget_status(self, capability_name: str) -> dict[str, Any] | None:
        canonical_name = _web_research_capability_name(capability_name)
        if canonical_name is None:
            return None
        budget_policy = (self.pack.approval_policy or {}).get("web_research_budget")
        if budget_policy is None:
            return None
        statuses: list[dict[str, Any]] = []
        if isinstance(budget_policy, dict):
            total_budget = _optional_nonnegative_int(
                budget_policy.get("total", budget_policy.get("web", budget_policy.get("all")))
            )
            if total_budget is not None:
                statuses.append({"key": "total", "used": self.web_research_usage.get("total", 0), "budget": total_budget})
            capability_budget = None
            for key in _web_research_budget_keys(canonical_name):
                if key in budget_policy:
                    capability_budget = _optional_nonnegative_int(budget_policy.get(key))
                    break
            if capability_budget is not None:
                statuses.append(
                    {"key": canonical_name, "used": self.web_research_usage.get(canonical_name, 0), "budget": capability_budget}
                )
        else:
            total_budget = _optional_nonnegative_int(budget_policy)
            if total_budget is not None:
                statuses.append({"key": "total", "used": self.web_research_usage.get("total", 0), "budget": total_budget})
        if not statuses:
            return None
        exceeded = [status for status in statuses if int(status["used"]) >= int(status["budget"])]
        return exceeded[0] if exceeded else statuses[0]

    def record_web_research_usage(self, capability_name: str) -> None:
        canonical_name = _web_research_capability_name(capability_name)
        if canonical_name is None:
            return
        self.web_research_usage[canonical_name] = self.web_research_usage.get(canonical_name, 0) + 1
        self.web_research_usage["total"] = self.web_research_usage.get("total", 0) + 1

    def restore(self, usage: dict[str, int]) -> None:
        self.web_research_usage = dict(usage)
