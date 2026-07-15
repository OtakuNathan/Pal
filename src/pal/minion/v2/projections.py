from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pal.minion.v2.paths import plan_revision_root


@dataclass(frozen=True)
class PlanRevisionProjectionStore:
    runtime_root: Path

    def materialize(
        self,
        *,
        workflow_id: str,
        revision_id: str,
        architecture_artifact: Mapping[str, Any],
        markdown: str,
        review: Mapping[str, Any],
        status: str,
    ) -> Path:
        root = self._root(
            workflow_id=workflow_id,
            revision_id=revision_id,
            architecture_artifact=architecture_artifact,
        )
        root.mkdir(parents=True, exist_ok=True)
        _write_text_atomic(root / "plan.md", str(markdown).rstrip() + "\n")
        _write_json_atomic(root / "review.json", dict(review))
        self._write_status(root, architecture_artifact=architecture_artifact, status=status)
        return root

    def update_status(
        self,
        *,
        workflow_id: str,
        revision_id: str,
        architecture_artifact: Mapping[str, Any],
        status: str,
    ) -> Path:
        root = self._root(
            workflow_id=workflow_id,
            revision_id=revision_id,
            architecture_artifact=architecture_artifact,
        )
        root.mkdir(parents=True, exist_ok=True)
        self._write_status(root, architecture_artifact=architecture_artifact, status=status)
        return root

    def _root(
        self,
        *,
        workflow_id: str,
        revision_id: str,
        architecture_artifact: Mapping[str, Any],
    ) -> Path:
        return plan_revision_root(
            self.runtime_root,
            repository_layout=dict(architecture_artifact.get("repository_layout") or {}),
            workflow_id=workflow_id,
            revision_id=revision_id,
        )

    @staticmethod
    def _write_status(
        root: Path,
        *,
        architecture_artifact: Mapping[str, Any],
        status: str,
    ) -> None:
        layout = dict(architecture_artifact.get("repository_layout") or {})
        _write_json_atomic(
            root / "status.json",
            {
                "schema_version": "1",
                "status": str(status),
                "project": str(layout.get("project_name") or "project"),
                "workflow": str(layout.get("workflow_name") or "workflow"),
                "workflow_branch": str(layout.get("workflow_branch") or ""),
                "skeleton_commit": str(architecture_artifact.get("skeleton_commit_sha") or ""),
            },
        )


def _write_text_atomic(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    _write_text_atomic(
        path,
        json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
