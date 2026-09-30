"""Read workflow inputs through the artifact authority supplied at composition."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pal.bunshin.v2.artifacts import ContentAddressedArtifactStore
from pal.bunshin.v2.contracts import AggregateSnapshot
from pal.bunshin.v2.workflow_request_values import _normalize_references, _normalize_skill_refs, _normalize_workspace


@dataclass
class WorkflowRequests:
    artifacts: ContentAddressedArtifactStore

    def read(self, workflow: AggregateSnapshot) -> dict[str, Any]:
        request_ref = dict(workflow.payload.get('request_ref') or {})
        if not request_ref:
            raise ValueError('workflow has no request artifact')
        request = dict(self.artifacts.read_json(request_ref))
        request['workspace'] = _normalize_workspace(request.get('workspace'))
        request['references'] = _normalize_references(request.get('references'))
        request['skill_refs'] = _normalize_skill_refs(request.get('skill_refs'))
        return request
