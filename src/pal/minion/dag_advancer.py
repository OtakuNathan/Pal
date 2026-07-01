from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


READY_STATUSES = frozenset({"ready", "needs_repair", "stale"})
PRESERVED_STATUSES = frozenset({"ready", "blocked", "failed", "paused", "needs_repair", "stale"})


@dataclass(frozen=True)
class DagSpec:
    """Static DAG topology. Runtime aliases may say module; storage says node."""

    node_order: tuple[str, ...]
    node_kind: dict[str, str] = field(default_factory=dict)
    depends_on: dict[str, tuple[str, ...]] = field(default_factory=dict)
    dependents: dict[str, tuple[str, ...]] = field(default_factory=dict)
    default_executor_profile: str = ""
    node_executors: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_validation(cls, validation: dict[str, Any], existing: dict[str, Any] | None = None) -> DagSpec:
        nodes = [dict(item) for item in list(dict(validation or {}).get("nodes") or []) if isinstance(item, dict)]
        node_to_module = {
            str(node.get("node_id") or "").strip(): str(node.get("module_id") or "").strip()
            for node in nodes
            if str(node.get("node_id") or "").strip() and str(node.get("module_id") or "").strip()
        }
        node_order = tuple(
            _dedupe_text(
                [
                    str(node.get("module_id") or "").strip()
                    for node in nodes
                    if str(node.get("module_id") or "").strip()
                ]
            )
        )
        node_kind: dict[str, str] = {}
        depends_on: dict[str, tuple[str, ...]] = {}
        for node in nodes:
            module_id = str(node.get("module_id") or "").strip()
            if not module_id:
                continue
            kind = str(node.get("kind") or "module").strip().lower()
            node_kind[module_id] = kind if kind in {"prelude", "module", "join"} else "module"
            deps: list[str] = []
            for dep_node_id in _coerce_text_list(node.get("depends_on")):
                dep_module_id = node_to_module.get(dep_node_id, dep_node_id)
                if dep_module_id and dep_module_id != module_id:
                    deps.append(dep_module_id)
            depends_on[module_id] = tuple(_dedupe_text(deps))

        dependents: dict[str, list[str]] = {module_id: [] for module_id in node_order}
        for module_id, deps in depends_on.items():
            for dep in deps:
                dependents.setdefault(dep, [])
                if module_id not in dependents[dep]:
                    dependents[dep].append(module_id)

        existing = dict(existing or {})
        existing_node_executors = dict(existing.get("node_executors") or {})
        return cls(
            node_order=node_order,
            node_kind={module_id: node_kind.get(module_id, "module") for module_id in node_order},
            depends_on={module_id: depends_on.get(module_id, ()) for module_id in node_order},
            dependents={module_id: tuple(dependents.get(module_id, [])) for module_id in node_order},
            default_executor_profile=str(existing.get("default_executor_profile") or "").strip(),
            node_executors={
                module_id: str(existing_node_executors.get(module_id) or "").strip()
                for module_id in node_order
                if str(existing_node_executors.get(module_id) or "").strip()
            },
        )

    @classmethod
    def from_dict(cls, dag: dict[str, Any]) -> DagSpec:
        source = dict(dag or {})
        node_order = tuple(_coerce_text_list(source.get("module_order") or source.get("node_order")))
        depends_on = {
            str(key): tuple(_coerce_text_list(value))
            for key, value in dict(source.get("depends_on") or {}).items()
            if str(key or "").strip()
        }
        dependents = {
            str(key): tuple(_coerce_text_list(value))
            for key, value in dict(source.get("dependents") or {}).items()
            if str(key or "").strip()
        }
        if not dependents and depends_on:
            mutable_dependents: dict[str, list[str]] = {module_id: [] for module_id in node_order}
            for module_id, deps in depends_on.items():
                for dep in deps:
                    mutable_dependents.setdefault(dep, [])
                    if module_id not in mutable_dependents[dep]:
                        mutable_dependents[dep].append(module_id)
            dependents = {module_id: tuple(values) for module_id, values in mutable_dependents.items()}
        return cls(
            node_order=node_order,
            node_kind={
                str(key): str(value)
                for key, value in dict(source.get("module_kind") or source.get("node_kind") or {}).items()
                if str(key or "").strip()
            },
            depends_on=depends_on,
            dependents=dependents,
            default_executor_profile=str(source.get("default_executor_profile") or "").strip(),
            node_executors={
                str(key): str(value)
                for key, value in dict(source.get("node_executors") or {}).items()
                if str(key or "").strip() and str(value or "").strip()
            },
        )

    def to_dict(self) -> dict[str, Any]:
        node_order = list(self.node_order)
        return {
            "module_order": node_order,
            "module_kind": {
                module_id: str(self.node_kind.get(module_id) or "module")
                for module_id in node_order
            },
            "depends_on": {
                module_id: list(self.depends_on.get(module_id) or [])
                for module_id in node_order
            },
            "dependents": {
                module_id: list(self.dependents.get(module_id) or [])
                for module_id in node_order
            },
            "default_executor_profile": str(self.default_executor_profile or "").strip(),
            "node_executors": {
                module_id: str(self.node_executors.get(module_id) or "").strip()
                for module_id in node_order
                if str(self.node_executors.get(module_id) or "").strip()
            },
        }

    def to_storage_dict(self) -> dict[str, Any]:
        node_order = list(self.node_order)
        return {
            "node_order": node_order,
            "node_kind": {
                node_id: str(self.node_kind.get(node_id) or "module")
                for node_id in node_order
            },
            "depends_on": {
                node_id: list(self.depends_on.get(node_id) or [])
                for node_id in node_order
            },
            "dependents": {
                node_id: list(self.dependents.get(node_id) or [])
                for node_id in node_order
            },
            "default_executor_profile": str(self.default_executor_profile or "").strip(),
            "node_executors": {
                node_id: str(self.node_executors.get(node_id) or "").strip()
                for node_id in node_order
                if str(self.node_executors.get(node_id) or "").strip()
            },
        }


