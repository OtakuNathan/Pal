from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping, Protocol

import yaml
from jinja2 import BaseLoader, Environment, StrictUndefined, TemplateError

from pal.minion.v2.workspace_paths import (
    manager_owned_test_corpus_paths,
    repository_path_targets_control_plane,
)


class GraphSatelliteProjectionError(ValueError):
    """A Family-owned Architect-to-GraphIR satellite projection is invalid."""


class WorkspaceAuthorityProjectionError(ValueError):
    """Family property authority metadata cannot form a safe workspace policy."""


@dataclass(frozen=True)
class FamilyNodeProjection:
    """One opaque semantic payload plus common runtime satellite metadata."""

    satellite_data: Mapping[str, Any]
    workspace_policy: Mapping[str, Any]


class FamilyGraphSatelliteProjection(Protocol):
    """Family extension point consumed by the shared Graph compiler."""

    def project(
        self,
        *,
        document: Mapping[str, Any],
        node_name: str,
        node: Mapping[str, Any],
    ) -> FamilyNodeProjection:
        """Project one authored node into opaque GraphIR satellite data."""


@dataclass(frozen=True)
class FamilyGraphSatelliteProjector:
    """Render one Family's opaque execution semantics from ``architect.yaml``.

    The shared Graph compiler owns only the common DAG topology.  A Family owns
    the interpretation of its architecture authoring and supplies the template
    which turns it into durable satellite data for each executable node.  The
    resulting mapping is intentionally opaque to GraphIR, GraphExecution, and
    the execution adapters: they persist and compare it, but never interpret
    its Family-specific fields.
    """

    specialization_id: str
    template: str

    def __post_init__(self) -> None:
        if not self.specialization_id:
            raise ValueError("graph satellite specialization_id is required")
        if not self.template.strip():
            raise ValueError("graph satellite template is required")

    def project(
        self,
        *,
        document: Mapping[str, Any],
        node_name: str,
        node: Mapping[str, Any],
    ) -> FamilyNodeProjection:
        try:
            rendered = _environment().from_string(self.template).render(
                document=copy.deepcopy(dict(document)),
                node_name=str(node_name),
                node=copy.deepcopy(dict(node)),
            )
        except TemplateError as exc:
            raise GraphSatelliteProjectionError(
                "Family graph satellite template failed for "
                f"{self.specialization_id}.{node_name}: {exc}"
            ) from exc
        try:
            value = yaml.safe_load(rendered)
        except yaml.YAMLError as exc:
            raise GraphSatelliteProjectionError(
                "Family graph satellite template rendered invalid YAML for "
                f"{self.specialization_id}.{node_name}: {exc}"
            ) from exc
        if not isinstance(value, Mapping):
            raise GraphSatelliteProjectionError(
                "Family graph satellite template must render an object for "
                f"{self.specialization_id}.{node_name}"
            )
        payload = dict(value)
        satellite_data = payload.get("satellite_data")
        if not isinstance(satellite_data, Mapping) or not satellite_data:
            raise GraphSatelliteProjectionError(
                "Family graph satellite template must render non-empty "
                f"satellite_data for {self.specialization_id}.{node_name}"
            )
        workspace_policy = payload.get("workspace_policy") or {}
        if not isinstance(workspace_policy, Mapping):
            raise GraphSatelliteProjectionError(
                "Family graph satellite workspace_policy must be an object for "
                f"{self.specialization_id}.{node_name}"
            )
        unknown = set(payload) - {"satellite_data", "workspace_policy"}
        if unknown:
            raise GraphSatelliteProjectionError(
                "Family graph satellite template rendered unknown top-level "
                "fields for "
                f"{self.specialization_id}.{node_name}: {', '.join(sorted(unknown))}"
            )
        return FamilyNodeProjection(
            satellite_data=copy.deepcopy(dict(satellite_data)),
            workspace_policy=copy.deepcopy(dict(workspace_policy)),
        )


