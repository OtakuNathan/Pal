from __future__ import annotations

from peewee import BooleanField, CharField, TextField
from playhouse.sqlite_ext import JSONField

from pal.foundation.persistence import BaseModel, utc_now


class ServiceDefinitionModel(BaseModel):
    service_id = CharField(primary_key=True)
    goal = TextField()
    method = TextField(default="")
    skill_refs_blob = JSONField(default=list)
    out_channel_id = CharField(null=True)
    schedule_blob = JSONField(default=dict)
    enabled = BooleanField(default=True)
    next_due_at_utc = TextField(null=True)
    last_run_at_utc = TextField(null=True)
    created_at = TextField(default=utc_now)
    updated_at = TextField(default=utc_now)

    class Meta:
        table_name = "service_definitions"


class ServiceRunModel(BaseModel):
    service_run_id = CharField(primary_key=True)
    service_id = CharField(index=True)
    trigger_kind = CharField()
    status = CharField(default="running")
    trigger_metadata = JSONField(default=dict)
    turn_id = CharField(null=True)
    output_summary = TextField(null=True)
    error_text = TextField(null=True)
    started_at = TextField(default=utc_now)
    completed_at = TextField(null=True)
    created_at = TextField(default=utc_now)
    updated_at = TextField(default=utc_now)

    class Meta:
        table_name = "service_runs"