@dataclass(frozen=True)
class DagState:
    """Runtime DAG state. This is the single in-memory shape for DAG transitions."""

    spec: DagSpec
    remaining_indegree: dict[str, int] = field(default_factory=dict)
    node_status: dict[str, str] = field(default_factory=dict)
    ready_nodes: tuple[str, ...] = ()
    running_nodes: dict[str, str] = field(default_factory=dict)
    completed_nodes: tuple[str, ...] = ()
    node_outputs: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_validation(cls, validation: dict[str, Any], existing: dict[str, Any] | None = None) -> DagState:
        existing = dict(existing or {})
        spec = DagSpec.from_validation(validation, existing=existing)
        existing_status = dict(existing.get("module_status") or existing.get("node_status") or {})
        existing_running = dict(existing.get("running_modules") or existing.get("running_nodes") or {})
        existing_outputs = dict(existing.get("module_outputs") or existing.get("node_outputs") or {})
        completed_modules = {
            module_id
            for module_id in _coerce_text_list(existing.get("completed_modules") or existing.get("completed_nodes"))
            if module_id in spec.node_order
        }
        for module_id, status in existing_status.items():
            if module_id in spec.node_order and str(status or "").strip().lower() == "completed":
                completed_modules.add(str(module_id))

        module_status: dict[str, str] = {}
        remaining_indegree: dict[str, int] = {}
        for module_id in spec.node_order:
            raw_status = str(existing_status.get(module_id) or "").strip().lower()
            if module_id in completed_modules:
                status = "completed"
            elif module_id in existing_running:
                status = "running"
            elif raw_status in PRESERVED_STATUSES:
                status = raw_status
            else:
                status = ""
            remaining = _remaining_dependency_count(spec.depends_on, module_id, completed_modules)
            if status in {"", "blocked"}:
                status = "ready" if remaining == 0 else "blocked"
            if status in {"ready", "completed"}:
                remaining = 0
            module_status[module_id] = status
            remaining_indegree[module_id] = int(remaining)

        running_modules = {
            module_id: str(child_id)
            for module_id, child_id in existing_running.items()
            if module_id in spec.node_order and module_status.get(module_id) == "running" and str(child_id or "").strip()
        }
        ready_nodes = tuple(_ready_ids_from_parts(spec.node_order, module_status, remaining_indegree))
        return cls(
            spec=spec,
            remaining_indegree=remaining_indegree,
            node_status=module_status,
            ready_nodes=ready_nodes,
            running_nodes=running_modules,
            completed_nodes=tuple(module_id for module_id in spec.node_order if module_id in completed_modules),
            node_outputs={
                module_id: dict(output)
                for module_id, output in existing_outputs.items()
                if module_id in spec.node_order and isinstance(output, dict)
            },
        )

    @classmethod
    def from_dict(cls, dag: dict[str, Any]) -> DagState:
        source = dict(dag or {})
        spec = DagSpec.from_dict(source)
        remaining_indegree = {
            str(key): max(0, _coerce_int(value) or 0)
            for key, value in dict(source.get("remaining_indegree") or {}).items()
            if str(key or "").strip()
        }
        module_status = {
            str(key): str(value)
            for key, value in dict(source.get("module_status") or source.get("node_status") or {}).items()
            if str(key or "").strip()
        }
        running_modules = {
            str(key): str(value)
            for key, value in dict(source.get("running_modules") or source.get("running_nodes") or {}).items()
            if str(key or "").strip() and str(value or "").strip()
        }
        completed_modules = tuple(
            item for item in _coerce_text_list(source.get("completed_modules") or source.get("completed_nodes")) if item in spec.node_order
        )
        module_outputs = {
            str(key): dict(value)
            for key, value in dict(source.get("module_outputs") or source.get("node_outputs") or {}).items()
            if str(key or "").strip() and isinstance(value, dict)
        }
        ready_nodes = tuple(_ready_ids_from_parts(spec.node_order, module_status, remaining_indegree))
        return cls(
            spec=spec,
            remaining_indegree=remaining_indegree,
            node_status=module_status,
            ready_nodes=ready_nodes,
            running_nodes={module_id: child_id for module_id, child_id in running_modules.items() if module_id in spec.node_order},
            completed_nodes=completed_modules,
            node_outputs={module_id: output for module_id, output in module_outputs.items() if module_id in spec.node_order},
        )

    def to_dict(self) -> dict[str, Any]:
        node_order = list(self.spec.node_order)
        result = self.spec.to_dict()
        result.update(
            {
                "remaining_indegree": {
                    module_id: max(0, _coerce_int(self.remaining_indegree.get(module_id)) or 0)
                    for module_id in node_order
                },
                "module_status": {
                    module_id: str(self.node_status.get(module_id) or "")
                    for module_id in node_order
                    if str(self.node_status.get(module_id) or "").strip()
                },
                "ready_modules": [module_id for module_id in node_order if module_id in set(self.ready_nodes)],
                "running_modules": {
                    module_id: str(child_id)
                    for module_id, child_id in self.running_nodes.items()
                    if module_id in node_order and str(child_id or "").strip()
                },
                "completed_modules": [module_id for module_id in node_order if module_id in set(self.completed_nodes)],
                "module_outputs": {
                    module_id: dict(output)
                    for module_id, output in self.node_outputs.items()
                    if module_id in node_order and isinstance(output, dict)
                },
            }
        )
        return result

    def to_storage_dict(self) -> dict[str, Any]:
        node_order = list(self.spec.node_order)
        result = self.spec.to_storage_dict()
        result.update(
            {
                "remaining_indegree": {
                    node_id: max(0, _coerce_int(self.remaining_indegree.get(node_id)) or 0)
                    for node_id in node_order
                },
                "node_status": {
                    node_id: str(self.node_status.get(node_id) or "")
                    for node_id in node_order
                    if str(self.node_status.get(node_id) or "").strip()
                },
                "ready_nodes": [node_id for node_id in node_order if node_id in set(self.ready_nodes)],
                "running_nodes": {
                    node_id: str(child_id)
                    for node_id, child_id in self.running_nodes.items()
                    if node_id in node_order and str(child_id or "").strip()
                },
                "completed_nodes": [node_id for node_id in node_order if node_id in set(self.completed_nodes)],
                "node_outputs": {
                    node_id: dict(output)
                    for node_id, output in self.node_outputs.items()
                    if node_id in node_order and isinstance(output, dict)
                },
            }
        )
        return result


