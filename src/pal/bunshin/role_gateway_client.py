"""Sandbox client helpers independent of the Manager gateway implementation."""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from pal.bunshin.ipc import ROLE_GATEWAY_TOKEN_ENV, BunshinRoleGatewayClient
from pal.bunshin.artifacts import ArtifactRef


def role_gateway_client_from_env(runtime_root: Path) -> BunshinRoleGatewayClient | None:
    token = str(os.environ.get(ROLE_GATEWAY_TOKEN_ENV) or "").strip()
    if not token:
        return None
    return BunshinRoleGatewayClient(Path(runtime_root), token)


@dataclass
class RoleGatewayArtifactStore:
    client: BunshinRoleGatewayClient
    evidence_scope: Mapping[str, Any] = field(default_factory=dict)

    def read_bytes(self, ref: ArtifactRef | Mapping[str, Any] | str) -> bytes:
        digest = ref.sha256 if isinstance(ref, ArtifactRef) else ref if isinstance(ref, str) else ref.get("sha256")
        response = self.client.request_sync("draft_evidence_read", {
            **self.evidence_scope, "sha256": digest,
        })
        return base64.b64decode(str(response["data_base64"]), validate=True)

    def put_bytes(
        self,
        data: bytes,
        *,
        artifact_type: str,
        schema_version: str = "1",
        media_type: str = "application/octet-stream",
        provenance: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        child_refs: tuple[tuple[str, str], ...] = (),
    ) -> ArtifactRef:
        _ = provenance, metadata, child_refs
        response = self.client.request_sync(
            "artifact_put",
            {
                "data_base64": base64.b64encode(bytes(data)).decode("ascii"),
                "artifact_type": str(artifact_type),
                "schema_version": str(schema_version),
                "media_type": str(media_type),
            },
        )
        return ArtifactRef.from_mapping(dict(response.get("artifact_ref") or {}))
