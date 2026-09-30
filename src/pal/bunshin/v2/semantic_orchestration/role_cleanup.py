from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from pal.lsp.ipc import LspManagerClient
from pal.bunshin.v2.worker_processes import WorkerProcesses


@dataclass
class RoleCleanup:
    processes: WorkerProcesses
    runtime_root: Path

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
