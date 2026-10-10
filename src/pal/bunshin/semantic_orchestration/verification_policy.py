from __future__ import annotations
from dataclasses import field
from typing import Any, Mapping
from pal.bunshin.contracts import AggregateSnapshot, AggregateType
from pal.bunshin.candidate_builder import validate_candidate_submission
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.verification import UnknownPolicy, VerificationCaseResult, VerificationCaseSpec, VerificationStatus, historical_repair_checklist_items
from pal.bunshin.role_protocol import stable_hash
from pal.bunshin.verification_lsp_policy import lsp_policy_errors


def _validate_skeleton_coder_report(
    value: Mapping[str, Any],
    *,
    expected_module: str,
    work_view: Mapping[str, Any],
) -> None:
    bound_view = dict(work_view)
    bound_module = str(bound_view.get("module_name") or expected_module or "").strip()
    if bound_module != str(expected_module or "").strip():
        raise ValueError(
            f"Coder work view module {bound_module!r} does not match expected module {expected_module!r}"
        )
    bound_view["module_name"] = bound_module
    validate_candidate_submission(value, work_view=bound_view)


def _reject_manager_identity_fields(value: Any, *, owner: str, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key)
            lowered = key.casefold()
            if (
                lowered.endswith("_id")
                or lowered.endswith("_ref")
                or lowered.endswith("_sha")
                or "sha256" in lowered
                or lowered in {"handle", "json_pointer", "artifact"}
            ):
                raise ValueError(f"{owner} contains Manager-owned identity field at {path}.{key}")
            _reject_manager_identity_fields(item, owner=owner, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_manager_identity_fields(item, owner=owner, path=f"{path}[{index}]")


def _resolve_dependency_node_id(
    repository: BunshinRepository,
    node: AggregateSnapshot,
    *,
    dependency_module: str,
) -> str:
    name = str(dependency_module or "").strip()
    if not name:
        raise ValueError("verification defect route requires a target module")
    current_name = str(
        node.payload.get("module_name") or node.payload.get("unit_id") or ""
    ).strip()
    # A verifier normally reports a defect in the module it is reviewing.  A
    # repair target may also be a direct dependency, but requiring every
    # finding to name a dependency makes a self-owned finding fail only after
    # the verifier has already completed and submitted it.
    if current_name == name:
        return node.aggregate_id
    matches: list[str] = []
    for dependency in _verification_related_module_nodes(repository, node):
        module_name = str(dependency.payload.get("module_name") or dependency.payload.get("unit_id") or "")
        if module_name == name:
            matches.append(dependency.aggregate_id)
    if len(matches) != 1:
        raise ValueError(f"dependency_module {name!r} does not name exactly one direct dependency")
    return matches[0]


def _verification_repair_path_owners(
    repository: BunshinRepository,
    node: AggregateSnapshot,
) -> dict[str, list[dict[str, str]]]:
    """Compile immutable module path ownership for Manager-routed repairs."""

    owners: dict[str, list[dict[str, str]]] = {}
    for dependency in _verification_related_module_nodes(
        repository,
        node,
        include_current=True,
    ):
        module_name = str(
            dependency.payload.get("module_name")
            or dependency.payload.get("unit_id")
            or ""
        ).strip()
        if not module_name:
            continue
        policy = dict(dependency.payload.get("path_policy") or {})
        scopes: list[dict[str, str]] = [
            {
                "kind": "file",
                "path": str(path).replace("\\", "/").strip("/"),
            }
            for path in list(policy.get("contract_paths") or [])
            if str(path).strip()
        ]
        scopes.extend(
            {
                "kind": str(dict(raw or {}).get("kind") or ""),
                "path": str(dict(raw or {}).get("path") or "")
                .replace("\\", "/")
                .strip("/"),
            }
            for raw in list(policy.get("implementation_scopes") or [])
        )
        for field in ("developer_tests",):
            raw_scope = dict(policy.get(field) or {})
            if raw_scope:
                scopes.append(
                    {
                        "kind": str(raw_scope.get("kind") or ""),
                        "path": str(raw_scope.get("path") or "")
                        .replace("\\", "/")
                        .strip("/"),
                    }
                )
        normalized = [
            scope
            for scope in scopes
            if scope["kind"] in {"file", "directory", "repository"} and scope["path"]
        ]
        if normalized:
            owners[module_name] = list(
                {
                    (scope["kind"], scope["path"]): scope
                    for scope in normalized
                }.values()
            )
    return dict(sorted(owners.items()))


def _verification_repair_scope(
    repository: BunshinRepository,
    node: AggregateSnapshot,
) -> dict[str, Any]:
    """Separate visible contracts from immutable products bound to this check.

    A provider's *current* ACCEPTED state does not prove that its product was
    assembled into this verifier's candidate. Only the recorded baseline does.
    """

    current = str(node.payload.get("module_name") or node.payload.get("unit_id") or "")
    owners = _verification_repair_path_owners(repository, node)
    execution = repository.cycles.read_graph_execution(workflow_id=node.workflow_id)
    checker_providers = (
        set(execution.graph.checker_predecessors(current))
        if execution is not None and current in execution.graph.nodes
        else None
    )
    declared_ids = set(str(item) for item in node.payload.get("dependency_node_ids") or [])
    outputs = dict(node.payload.get("dependency_outputs") or {})
    bound: set[str] = set()
    for provider in _verification_related_module_nodes(repository, node):
        name = str(provider.payload.get("module_name") or provider.payload.get("unit_id") or "")
        product = dict(outputs.get(provider.aggregate_id) or {})
        if (
            provider.aggregate_id in declared_ids
            and (checker_providers is None or name in checker_providers)
            and str(product.get("candidate_digest") or "")
            and dict(product.get("candidate_ref") or {}).get("sha256")
        ):
            bound.add(name)
    return {
        "module_name": current,
        "graph_sink": bool(node.payload.get("graph_sink")),
        "repair_path_owners": owners,
        "dependency_modules": sorted(bound),
        "contract_only_modules": sorted(set(owners) - bound - {current}),
    }


def _verification_related_module_nodes(
    repository: BunshinRepository,
    node: AggregateSnapshot,
    *,
    include_current: bool = False,
) -> tuple[AggregateSnapshot, ...]:
    """Return the semantic module closure without exposing or rewriting DAG edges."""

    pending = [
        str(item)
        for item in (
            *list(node.payload.get("dependency_node_ids") or []),
            *list(node.payload.get("contract_dependency_node_ids") or []),
        )
        if str(item)
    ]
    if include_current:
        pending.insert(0, node.aggregate_id)
    visited: set[str] = set()
    modules: list[AggregateSnapshot] = []
    while pending:
        node_id = pending.pop(0)
        if node_id in visited:
            continue
        visited.add(node_id)
        dependency = (
            node
            if node_id == node.aggregate_id
            else repository.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
        )
        if dependency is None:
            continue
        if str(dependency.payload.get("node_kind") or "unit") == "unit":
            modules.append(dependency)
        pending.extend(
            str(item)
            for item in (
                *list(dependency.payload.get("dependency_node_ids") or []),
                *list(dependency.payload.get("contract_dependency_node_ids") or []),
            )
            if str(item) and str(item) not in visited
        )
    return tuple(modules)


def _manager_routed_findings(payload: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Return one deduplicated semantic finding stream from Manager artifacts.

    Replan batches contain both a flattened ``findings`` projection and grouped
    source packets.  Other repair artifacts expose only one of those shapes.
    The worker must see one required WorkItem per semantic finding regardless
    of the Manager-side storage layout.
    """

    candidates: list[Mapping[str, Any]] = [
        item
        for item in list(payload.get("findings") or [])
        if isinstance(item, Mapping)
    ]
    if not candidates:
        candidates.extend(
            item
            for group in list(payload.get("finding_groups") or [])
            if isinstance(group, Mapping)
            for item in list(group.get("findings") or [])
            if isinstance(item, Mapping)
        )
    if not candidates and any(
        key in payload
        for key in ("summary", "finding_kind", "severity", "priority")
    ):
        candidates.append(payload)

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in candidates:
        finding = dict(raw)
        identity = str(
            finding.get("finding_id")
            or finding.get("finding_key")
            or stable_hash(finding)[:16]
        )
        if identity in seen:
            continue
        seen.add(identity)
        result.append(finding)
    return tuple(result)


def _manager_required_system_scenario_work_items(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Compile authored delivery scenarios into required sink-verifier work.

    Scenario semantics remain Family-owned data. The Manager only preserves
    their stable semantic names as checklist obligations so a sink verifier
    cannot exercise one representative entrypoint and silently omit another
    authored system scenario.
    """

    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, Mapping):
        return ()
    return tuple(
        {
            "kind": "task",
            "summary": f"verify system scenario: {name}",
            "status": "pending",
            "origin": "manager_system_scenario",
            "required": True,
        }
        for raw_name in scenarios
        for name in (str(raw_name).strip(),)
        if name
    )


def _validate_verification_policy(
    plan: Mapping[str, Any],
    cases: list[VerificationCaseSpec],
    policy: Mapping[str, Any],
    node: AggregateSnapshot,
    *,
    work_view: Mapping[str, Any],
) -> None:
    tags = {
        str(tag)
        for item in list(plan.get("recorded_results") or [])
        for tag in list(dict(item or {}).get("obligation_tags") or [])
    }
    exceptions = dict(plan.get("policy_exceptions") or {})
    obligations = (
        ("require_focused_tests", "focused_tests"),
        ("require_warning_clean", "warning_clean"),
        ("require_consumer_probe", "consumer_probe"),
        ("require_public_surface_dogfood", "public_surface_dogfood"),
        ("require_platform_probe", "platform_probe"),
        ("require_candidate_delta_review", "candidate_delta_review"),
    )
    for policy_key, obligation_tag in obligations:
        if not bool(policy.get(policy_key, False)) or obligation_tag in tags:
            continue
        if not str(exceptions.get(obligation_tag) or "").strip():
            raise ValueError(f"VerificationPolicy requires {obligation_tag} evidence or a concrete UNKNOWN reason")
    if (
        bool(policy.get("require_historical_regressions", False))
        and node.payload.get("historical_repair_bill_refs")
        and "historical_regressions" not in tags
    ):
        raise ValueError("VerificationPolicy requires historical RepairBill regressions first")
    required_historical = historical_repair_checklist_items(work_view)
    if required_historical:
        historical_status = {
            str(item.get("name") or ""): str(item.get("status") or "")
            for item in list(plan.get("recorded_results") or [])
            if str(dict(item or {}).get("case_kind") or "") == "historical_regression"
        }
        missing = [
            str(item["case"])
            for item in required_historical
            if str(item["case"]) not in historical_status
        ]
        if missing:
            raise ValueError(
                "verification must replay every historical RepairBill case before submit: "
                + ", ".join(missing)
            )
    lsp_errors = lsp_policy_errors(policy, list(plan.get("recorded_results") or []), exceptions)
    if lsp_errors:
        raise ValueError(lsp_errors[0])
    allowed_obligations = {
        str(item) for item in list(policy.get("allowed_obligations") or []) if str(item)
    }
    unexpected = tags - allowed_obligations if allowed_obligations else set()
    if unexpected:
        raise ValueError(
            "verification evidence exceeds this node's declared scope: "
            + ", ".join(sorted(unexpected))
        )


def _routable_verification_findings(
    findings: list[Mapping[str, Any]],
    case_results: list[VerificationCaseResult],
    *,
    status: VerificationStatus,
) -> list[dict[str, Any]]:
    if status not in {VerificationStatus.FAIL, VerificationStatus.UNKNOWN}:
        return []
    case_ids = {
        item.case_id
        for item in case_results
        if item.status == status
    }
    return [
        dict(item)
        for item in findings
        if str(item.get("case_id") or "") in case_ids
    ]


def _manager_unknown_policy(node: AggregateSnapshot) -> UnknownPolicy:
    raw = dict(node.payload.get("unknown_policy") or {})
    return UnknownPolicy(
        architecture_allows_platform_unknown=bool(raw.get("architecture_allows_platform_unknown")),
        assumption_ref=dict(raw.get("assumption_ref") or {}) or None,
        hard_or_core_semantics=bool(raw.get("hard_or_core_semantics", True)),
        human_waiver_ref=dict(raw.get("human_waiver_ref") or {}) or None,
    )
