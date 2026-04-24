from __future__ import annotations

from peewee import BooleanField, CharField, Check, FloatField, IntegerField, TextField
from playhouse.sqlite_ext import JSONField

from pal.foundation.persistence import BaseModel, utc_now


class BehaviorAffordanceModel(BaseModel):
    affordance_id = CharField(primary_key=True)
    module_id = CharField(default="")
    title = TextField(default="")
    scenario_text = TextField(default="")
    prompt_hint = TextField(default="")
    visibility_mode = CharField(default="discoverable", constraints=[Check("visibility_mode IN ('resident', 'discoverable')")])
    activation_kind = CharField(default="deliberative", constraints=[Check("activation_kind IN ('deliberative', 'reactive')")])
    activation_mode = CharField(default="suggest", constraints=[Check("activation_mode IN ('suggest', 'automatic', 'require_approval')")])
    source_kind = CharField(default="declared", constraints=[Check("source_kind IN ('declared', 'instructed', 'learned')")])
    activation_terms_blob = JSONField(default=list)
    capability_refs_blob = JSONField(default=list)
    skill_refs_blob = JSONField(default=list)
    memory_query_hints_blob = JSONField(default=list)
    evidence_refs_blob = JSONField(default=list)
    metadata_blob = JSONField(default=dict)
    priority = IntegerField(default=100)
    activation_threshold = FloatField(default=0.25)
    enabled = BooleanField(default=True)
    created_at = TextField(default=utc_now)
    updated_at = TextField(default=utc_now)

    class Meta:
        table_name = "behavior_affordances"
        indexes = (
            (("module_id",), False),
            (("source_kind",), False),
            (("visibility_mode", "activation_kind"), False),
            (("enabled",), False),
        )


class BehaviorSkillModel(BaseModel):
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
