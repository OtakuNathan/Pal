from __future__ import annotations
import contextlib
import hashlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from pal.bunshin.scoped_execution import _path_is_relative_to
from pal.bunshin.workspace_tools import _append_unique_artifact
from pal.shared import BunshinInvocationPack


@dataclass
class Artifacts:
    pack: BunshinInvocationPack
    produced_artifacts: list[dict[str, Any]] = field(default_factory=list)

    def finalize_produced_artifacts(self) -> None:
        if not self.produced_artifacts:
            return
        staged_present = any(bool(item.get("staged")) or str(item.get("stage_path") or "").strip() for item in self.produced_artifacts)
        staging_enabled = bool(str((self.pack.workspace or {}).get("artifact_stage_dir") or "").strip() or staged_present)
        if not staging_enabled:
            return
        accepted_indexes = self.accepted_artifact_indexes()
        next_artifacts: list[dict[str, Any]] = []
        for index, artifact in enumerate(list(self.produced_artifacts)):
            if index in accepted_indexes:
                next_artifacts.append(self.promote_produced_artifact(artifact))
            else:
                self.delete_registered_artifact_paths(artifact)
        self.produced_artifacts[:] = next_artifacts
        self.cleanup_artifact_stage_dir()

    def accepted_artifact_indexes(self) -> set[int]:
        primary_indexes = [
            index
            for index, artifact in enumerate(self.produced_artifacts)
            if str(artifact.get("role") or "").strip().lower() == "primary"
        ]
        if not primary_indexes:
            return set(range(len(self.produced_artifacts)))
        latest_primary = primary_indexes[-1]
        return {
            index
            for index, artifact in enumerate(self.produced_artifacts)
            if index == latest_primary or str(artifact.get("role") or "").strip().lower() != "primary"
        }

    def promote_produced_artifact(self, artifact: dict[str, Any]) -> dict[str, Any]:
        promoted = dict(artifact)
        source_text = str(promoted.get("stage_path") or promoted.get("path") or "").strip()
        relative_path = str(promoted.get("relative_path") or "").strip()
        if str(promoted.get("role") or "").strip().lower() == "primary":
            relative_path = str(promoted.get("requested_relative_path") or relative_path).strip()
        final_root_text = str(promoted.get("final_artifact_dir") or (self.pack.workspace or {}).get("artifact_dir") or "").strip()
        if source_text and relative_path and final_root_text:
            source = Path(source_text).expanduser()
            final_root = Path(final_root_text).expanduser().resolve()
            destination = (final_root / relative_path).resolve()
            if destination != final_root and _path_is_relative_to(destination, final_root):
                if source.exists():
                    final_root.mkdir(parents=True, exist_ok=True)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        same_file = source.resolve() == destination.resolve()
                    except OSError:
                        same_file = False
                    if not same_file:
                        shutil.copy2(source, destination)
                if destination.exists() and destination.is_file():
                    promoted = self.refresh_artifact_file_metadata(promoted, destination)
        for key in ("staged", "stage_path", "final_artifact_dir", "requested_relative_path"):
            promoted.pop(key, None)
        return promoted

    def refresh_artifact_file_metadata(self, artifact: dict[str, Any], path: Path) -> dict[str, Any]:
        refreshed = dict(artifact)
        data = path.read_bytes()
        refreshed["path"] = str(path)
        refreshed["size_bytes"] = path.stat().st_size
        refreshed["sha256"] = hashlib.sha256(data).hexdigest()
        final_root_text = str(refreshed.get("final_artifact_dir") or (self.pack.workspace or {}).get("artifact_dir") or "").strip()
        if final_root_text:
            final_root = Path(final_root_text).expanduser().resolve()
            with contextlib.suppress(ValueError):
                refreshed["relative_path"] = str(path.resolve().relative_to(final_root)).replace("\\", "/")
        return refreshed

    def delete_registered_artifact_paths(self, artifact: dict[str, Any]) -> None:
        cleanup_roots = self.artifact_cleanup_roots(artifact)
        if not cleanup_roots:
            return
        seen: set[Path] = set()
        for raw_path in (artifact.get("stage_path"), artifact.get("path")):
            text = str(raw_path or "").strip()
            if not text:
                continue
            path = Path(text).expanduser()
            with contextlib.suppress(OSError):
                path = path.resolve()
            if path in seen:
                continue
            seen.add(path)
            root = next((item for item in cleanup_roots if path != item and _path_is_relative_to(path, item)), None)
            if root is None or not path.exists():
                continue
            with contextlib.suppress(OSError):
                if path.is_file() or path.is_symlink():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)
            self.prune_empty_artifact_dirs(path.parent, stop=root)

    def artifact_cleanup_roots(self, artifact: dict[str, Any]) -> list[Path]:
        roots: list[Path] = []
        for raw_root in (
            (self.pack.workspace or {}).get("artifact_stage_dir"),
            artifact.get("final_artifact_dir"),
            (self.pack.workspace or {}).get("artifact_dir"),
        ):
            text = str(raw_root or "").strip()
            if not text:
                continue
            root = Path(text).expanduser()
            with contextlib.suppress(OSError):
                root = root.resolve()
            if root not in roots:
                roots.append(root)
        return roots

    def cleanup_artifact_stage_dir(self) -> None:
        stage_dir = str((self.pack.workspace or {}).get("artifact_stage_dir") or "").strip()
        if not stage_dir:
            return
        root = Path(stage_dir).expanduser()
        with contextlib.suppress(OSError):
            root = root.resolve()
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)

    def prune_empty_artifact_dirs(self, path: Path, *, stop: Path) -> None:
        current = path
        while current != stop and _path_is_relative_to(current, stop):
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent

    def artifact_payload(self) -> dict[str, Any]:
        self.finalize_produced_artifacts()
        artifacts = [dict(item) for item in self.produced_artifacts]
        payload: dict[str, Any] = {"artifacts": artifacts}
        primary = next((item for item in artifacts if str(item.get("role") or "") == "primary"), None)
        if primary is None and artifacts:
            primary = artifacts[0]
        if primary is not None:
            payload["primary_artifact"] = dict(primary)
        return payload

    def record_produced_artifact(self, artifact: dict[str, Any]) -> None:
        path = str(artifact.get("path") or "").strip()
        relative_path = str(artifact.get("relative_path") or "").strip()
        if not path and not relative_path:
            return
        for existing in self.produced_artifacts:
            if path and str(existing.get("path") or "") == path:
                return
            if relative_path and str(existing.get("relative_path") or "") == relative_path:
                return
        _append_unique_artifact(self.produced_artifacts, artifact)

    def restore(self, artifacts: list[dict[str, Any]]) -> None:
        self.produced_artifacts[:] = [dict(item) for item in artifacts]
