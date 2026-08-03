from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

import yaml
from jinja2 import BaseLoader, Environment, StrictUndefined, TemplateError


class GraphSatelliteProjectionError(ValueError):
    """A Family-owned Architect-to-GraphIR satellite projection is invalid."""


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


def _environment() -> Environment:
    return Environment(
        loader=BaseLoader(),
        autoescape=False,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
