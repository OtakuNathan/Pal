from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping
from uuid import uuid4

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from pal.bunshin.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.v2.paths import runtime_spool_root


TASK_LEDGER_ARTIFACT = "TaskLedgerArtifact"
TASK_LEDGER_YAML_ARTIFACT = "TaskLedgerYamlArtifact"

_SAFE_STAMP = re.compile(r"[^0-9A-Za-z._-]+")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TaskRevisionAuthority(_StrictModel):
    title: str = Field(min_length=1)
    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    observed_at: str = Field(min_length=1)
    origin: Literal["architect_user_clarification"]

    @field_validator("title", "question", "answer")
    @classmethod
    def _require_nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("revision authority text must not be blank")
        return value


class TaskRevisionEntry(_StrictModel):
    sequence: int = Field(ge=1)
    authority: TaskRevisionAuthority

    @model_validator(mode="before")
    @classmethod
    def _read_historical_entries(cls, value: Any) -> Any:
        # Earlier ledgers carried an LLM-compiled summary and JSON patch beside
        # the exact communication record. Those derived fields are deliberately
        # ignored when old immutable artifacts are read; authority is the only
        # task-revision truth projected to roles.
        if isinstance(value, Mapping):
            normalized = dict(value)
            normalized.pop("summary", None)
            normalized.pop("changes", None)
            return normalized
        return value


class TaskLedger(_StrictModel):
    schema_version: Literal["1"]
    title: str = Field(min_length=1)
    original: dict[str, Any]
    revisions: list[TaskRevisionEntry]

    @model_validator(mode="after")
    def _validate_history(self) -> TaskLedger:
        if not self.original:
            raise ValueError("original task specification must not be empty")
        expected = list(range(1, len(self.revisions) + 1))
        actual = [item.sequence for item in self.revisions]
        if actual != expected:
            raise ValueError("task revision sequence must be contiguous and ordered")
        return self


@dataclass(frozen=True)
class MaterializedTaskLedger:
    root: Path
    files: tuple[str, ...]