@dataclass(frozen=True)
class DagAdvanceResult:
    dag: dict[str, Any]
    status: str
    ready_node_ids: tuple[str, ...] = ()
    active_child_work_order_ids: tuple[str, ...] = ()
    completed_node_ids: tuple[str, ...] = ()
    next_node_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dag(cls, dag: dict[str, Any], **extra: Any) -> DagAdvanceResult:
        ready = tuple(ready_module_ids(dag))
        return cls(
            dag=dag,
            status=module_dag_status(dag),
            ready_node_ids=ready,
            active_child_work_order_ids=tuple(_active_child_work_order_ids(dag)),
            completed_node_ids=tuple(_coerce_text_list(dag.get("completed_modules"))),
            next_node_id=ready[0] if ready else "",
            extra=dict(extra),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "dag": self.dag,
            "status": self.status,
            "ready_node_ids": list(self.ready_node_ids),
            "ready_module_ids": list(self.ready_node_ids),
            "next_node_id": self.next_node_id,
            "next_module_id": self.next_node_id,
            "active_child_work_order_ids": list(self.active_child_work_order_ids),
            "completed_node_ids": list(self.completed_node_ids),
            "completed_modules": list(self.completed_node_ids),
        }
        result.update(self.extra)
        return result


def dag_spec_from_validation(validation: dict[str, Any], existing: dict[str, Any] | None = None) -> DagSpec:
    return DagSpec.from_validation(validation, existing=existing)


