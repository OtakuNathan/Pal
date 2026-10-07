from __future__ import annotations
from pal.bunshin.semantic_orchestration.role_inputs import _role_workspace_input_binding_roots
from dataclasses import dataclass
from typing import Any, Mapping
from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.input_binding import (
    INPUT_BINDING_MANIFEST_ARTIFACT, BoundInputError, InputBindingManifest, bound_input_reference_entries,
    materialize_bound_inputs, verify_bound_inputs, verify_repo_bound_inputs,
)
from pal.bunshin.contracts import AggregateSnapshot


@dataclass
class AttemptInputs:
    artifacts: ContentAddressedArtifactStore

    def bind_role_attempt_inputs(
        self,
        *,
        workflow: AggregateSnapshot,
        request: Mapping[str, Any],
        workspace: Mapping[str, Any],
        snapshot: AggregateSnapshot,
        workspace_source_root: str,
    ) -> list[dict[str, Any]]:
        """Bind the workflow's declared inputs into one role attempt workspace.

        Loads the workflow's immutable input-binding manifest from the
        durable ``input_binding_ref`` record, materializes and verifies the
        bound inputs for the resolved workspace (or re-verifies them in
        place for repository-including workspaces), and returns the
        ``bound_input`` reference entries that advertise the deterministic
        locations to the worker.  A missing record means the workflow
        declared no repo-relative inputs and binding is skipped; a present
        but unreadable record, unavailable durable content, or a hash
        mismatch raises :class:`BoundInputError` so the attempt fails
        closed before environment preparation and process spawn.

        The stage is deterministic and idempotent per manifest: retries,
        restarts, and fenced recovery re-execute it against the same
        immutable manifest, so every attempt of one assignment consumes
        byte-identical bound inputs and a stale fenced attempt can only
        rewrite identical bytes, never re-authorize different ones.
        """

        ref_value = workflow.payload.get("input_binding_ref") or request.get(
            "input_binding_ref"
        )
        if not ref_value:
            return []
        if not isinstance(ref_value, Mapping) or not str(
            ref_value.get("sha256") or ""
        ).strip():
            raise BoundInputError(
                "workflow input_binding_ref is not a readable artifact reference"
            )
        if str(ref_value.get("artifact_type") or "") != INPUT_BINDING_MANIFEST_ARTIFACT:
            raise BoundInputError(
                "workflow input_binding_ref does not reference an InputBindingManifestArtifact"
            )
        try:
            manifest = InputBindingManifest.from_payload(
                self.artifacts.read_json(ref_value)
            )
        except BoundInputError:
            raise
        except Exception as exc:
            raise BoundInputError(
                f"workflow input-binding manifest is unreadable: {exc}"
            ) from exc
        if manifest.workflow_id != workflow.workflow_id:
            raise BoundInputError(
                "workflow input-binding manifest belongs to a different workflow: "
                f"expected {workflow.workflow_id!r}, got {manifest.workflow_id!r}"
            )
        repo_root, workspace_root = _role_workspace_input_binding_roots(
            workspace,
            snapshot=snapshot,
            workspace_source_root=workspace_source_root,
        )
        if repo_root is not None:
            verify_repo_bound_inputs(manifest=manifest, repo_root=repo_root)
            return bound_input_reference_entries(
                manifest=manifest,
                repo_root=repo_root,
            )
        materialize_bound_inputs(
            manifest=manifest,
            artifacts=self.artifacts,
            destination_root=workspace_root,
        )
        verify_bound_inputs(
            manifest=manifest,
            destination_root=workspace_root,
        )
        return bound_input_reference_entries(
            manifest=manifest,
            workspace_root=workspace_root,
        )
