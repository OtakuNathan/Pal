from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
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

from pal.minion.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.minion.v2.paths import runtime_spool_root


TASK_LEDGER_ARTIFACT = "TaskLedgerArtifact"
TASK_REVISION_AUTHORITY_ARTIFACT = "TaskRevisionAuthorityArtifact"
TASK_LEDGER_YAML_ARTIFACT = "TaskLedgerYamlArtifact"

_SAFE_STAMP = re.compile(r"[^0-9A-Za-z._-]+")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TaskRevisionAuthority(_StrictModel):
    title: str = Field(min_length=1)
    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    observed_at: str = Field(min_length=1)
    origin: Literal["architect_user_clarification", "human_review_edit"]

    @field_validator("title", "question", "answer")
    @classmethod
    def _require_nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("revision authority text must not be blank")
        return value


class TaskRevisionChange(_StrictModel):
    op: Literal["add", "replace", "remove"]
    path: str = Field(min_length=1)
    value: Any = None

    @model_validator(mode="after")
    def _validate_value_presence(self) -> TaskRevisionChange:
        supplied = "value" in self.model_fields_set
        if self.op in {"add", "replace"} and not supplied:
            raise ValueError(f"{self.op} requires value")
        if self.op == "remove" and supplied:
            raise ValueError("remove must not contain value")
        if not self.path.startswith("/") or self.path == "/":
            raise ValueError("path must be a non-root JSON Pointer")
        _json_pointer_tokens(self.path)
        return self


class TaskRevisionDraft(_StrictModel):
    schema_version: Literal["1"]
    summary: str = Field(min_length=1)
    changes: list[TaskRevisionChange] = Field(min_length=1)


class TaskRevisionEntry(_StrictModel):
    sequence: int = Field(ge=1)
    authority: TaskRevisionAuthority
    summary: str = Field(min_length=1)
    changes: list[TaskRevisionChange] = Field(min_length=1)


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
        effective: Any = copy.deepcopy(self.original)
        for revision in self.revisions:
            for change in revision.changes:
                effective = _apply_change(effective, change)
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

    def publish_authority(
        self,
        *,
        title: str,
        question: str,
        answer: str,
        origin: Literal["architect_user_clarification", "human_review_edit"],
        actor: str,
        source_channel: str,
        observed_at: str | None = None,
    ) -> ArtifactRef:
        authority = TaskRevisionAuthority.model_validate(
            {
                "title": str(title).strip(),
                "question": str(question),
                "answer": str(answer),
                "observed_at": observed_at or datetime.now(UTC).isoformat(),
                "origin": origin,
            }
        )
        return self.artifacts.put_json(
            authority.model_dump(mode="json"),
            artifact_type=TASK_REVISION_AUTHORITY_ARTIFACT,
            provenance={
                "actor": actor,
                "source_channel": source_channel,
                "origin": origin,
                "observed_at": authority.observed_at,
            },
        )

    def append_revision(
        self,
        *,
        base_ref: ArtifactRef | Mapping[str, Any],
        authority_ref: ArtifactRef | Mapping[str, Any],
        revision: Mapping[str, Any],
        actor: str,
        source_channel: str,
    ) -> ArtifactRef:
        base_artifact = (
            base_ref if isinstance(base_ref, ArtifactRef) else ArtifactRef.from_mapping(base_ref)
        )
        authority_artifact = (
            authority_ref
            if isinstance(authority_ref, ArtifactRef)
            else ArtifactRef.from_mapping(authority_ref)
        )
        _require_artifact_type(self.artifacts, base_artifact, TASK_LEDGER_ARTIFACT)
        _require_artifact_type(
            self.artifacts,
            authority_artifact,
            TASK_REVISION_AUTHORITY_ARTIFACT,
        )
        ledger = TaskLedger.model_validate(self.artifacts.read_json(base_artifact))
        authority = TaskRevisionAuthority.model_validate(
            self.artifacts.read_json(authority_artifact)
        )
        draft = TaskRevisionDraft.model_validate(dict(revision or {}))
        entry = TaskRevisionEntry(
            sequence=len(ledger.revisions) + 1,
            authority=authority,
            summary=draft.summary,
            changes=draft.changes,
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
            },
            child_refs=(
                (base_artifact.sha256, "previous_task_ledger"),
                (authority_artifact.sha256, "revision_authority"),
            ),
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