def apply_workspace_authority_rules(
    *,
    document: Mapping[str, Any],
    projections: Mapping[str, FamilyNodeProjection],
    rules: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
) -> dict[str, FamilyNodeProjection]:
    """Compile Family property metadata into node-local workspace authority.

    The Manager understands only JSON pointers, owner predicates, and path
    scopes.  Property meaning (for example, a build system) remains opaque and
    is supplied by the pinned Family specialization.
    """

    result = {
        str(name): FamilyNodeProjection(
            satellite_data=copy.deepcopy(dict(value.satellite_data)),
            workspace_policy=copy.deepcopy(dict(value.workspace_policy)),
        )
        for name, value in projections.items()
    }
    effective_owners = _effective_write_owners(result)
    reference_paths = {
        _normalize_repo_path(str(path))
        for projection in result.values()
        for path in list(
            dict(projection.workspace_policy).get("reference_only") or []
        )
    }
    frozen_contract_paths = {
        _normalize_repo_path(str(path))
        for projection in result.values()
        if str(
            dict(projection.workspace_policy).get("contract_mode")
            or "file_frozen"
        )
        == "file_frozen"
        for path in list(
            dict(projection.workspace_policy).get("contract_paths") or []
        )
    }
    manager_owned_test_paths = manager_owned_test_corpus_paths(result)
    seen_ids: set[str] = set()
    for raw_rule in rules:
        rule = dict(raw_rule or {})
        rule_id = str(rule.get("id") or "").strip()
        if not rule_id or rule_id in seen_ids:
            raise WorkspaceAuthorityProjectionError(
                "workspace authority ids must be non-empty and unique"
            )
        seen_ids.add(rule_id)
        property_value = _resolve_json_pointer(
            document,
            str(rule.get("property_pointer") or ""),
        )
        owner = _resolve_json_pointer(
            document,
            str(rule.get("owner_pointer") or ""),
        )
        scopes_value = _resolve_json_pointer(
            document,
            str(rule.get("scopes_pointer") or ""),
        )
        owner_collection = _resolve_json_pointer(
            document,
            str(rule.get("owner_collection_pointer") or ""),
        )
        if not isinstance(property_value, Mapping):
            raise WorkspaceAuthorityProjectionError(
                f"workspace authority {rule_id} property must be an object"
            )
        if not isinstance(owner, str) or not owner.strip():
            raise WorkspaceAuthorityProjectionError(
                f"workspace authority {rule_id} owner must be a non-empty string"
            )
        owner = owner.strip()
        if (
            not isinstance(owner_collection, Mapping)
            or owner not in owner_collection
        ):
            raise WorkspaceAuthorityProjectionError(
                f"workspace authority {rule_id} owner {owner!r} is unavailable"
            )
        owner_value = owner_collection[owner]
        if not isinstance(owner_value, Mapping):
            raise WorkspaceAuthorityProjectionError(
                f"workspace authority {rule_id} owner {owner!r} is not an object"
            )
        for field, expected in dict(rule.get("owner_constraints") or {}).items():
            if owner_value.get(str(field)) != expected:
                raise WorkspaceAuthorityProjectionError(
                    f"workspace authority {rule_id} owner {owner!r} violates "
                    f"Family constraint {field}={expected!r}"
                )
        if owner not in result:
            raise WorkspaceAuthorityProjectionError(
                f"workspace authority {rule_id} owner {owner!r} has no executable node"
            )
        if not isinstance(scopes_value, list):
            raise WorkspaceAuthorityProjectionError(
                f"workspace authority {rule_id} scopes must be a list"
            )
        scopes = tuple(
            _normalize_scope(raw, rule_id=rule_id)
            for raw in scopes_value
        )
        if len(set(scopes)) != len(scopes):
            raise WorkspaceAuthorityProjectionError(
                f"workspace authority {rule_id} contains duplicate write scopes"
            )
        for kind, path in scopes:
            if any(
                _repo_paths_overlap(path, reserved)
                for reserved in manager_owned_test_paths
            ):
                raise WorkspaceAuthorityProjectionError(
                    f"workspace authority {rule_id} scope {path} overlaps "
                    "a Manager-owned test corpus"
                )
            if any(
                _repo_paths_overlap(path, reference)
                for reference in reference_paths
            ):
                raise WorkspaceAuthorityProjectionError(
                    f"workspace authority {rule_id} scope {path} "
                    "overlaps a reference-only path"
                )
            if any(
                _repo_paths_overlap(path, frozen)
                for frozen in frozen_contract_paths
            ):
                raise WorkspaceAuthorityProjectionError(
                    f"workspace authority {rule_id} scope {path} "
                    "overlaps a frozen contract"
                )
            for other_owner, other_kind, other_path in effective_owners:
                if not _paths_overlap(kind, path, other_kind, other_path):
                    continue
                if other_owner != owner:
                    raise WorkspaceAuthorityProjectionError(
                        f"workspace authority {rule_id} scope {path} overlaps "
                        f"write authority owned by {other_owner}"
                    )
                raise WorkspaceAuthorityProjectionError(
                    f"workspace authority {rule_id} scope {path} duplicates "
                    f"write authority already owned by {owner}"
                )
            effective_owners.append((owner, kind, path))
        policy = copy.deepcopy(dict(result[owner].workspace_policy))
        implementation = [
            _normalize_scope_dict(raw, rule_id=rule_id)
            for raw in list(policy.get("implementation_scopes") or [])
        ]
        implementation.extend(
            {"kind": kind, "path": path} for kind, path in scopes
        )
        policy["implementation_scopes"] = list(
            {
                (str(item["kind"]), str(item["path"])): item
                for item in implementation
            }.values()
        )
        bindings = [
            copy.deepcopy(dict(item))
            for item in list(policy.get("workspace_authorities") or [])
        ]
        bindings.append(
            {
                "id": rule_id,
                "property": copy.deepcopy(dict(property_value)),
                "write_scopes": [
                    {"kind": kind, "path": path} for kind, path in scopes
                ],
            }
        )
        policy["workspace_authorities"] = bindings
        result[owner] = FamilyNodeProjection(
            satellite_data=result[owner].satellite_data,
            workspace_policy=policy,
        )
    return result


