from __future__ import annotations

from peewee import BooleanField, CharField, Check, TextField
from playhouse.sqlite_ext import JSONField
from pal.foundation.persistence import BaseModel, utc_now

# Keep the existing table name so stored manuals remain readable.
class SkillModel(BaseModel):
    skill_id = CharField(primary_key=True)
    module_id = CharField(default="")
    title = TextField(default="")
    summary = TextField(default="")
    manual_text = TextField(default="")
    source_kind = CharField(default="declared", constraints=[Check("source_kind IN ('declared', 'instructed', 'learned')")])
    activation_terms_blob = JSONField(default=list)
    capability_refs_blob = JSONField(default=list)
    metadata_blob = JSONField(default=dict)
    enabled = BooleanField(default=True)
    created_at = TextField(default=utc_now)
    updated_at = TextField(default=utc_now)

    class Meta:
        table_name = "behavior_skills"
        indexes = (
            (("module_id",), False),
            (("source_kind",), False),
            (("enabled",), False),
        )