def validate_task_revision_draft(value: Any) -> dict[str, Any]:
    try:
        draft = TaskRevisionDraft.model_validate(value)
    except ValidationError as exc:
        raise ValueError(f"invalid task revision YAML: {exc}") from exc
    return _serialize_task_revision_draft(draft)


def effective_task(ledger_value: Mapping[str, Any]) -> dict[str, Any]:
    ledger = TaskLedger.model_validate(ledger_value)
    effective: Any = copy.deepcopy(ledger.original)
    for revision in ledger.revisions:
        for change in revision.changes:
            effective = _apply_change(effective, change)
    if not isinstance(effective, dict):
        raise ValueError("effective task must remain an object")
    return effective


def render_task_ledger_yaml(ledger_value: Mapping[str, Any]) -> bytes:
    ledger = validate_task_ledger(ledger_value)
    return yaml.safe_dump(
        ledger,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).encode("utf-8")


def load_task_revision_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read task revision YAML: {exc}") from exc
    return validate_task_revision_draft(value)


def task_revision_template_yaml() -> str:
    return (
        "# JSON Pointer paths start inside task.yaml.original; never prefix /original.\n"
        "schema_version: '1'\n"
        "summary: replace with the smallest semantic consequence of the user answer\n"
        "changes:\n"
        "  - op: replace\n"
        "    path: /replace/with/exact/task/path\n"
        "    value: replace with the corrected value\n"
    )


def _serialize_task_ledger(ledger: TaskLedger) -> dict[str, Any]:
    payload = ledger.model_dump(mode="json", exclude_none=False)
    for revision in payload["revisions"]:
        for change in revision["changes"]:
            if change["op"] == "remove":
                change.pop("value", None)
    return payload


def _serialize_task_revision_draft(draft: TaskRevisionDraft) -> dict[str, Any]:
    payload = draft.model_dump(mode="json", exclude_none=False)
    for change in payload["changes"]:
        if change["op"] == "remove":
            change.pop("value", None)
    return payload


def _json_pointer_tokens(path: str) -> list[str]:
    if not path.startswith("/"):
        raise ValueError("JSON Pointer must start with /")
    tokens: list[str] = []
    for raw in path[1:].split("/"):
        index = 0
        while index < len(raw):
            if raw[index] == "~":
                if index + 1 >= len(raw) or raw[index + 1] not in {"0", "1"}:
                    raise ValueError(f"invalid JSON Pointer escape in {path!r}")
                index += 2
            else:
                index += 1
        tokens.append(raw.replace("~1", "/").replace("~0", "~"))
    return tokens


def _apply_change(document: Any, change: TaskRevisionChange) -> Any:
    result = copy.deepcopy(document)
    tokens = _json_pointer_tokens(change.path)
    parent: Any = result
    for token in tokens[:-1]:
        if isinstance(parent, dict):
            if token not in parent:
                raise ValueError(f"task revision path does not exist: {change.path}")
            parent = parent[token]
        elif isinstance(parent, list):
            index = _list_index(token, len(parent), allow_end=False)
            parent = parent[index]
        else:
            raise ValueError(f"task revision path crosses a scalar: {change.path}")
    leaf = tokens[-1]
    if isinstance(parent, dict):
        exists = leaf in parent
        if change.op == "add":
            if exists:
                raise ValueError(f"task revision add path already exists: {change.path}")
            parent[leaf] = copy.deepcopy(change.value)
        elif change.op == "replace":
            if not exists:
                raise ValueError(f"task revision replace path does not exist: {change.path}")
            parent[leaf] = copy.deepcopy(change.value)
        else:
            if not exists:
                raise ValueError(f"task revision remove path does not exist: {change.path}")
            del parent[leaf]
    elif isinstance(parent, list):
        if change.op == "add":
            index = len(parent) if leaf == "-" else _list_index(leaf, len(parent), allow_end=True)
            parent.insert(index, copy.deepcopy(change.value))
        else:
            index = _list_index(leaf, len(parent), allow_end=False)
            if change.op == "replace":
                parent[index] = copy.deepcopy(change.value)
            else:
                del parent[index]
    else:
        raise ValueError(f"task revision path parent is a scalar: {change.path}")
    return result


def _list_index(token: str, length: int, *, allow_end: bool) -> int:
    if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
        raise ValueError(f"invalid task revision array index: {token!r}")
    index = int(token)
    maximum = length if allow_end else length - 1
    if index < 0 or index > maximum:
        raise ValueError(f"task revision array index is out of range: {token!r}")
    return index


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