class TaskLedgerService:
    """Persist one immutable, append-only task ledger and project it as task.yaml."""

    def __init__(self, runtime_root: Path, artifacts: ContentAddressedArtifactStore) -> None:
        self.runtime_root = Path(runtime_root)
        self.artifacts = artifacts

    def publish(
        self,
        *,
        title: str,
        task_spec: Mapping[str, Any],
        actor: str,
        source_channel: str,
    ) -> ArtifactRef:
        ledger = TaskLedger.model_validate(
            {
                "schema_version": "1",
                "title": str(title or "Task").strip() or "Task",
                "original": dict(task_spec or {}),
                "revisions": [],
            }
        )
        return self.artifacts.put_json(
            _serialize_task_ledger(ledger),
            artifact_type=TASK_LEDGER_ARTIFACT,
            provenance={"actor": actor, "source_channel": source_channel},
        )

    def append_revision(
        self,
        *,
        base_ref: ArtifactRef | Mapping[str, Any],
        authority: TaskRevisionAuthority | Mapping[str, Any],
        actor: str,
        source_channel: str,
    ) -> ArtifactRef:
        base_artifact = (
            base_ref if isinstance(base_ref, ArtifactRef) else ArtifactRef.from_mapping(base_ref)
        )
        _require_artifact_type(self.artifacts, base_artifact, TASK_LEDGER_ARTIFACT)
        ledger = TaskLedger.model_validate(self.artifacts.read_json(base_artifact))
        authority_value = (
            authority
            if isinstance(authority, TaskRevisionAuthority)
            else TaskRevisionAuthority.model_validate(dict(authority or {}))
        )
        entry = TaskRevisionEntry(
            sequence=len(ledger.revisions) + 1,
            authority=authority_value,
        )
        next_ledger = TaskLedger(
            schema_version="1",
            title=ledger.title,
            original=ledger.original,
            revisions=[*ledger.revisions, entry],
        )
        return self.artifacts.put_json(
            _serialize_task_ledger(next_ledger),
            artifact_type=TASK_LEDGER_ARTIFACT,
            provenance={
                "actor": actor,
                "source_channel": source_channel,
                "base_task_ledger": base_artifact.sha256,
                "revision_sequence": entry.sequence,
                "revision_origin": authority_value.origin,
            },
            child_refs=((base_artifact.sha256, "previous_task_ledger"),),
        )

    def materialize(
        self,
        ledger_ref: ArtifactRef | Mapping[str, Any],
    ) -> MaterializedTaskLedger:
        ref = (
            ledger_ref
            if isinstance(ledger_ref, ArtifactRef)
            else ArtifactRef.from_mapping(ledger_ref)
        )
        _require_artifact_type(self.artifacts, ref, TASK_LEDGER_ARTIFACT)
        ledger = validate_task_ledger(self.artifacts.read_json(ref))
        root = runtime_spool_root(self.runtime_root) / "task-ledgers" / ref.sha256
        complete = root / ".complete"
        if complete.is_file():
            return MaterializedTaskLedger(root=root, files=("task.yaml",))
        temporary = root.with_name(f".{root.name}.{uuid4().hex}.tmp")
        temporary.mkdir(parents=True, exist_ok=False)
        try:
            _write_exclusive(temporary / "task.yaml", render_task_ledger_yaml(ledger))
            _write_exclusive(temporary / ".complete", b"ok\n")
            root.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(temporary, root)
            except OSError:
                if not complete.is_file():
                    raise
        finally:
            if temporary.exists():
                for path in sorted(temporary.rglob("*"), reverse=True):
                    if path.is_file() or path.is_symlink():
                        path.unlink()
                    elif path.is_dir():
                        path.rmdir()
                temporary.rmdir()
        return MaterializedTaskLedger(root=root, files=("task.yaml",))

    def source_attachments(
        self,
        ledger_ref: ArtifactRef | Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        ref = (
            ledger_ref
            if isinstance(ledger_ref, ArtifactRef)
            else ArtifactRef.from_mapping(ledger_ref)
        )
        _require_artifact_type(self.artifacts, ref, TASK_LEDGER_ARTIFACT)
        ledger = validate_task_ledger(self.artifacts.read_json(ref))
        yaml_ref = self.artifacts.put_bytes(
            render_task_ledger_yaml(ledger),
            artifact_type=TASK_LEDGER_YAML_ARTIFACT,
            media_type="application/yaml",
            provenance={"task_ledger_sha256": ref.sha256},
            child_refs=((ref.sha256, "task_ledger"),),
        )
        return [
            {
                "name": "task.yaml",
                "media_type": "application/yaml",
                "artifact_ref": yaml_ref.to_dict(),
            }
        ]

    def materialize_artifact(
        self,
        ref: ArtifactRef,
        *,
        semantic_name: str,
    ) -> Path:
        safe_name = _SAFE_STAMP.sub("_", semantic_name.strip()).strip("._") or "input"
        suffix = _media_suffix(ref.media_type)
        root = runtime_spool_root(self.runtime_root) / "semantic-inputs" / ref.sha256
        target = root / f"{safe_name}{suffix}"
        if target.is_file():
            return target
        root.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        _write_exclusive(temporary, self.artifacts.read_bytes(ref))
        try:
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return target


def validate_task_ledger(value: Any) -> dict[str, Any]:
    try:
        ledger = TaskLedger.model_validate(value)
    except ValidationError as exc:
        raise ValueError(f"invalid TaskLedgerArtifact: {exc}") from exc
    return _serialize_task_ledger(ledger)


def render_task_ledger_yaml(ledger_value: Mapping[str, Any]) -> bytes:
    ledger = validate_task_ledger(ledger_value)
    return yaml.safe_dump(
        ledger,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).encode("utf-8")


def _serialize_task_ledger(ledger: TaskLedger) -> dict[str, Any]:
    return ledger.model_dump(mode="json", exclude_none=False)


def _media_suffix(media_type: str) -> str:
    normalized = str(media_type or "").lower()
    if normalized == "application/json":
        return ".json"
    if "yaml" in normalized:
        return ".yaml"
    if "markdown" in normalized:
        return ".md"
    if "diff" in normalized or "patch" in normalized:
        return ".diff"
    return ".txt"


def _require_artifact_type(
    artifacts: ContentAddressedArtifactStore,
    ref: ArtifactRef,
    expected_type: str,
) -> None:
    record = artifacts.metadata_repository.read_artifact_record(ref.sha256)
    if record is None or str(record.get("artifact_type") or "") != expected_type:
        raise ValueError(f"expected durable {expected_type}")


def _write_exclusive(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