def dag_state_from_validation(validation: dict[str, Any], existing: dict[str, Any] | None = None) -> DagState:
    return DagState.from_validation(validation, existing=existing)


def dag_state_from_dict(dag: dict[str, Any]) -> DagState:
    return DagState.from_dict(dag)


def dag_state_to_runtime_dict(dag: dict[str, Any]) -> dict[str, Any]:
    return DagState.from_dict(dag).to_dict() if dict(dag or {}) else {}


def dag_state_to_storage_dict(dag: dict[str, Any]) -> dict[str, Any]:
    return DagState.from_dict(dag).to_storage_dict() if dict(dag or {}) else {}


def module_kind_from_validation(validation: dict[str, Any], module_id: str) -> str:
    wanted = str(module_id or "").strip()
    for item in list(dict(validation or {}).get("nodes") or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("module_id") or "").strip() == wanted:
            kind = str(item.get("kind") or "module").strip().lower()
            return kind if kind in {"prelude", "module", "join"} else "module"
    return "module"


def build_module_dag_from_validation(
    validation: dict[str, Any],
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return DagState.from_validation(validation, existing=existing).to_dict()


def module_dag_status(dag: dict[str, Any]) -> str:
    statuses = dict(dag.get("module_status") or {})
    if statuses and all(str(status or "").strip().lower() == "completed" for status in statuses.values()):
        return "completed"
    if dict(dag.get("running_modules") or {}):
        return "running_module"
    if ready_module_ids(dag):
        return "awaiting_continue"
    if any(str(status or "").strip().lower() in {"needs_repair", "stale"} for status in statuses.values()):
        return "awaiting_continue"
    return "blocked"


def ready_module_ids(dag: dict[str, Any]) -> list[str]:
    module_order = _coerce_text_list(dag.get("module_order"))
    module_status = dict(dag.get("module_status") or {})
    remaining_indegree = dict(dag.get("remaining_indegree") or {})
    return _ready_ids_from_parts(module_order, module_status, remaining_indegree)


def claim_ready_modules(
    dag: dict[str, Any],
    module_to_child_id: dict[str, str],
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    updated = _copy_dag(dag)
    module_status = dict(updated.get("module_status") or {})
    running_modules = dict(updated.get("running_modules") or {})
    module_order = _coerce_text_list(updated.get("module_order"))
    ready = ready_module_ids(updated)
    max_claims = len(ready) if limit is None else max(0, _coerce_int(limit) or 0)
    claims: list[dict[str, str]] = []
    for module_id in ready:
        if len(claims) >= max_claims:
            break
        child_id = str(dict(module_to_child_id or {}).get(module_id) or "").strip()
        if not child_id:
            continue
        module_status[module_id] = "running"
        running_modules[module_id] = child_id
        claims.append({"module_id": module_id, "child_work_order_id": child_id})
    updated["module_status"] = module_status
    updated["running_modules"] = {
        module_id: str(child_id)
        for module_id, child_id in running_modules.items()
        if module_id in module_order and str(child_id or "").strip()
    }
    updated["ready_modules"] = ready_module_ids(updated)
    result = _advance_result(updated)
    result.update(
        {
            "claims": claims,
            "claimed_modules": [claim["module_id"] for claim in claims],
            "claimed_child_work_order_ids": [claim["child_work_order_id"] for claim in claims],
        }
    )
    return result


def mark_modules_running(dag: dict[str, Any], module_to_child_id: dict[str, str]) -> dict[str, Any]:
    return claim_ready_modules(dag, module_to_child_id)


def complete_module(
    dag: dict[str, Any],
    module_id: str,
    child_output: dict[str, Any] | None = None,
    *,
    child_work_order_id: str = "",
) -> dict[str, Any]:
    updated = _copy_dag(dag)
    module_order = _coerce_text_list(updated.get("module_order"))
    resolved_module_id = str(module_id or "").strip()
    module_status = dict(updated.get("module_status") or {})
    running_modules = dict(updated.get("running_modules") or {})
    module_outputs = dict(updated.get("module_outputs") or {})
    completed_modules = {
        item for item in _coerce_text_list(updated.get("completed_modules")) if item in module_order
    }
    expected_child_id = str(child_work_order_id or "").strip()
    current_child_id = str(running_modules.get(resolved_module_id) or "").strip()
    if resolved_module_id not in module_order:
        result = _advance_result(updated)
        result.update({"advanced": False, "reason": "unknown_module", "module_id": resolved_module_id})
        return result
    if expected_child_id and current_child_id != expected_child_id:
        result = _advance_result(updated)
        result.update(
            {
                "advanced": False,
                "reason": "stale_child_work_order" if current_child_id else "module_not_running",
                "module_id": resolved_module_id,
                "expected_child_work_order_id": current_child_id,
                "child_work_order_id": expected_child_id,
            }
        )
        return result
    if resolved_module_id in module_order and module_status.get(resolved_module_id) != "completed":
        completed_modules.add(resolved_module_id)
        module_status[resolved_module_id] = "completed"
        running_modules.pop(resolved_module_id, None)
        if child_output:
            module_outputs[resolved_module_id] = dict(child_output)
        completed_set = set(completed_modules)
        remaining_indegree = dict(updated.get("remaining_indegree") or {})
        depends_on = dict(updated.get("depends_on") or {})
        dependents = dict(updated.get("dependents") or {})
        for dependent in _coerce_text_list(dependents.get(resolved_module_id)):
            if str(module_status.get(dependent) or "").strip().lower() == "completed":
                remaining_indegree[dependent] = 0
                continue
            remaining = _remaining_dependency_count(depends_on, dependent, completed_set)
            remaining_indegree[dependent] = int(remaining)
            if remaining == 0 and str(module_status.get(dependent) or "").strip().lower() in {"", "blocked", "stale"}:
                module_status[dependent] = "ready"
        updated["remaining_indegree"] = remaining_indegree
    updated["module_status"] = module_status
    updated["running_modules"] = {
        mid: str(child_id)
        for mid, child_id in running_modules.items()
        if mid in module_order and str(child_id or "").strip()
    }
    updated["completed_modules"] = [item for item in module_order if item in completed_modules]
    updated["module_outputs"] = {
        mid: dict(output)
        for mid, output in module_outputs.items()
        if mid in module_order and isinstance(output, dict)
    }
    updated["ready_modules"] = ready_module_ids(updated)
    result = _advance_result(updated)
    result.update(
        {
            "advanced": str(module_status.get(resolved_module_id) or "").strip().lower() == "completed",
            "reason": "completed" if resolved_module_id in completed_modules else "not_completed",
            "module_id": resolved_module_id,
            "child_work_order_id": expected_child_id or current_child_id,
        }
    )
    return result


def release_running_module(
    dag: dict[str, Any],
    child_work_order_id: str,
    *,
    terminal_failure: bool = False,
) -> dict[str, Any]:
    updated = _copy_dag(dag)
    child_id = str(child_work_order_id or "").strip()
    module_order = _coerce_text_list(updated.get("module_order"))
    module_status = dict(updated.get("module_status") or {})
    running_modules = dict(updated.get("running_modules") or {})
    released_module_id = next(
        (
            str(module_id)
            for module_id, value in running_modules.items()
            if str(value or "").strip() == child_id
        ),
        "",
    )
    if released_module_id:
        running_modules.pop(released_module_id, None)
        if str(module_status.get(released_module_id) or "").strip().lower() == "running":
            module_status[released_module_id] = "blocked" if terminal_failure else "ready"
    updated["module_status"] = module_status
    updated["running_modules"] = {
        mid: str(value)
        for mid, value in running_modules.items()
        if mid in module_order and str(value or "").strip()
    }
    updated["ready_modules"] = ready_module_ids(updated)
    result = _advance_result(updated)
    result["released_module_id"] = released_module_id
    return result


def affected_modules_for_repair(dag: dict[str, Any], replay_targets: list[str]) -> list[str]:
    module_order = _coerce_text_list(dag.get("module_order"))
    dependents = dict(dag.get("dependents") or {})
    affected = set(_coerce_text_list(replay_targets))
    queue = list(affected)
    while queue:
        module_id = queue.pop(0)
        for dependent in _coerce_text_list(dependents.get(module_id)):
            if dependent in affected:
                continue
            affected.add(dependent)
            queue.append(dependent)
    return [module_id for module_id in module_order if module_id in affected]


def apply_repair_replay(
    dag: dict[str, Any],
    replay_targets: list[str],
    *,
    child_work_order_ids: dict[str, str] | None = None,
    replay_attempts: dict[str, int] | None = None,
    completed_modules: list[str] | None = None,
) -> dict[str, Any]:
    updated = _copy_dag(dag)
    module_order = _coerce_text_list(updated.get("module_order"))
    targets = [module_id for module_id in _coerce_text_list(replay_targets) if module_id in module_order]
    affected_modules = affected_modules_for_repair(updated, targets)
    target_set = set(targets)
    affected_set = set(affected_modules)
    child_ids = {
        str(module_id): str(child_id)
        for module_id, child_id in dict(child_work_order_ids or {}).items()
        if str(module_id or "").strip() and str(child_id or "").strip()
    }
    attempts = {
        str(module_id): max(0, _coerce_int(value) or 0)
        for module_id, value in dict(replay_attempts or {}).items()
        if str(module_id or "").strip()
    }
    completed_set = {
        module_id
        for module_id in _coerce_text_list(completed_modules if completed_modules is not None else updated.get("completed_modules"))
        if module_id in module_order and module_id not in affected_set
    }
    module_status = dict(updated.get("module_status") or {})
    running_modules = dict(updated.get("running_modules") or {})
    module_outputs = dict(updated.get("module_outputs") or {})
    invalidated_child_ids: list[str] = []
    for module_id in affected_modules:
        attempts[module_id] = int(attempts.get(module_id) or 0) + 1
        invalidated_child_ids.extend([child_ids.pop(module_id, ""), running_modules.pop(module_id, "")])
        module_outputs.pop(module_id, None)
        module_status[module_id] = "needs_repair" if module_id in target_set else "stale"

    depends_on = dict(updated.get("depends_on") or {})
    remaining_indegree: dict[str, int] = {}
    ready_modules: list[str] = []
    for module_id in module_order:
        remaining = _remaining_dependency_count(depends_on, module_id, completed_set)
        remaining_indegree[module_id] = int(remaining)
        status = str(module_status.get(module_id) or "").strip().lower()
        if status == "completed" and module_id not in completed_set:
            status = "blocked"
            module_status[module_id] = status
        if status in READY_STATUSES and remaining == 0:
            ready_modules.append(module_id)
        elif status in {"needs_repair", "stale"}:
            module_status[module_id] = status
        elif module_id not in completed_set and status not in {"running", "failed", "paused"}:
            module_status[module_id] = "ready" if remaining == 0 else "blocked"
            if remaining == 0:
                ready_modules.append(module_id)

    updated["module_status"] = module_status
    updated["running_modules"] = {
        module_id: str(child_id)
        for module_id, child_id in running_modules.items()
        if module_id in module_order and str(child_id or "").strip()
    }
    updated["completed_modules"] = [module_id for module_id in module_order if module_id in completed_set]
    updated["module_outputs"] = {
        module_id: dict(output)
        for module_id, output in module_outputs.items()
        if module_id in module_order and isinstance(output, dict)
    }
    updated["remaining_indegree"] = remaining_indegree
    updated["ready_modules"] = [module_id for module_id in module_order if module_id in set(ready_modules)]
    result = _advance_result(updated)
    result.update(
        {
            "affected_modules": affected_modules,
            "child_work_order_ids": child_ids,
            "replay_attempts": attempts,
            "invalidated_child_work_order_ids": _dedupe_text(invalidated_child_ids),
        }
    )
    return result


def _advance_result(dag: dict[str, Any]) -> dict[str, Any]:
    return DagAdvanceResult.from_dag(dag).to_dict()


def _active_child_work_order_ids(dag: dict[str, Any]) -> list[str]:
    return [
        str(child_id)
        for child_id in dict(dag.get("running_modules") or {}).values()
        if str(child_id or "").strip()
    ]


def _copy_dag(dag: dict[str, Any]) -> dict[str, Any]:
    return DagState.from_dict(dag).to_dict()


def _ready_ids_from_parts(module_order: Any, module_status: dict[str, Any], remaining_indegree: dict[str, Any]) -> list[str]:
    return [
        module_id
        for module_id in _coerce_text_list(module_order)
        if str(module_status.get(module_id) or "").strip().lower() in READY_STATUSES
        and max(0, _coerce_int(remaining_indegree.get(module_id)) or 0) == 0
    ]


def _remaining_dependency_count(depends_on: dict[str, Any], module_id: str, completed_modules: set[str]) -> int:
    return len(
        [
            dep
            for dep in _coerce_text_list(dict(depends_on or {}).get(module_id))
            if dep not in completed_modules
        ]
    )


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [line.strip(" -\t") for line in value.splitlines() if line.strip(" -\t")]
    return [str(item).strip() for item in list(value or []) if str(item or "").strip()]


def _dedupe_text(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
