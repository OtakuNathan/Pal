from __future__ import annotations

from peewee import CharField, IntegerField, TextField
from playhouse.sqlite_ext import JSONField

from pal.foundation.persistence import BaseModel, utc_now


class ArtifactRecordModel(BaseModel):
    artifact_id = CharField(primary_key=True)
    scope_key = TextField(index=True)
    turn_id = TextField(index=True)
    kind = CharField(index=True)
    source_channel = CharField(default="")
    file_name = TextField(default="")
    original_path = TextField(default="")
    original_mime_type = TextField(default="")
    original_size_bytes = IntegerField(default=0)
    normalized_path = TextField(default="")
    normalized_mime_type = TextField(default="")
    normalized_size_bytes = IntegerField(default=0)
    summary = TextField(default="")
    status = CharField(default="pending", index=True)
    notes = TextField(default="")
    metadata_blob = JSONField(default=dict)
    created_at = TextField(default=utc_now)
    updated_at = TextField(default=utc_now)

    class Meta:
        table_name = "artifact_records"
        indexes = (
            (("scope_key", "kind"), False),
            (("scope_key", "created_at"), False),
            (("turn_id",), False),
        )


class ArtifactRepresentationModel(BaseModel):
    representation_id = CharField(primary_key=True)
    artifact_id = CharField(index=True)
    representation_kind = CharField(index=True)
    selector_blob = JSONField(default=dict)
    path = TextField(default="")
    mime_type = TextField(default="")
    size_bytes = IntegerField(default=0)
    text_preview = TextField(default="")
    summary = TextField(default="")
    status = CharField(default="ready", index=True)
    metadata_blob = JSONField(default=dict)
    created_at = TextField(default=utc_now)
    updated_at = TextField(default=utc_now)

    class Meta:
        table_name = "artifact_representations"
        indexes = (
            (("artifact_id", "representation_kind"), False),
        )


class ArtifactHotStateModel(BaseModel):
    hot_id = CharField(primary_key=True)
    artifact_id = CharField(index=True)
    scope_key = TextField(index=True)
    last_accessed_at = TextField(default=utc_now)
    expires_at = TextField(index=True)
    hard_expires_at = TextField(index=True)
    access_count = IntegerField(default=0)

    class Meta:
        table_name = "artifact_hot_states"
        indexes = (
            (("scope_key", "expires_at"), False),
            (("scope_key", "last_accessed_at"), False),
        )

