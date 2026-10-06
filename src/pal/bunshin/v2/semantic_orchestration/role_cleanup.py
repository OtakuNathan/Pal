from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from pal.lsp.ipc import LspManagerClient
from pal.bunshin.v2.worker_processes import WorkerProcesses
from pal.bunshin.v2.background_assignments import BackgroundAssignments


@dataclass
class RoleCleanup:
    processes: WorkerProcesses
    runtime_root: Path
    background: BackgroundAssignments | None = None

    async def release_managed_lsp_workspace(self, workspace: Path) -> Mapping[str, Any]:
        try:
            return await LspManagerClient(
                self.runtime_root,
                request_timeout_seconds=15.0,
            ).release_workspace(workspace)
        except Exception as exc:
            # The LSP manager is optional. The strict process-holder check that
            # follows still protects the snapshot if a server remains alive.
            return {
                "status": "unavailable",
                "error": f"{exc.__class__.__name__}: {exc}",
            }

    async def close_owned_process(
        self,
        invocation_id: str,
    ) -> None:
        await self.processes.close(str(invocation_id))

    async def retire_incarnation(
        self,
        *,
        effect_key: str,
        invocation_id: str,
        lease_resource_key: str,
        fencing_token: int,
        assignment_id: str = "",
        attempt_id: str = "",
    ) -> str:
        """Return the old assignment only after exact setup/task/process closure.

        Business lease release and the durable submit-vs-cancel cut remain the
        caller's responsibility, before acknowledging aggregate/graph STALE.
        """
        if not effect_key or not invocation_id or not lease_resource_key or fencing_token <= 0:
            raise ValueError("retirement requires an immutable effect and business lease identity")
        if self.background is None:
            raise RuntimeError("incarnation cleanup requires background task ownership")
        bound_assignment = await self.background.retire(
            effect_key, lease_identity=(invocation_id, lease_resource_key, fencing_token),
        )
        if assignment_id and bound_assignment and assignment_id != bound_assignment:
            raise RuntimeError("retirement assignment differs from its effect ownership")
        assignment_id = assignment_id or bound_assignment
        await self.processes.close_incarnation(
            effect_key=effect_key,
            invocation_id=invocation_id,
            lease_resource_key=lease_resource_key,
            fencing_token=fencing_token,
            assignment_id=assignment_id,
            attempt_id=attempt_id,
        )
        return assignment_id