def _resolve_json_pointer(value: Any, pointer: str) -> Any:
    if not pointer.startswith("/"):
        raise WorkspaceAuthorityProjectionError(
            f"workspace authority pointer must be absolute: {pointer!r}"
        )
    current = value
    for encoded in pointer.split("/")[1:]:
        token = encoded.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or token not in current:
            raise WorkspaceAuthorityProjectionError(
                f"workspace authority pointer is unresolved: {pointer}"
            )
        current = current[token]
    return current


def _normalize_repo_path(value: str) -> str:
    text = str(value or "").replace("\\", "/").strip()
    parsed = PurePosixPath(text)
    if (
        not text
        or text.startswith("/")
        or text.endswith("/")
        or "\x00" in text
        or text in {".", ".."}
        or any(part in {"", ".", ".."} for part in parsed.parts)
        or parsed.as_posix() != text
    ):
        raise WorkspaceAuthorityProjectionError(
            f"workspace authority path must be repository-relative: {value!r}"
        )
    if repository_path_targets_control_plane(parsed.as_posix()):
        raise WorkspaceAuthorityProjectionError(
            "workspace authority path targets Manager or VCS control state: "
            f"{value!r}"
        )
    return parsed.as_posix()


def _normalize_scope(raw: Any, *, rule_id: str) -> tuple[str, str]:
    if not isinstance(raw, Mapping):
        raise WorkspaceAuthorityProjectionError(
            f"workspace authority {rule_id} scope must be an object"
        )
    unknown = set(raw) - {"kind", "path"}
    kind = str(raw.get("kind") or "")
    if unknown or kind not in {"file", "directory"}:
        raise WorkspaceAuthorityProjectionError(
            f"workspace authority {rule_id} scope requires only "
            "kind=file|directory and path"
        )
    return kind, _normalize_repo_path(str(raw.get("path") or ""))


def _normalize_scope_dict(raw: Any, *, rule_id: str) -> dict[str, str]:
    kind, path = _normalize_scope(raw, rule_id=rule_id)
    return {"kind": kind, "path": path}


def _effective_write_owners(
    projections: Mapping[str, FamilyNodeProjection],
) -> list[tuple[str, str, str]]:
    result: list[tuple[str, str, str]] = []
    for owner, projection in projections.items():
        policy = dict(projection.workspace_policy)
        if str(policy.get("contract_mode") or "review_guarded") == "review_guarded":
            result.extend(
                (owner, "file", _normalize_repo_path(str(path)))
                for path in list(policy.get("contract_paths") or [])
            )
        result.extend(
            (owner, *_normalize_scope(raw, rule_id="base_workspace_policy"))
            for raw in list(policy.get("implementation_scopes") or [])
        )
    return result


def _paths_overlap(
    left_kind: str,
    left_path: str,
    right_kind: str,
    right_path: str,
) -> bool:
    if left_path == right_path:
        return True
    if left_kind == "directory" and right_path.startswith(left_path + "/"):
        return True
    return right_kind == "directory" and left_path.startswith(right_path + "/")


def _repo_paths_overlap(left_path: str, right_path: str) -> bool:
    return (
        left_path == right_path
        or left_path.startswith(right_path + "/")
        or right_path.startswith(left_path + "/")
    )


def _environment() -> Environment:
    return Environment(
        loader=BaseLoader(),
        autoescape=False,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
