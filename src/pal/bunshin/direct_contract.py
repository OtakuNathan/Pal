"""Data contracts shared by direct task execution, verification and delivery."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.workspace_paths import repository_path_targets_control_plane


DIRECT_EXECUTION_ARTIFACT = "DirectExecutionArtifact"
DIRECT_MODULE = "repository"


def deliverable_paths(task_spec: Mapping[str, Any]) -> list[str]:
    value = task_spec.get("deliverable_paths", [])
    if not isinstance(value, list):
        raise ValueError("task_spec.deliverable_paths must be a list of repository-relative files")
    paths: list[str] = []
    for raw in value:
        path = PurePosixPath(raw) if isinstance(raw, str) else None
        if (path is None or not raw or raw != path.as_posix() or path.is_absolute()
                or raw in {".", ".."} or ".." in path.parts or "\\" in raw or "\x00" in raw
                or repository_path_targets_control_plane(raw)
                or raw.startswith(("inputs/", "tests/repository/"))
                or raw in {"inputs", "coder_report.json", "producer_report.json"}):
            raise ValueError(f"invalid direct deliverable path: {raw!r}")
        if raw not in paths:
            paths.append(raw)
    return paths


def capture_direct_references(artifacts: ContentAddressedArtifactStore, references: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Snapshot external evidence once; repository-relative inputs use the existing binder."""
    import mimetypes

    captured = {}
    for index, entry in enumerate(references):
        source = Path(str(entry.get("path") or "")).expanduser()
        if not source.is_absolute():
            continue
        if not source.exists() and not entry.get("required", True):
            continue
        if not source.is_file():
            raise ValueError("direct external references must name regular files; include repository directories in the workspace")
        name = f"source_{index}_{entry.get('name') or source.name}"
        ref = artifacts.put_bytes(source.read_bytes(), artifact_type="DirectReferenceArtifact",
            media_type=mimetypes.guess_type(source.name)[0] or "text/plain",
            provenance={"source_path": str(source), "reference_name": str(entry.get("name") or source.name)})
        captured[name] = ref.to_dict()
    return captured


def validate_direct_binding(binding: Mapping[str, Any]) -> None:
    if binding.get("family_id") != "software_engineering":
        raise ValueError("direct mode requires the software_engineering family")
    for role in ("implementation", "verifier"):
        profile = dict(dict(binding.get("role_bindings") or {}).get(role) or {}).get("role_profile") or {}
        if not dict(profile.get("metadata") or {}).get("direct_execution"):
            raise ValueError("pinned profiles do not support direct mode; create a new Task with current profiles")


def report_only_changes(outputs: list[str], changed_paths: list[str]) -> bool:
    # File attachments may also be executable source. Only report formats can
    # waive code-specific obligations or replace patch delivery.
    reports = {path for path in outputs if PurePosixPath(path).suffix.lower() in {".md", ".txt", ".rst", ".log", ".pdf"}}
    return bool(reports) and all(
        not path or path in reports or path.startswith("tests/repository/") for path in changed_paths
    )


def task_requirement_blocker(artifacts: ContentAddressedArtifactStore, finding_ref: ArtifactRef) -> dict[str, Any]:
    import json

    finding = artifacts.read_json(finding_ref)
    question = str(finding.get("summary") or "") or json.dumps(finding.get("findings") or finding, ensure_ascii=False)
    return {"kind": "task_requirement", "question": question, "finding_ref": finding_ref.to_dict()}
