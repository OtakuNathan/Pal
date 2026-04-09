from __future__ import annotations

from peewee import BlobField, CharField, Check, IntegerField, TextField
from playhouse.sqlite_ext import JSONField

from pal.foundation.persistence import BaseModel, utc_now


class MemoryFactModel(BaseModel):
    fact_id = CharField(primary_key=True)
    scope = CharField(default="system", constraints=[Check("scope IN ('system', 'task')")])
    task_id = TextField(null=True)
    title = TextField(null=True)
    summary = TextField(default="")
    search_text = TextField(default="")
    canonical_key = TextField(null=True)
    dedupe_fingerprint = TextField(null=True)
    payload_blob = JSONField(default=dict)
    lifecycle = CharField(default="active")
    use_count = IntegerField(default=0)
    last_used_at = TextField(null=True)
    created_at = TextField(default=utc_now)
    updated_at = TextField(default=utc_now)

    class Meta:
        table_name = "memory_facts"
        indexes = (
            (("canonical_key",), False),
            (("scope", "task_id"), False),
            (("dedupe_fingerprint",), False),
        )


class MemoryCaseModel(BaseModel):
    case_id = CharField(primary_key=True)
    scope = CharField(default="task", constraints=[Check("scope IN ('system', 'task')")])
    task_id = TextField(null=True)
    title = TextField(null=True)
    summary = TextField(default="")
    situation_text = TextField(default="")
    task_text = TextField(default="")
    action_text = TextField(default="")
    result_text = TextField(default="")
    search_text = TextField(default="")
    dedupe_fingerprint = TextField(null=True)
    payload_blob = JSONField(default=dict)
    lifecycle = CharField(default="active")
    use_count = IntegerField(default=0)
    last_used_at = TextField(null=True)
    created_at = TextField(default=utc_now)
    updated_at = TextField(default=utc_now)

    class Meta:
        table_name = "memory_cases"
        indexes = (
            (("scope", "task_id"), False),
            (("dedupe_fingerprint",), False),
        )


class MemoryTopicModel(BaseModel):
    topic_id = CharField(primary_key=True)
    document_id = CharField()
    topic = TextField()
    normalized_topic = TextField()
    created_at = TextField(default=utc_now)

    class Meta:
        table_name = "memory_topics"
        indexes = (
            (("document_id", "normalized_topic"), True),
            (("normalized_topic",), False),
        )


class MemoryEmbeddingModel(BaseModel):
    embedding_id = CharField(primary_key=True)
    document_id = CharField()
    embedding_kind = CharField(default="primary")
    model_name = TextField()
    model_revision = TextField(null=True)
    source_text_hash = TextField()
    embedding_norm = TextField(null=True)
    index_status = CharField(
        default="pending",
        constraints=[Check("index_status IN ('pending', 'ready', 'stale', 'failed')")],
    )
    last_error = TextField(null=True)
    created_at = TextField(default=utc_now)
    updated_at = TextField(default=utc_now)

    class Meta:
        table_name = "memory_embeddings"
        indexes = (
            (("document_id", "embedding_kind"), True),
            (("index_status", "updated_at"), False),
        )


class MemoryEmbeddingVecModel(BaseModel):
    embedding_id = CharField(primary_key=True)
    vector_blob = BlobField()
    dimension = IntegerField()
    updated_at = TextField(default=utc_now)

    class Meta:
        table_name = "memory_embedding_vec"
