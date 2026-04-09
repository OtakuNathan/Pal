from __future__ import annotations

from peewee import Check, CharField, TextField
from playhouse.sqlite_ext import JSONField

from pal.foundation.persistence import BaseModel, utc_now


class PalPersonaModel(BaseModel):
    persona_id = CharField(primary_key=True)
    display_name = TextField()
    language = CharField(default="en")
    vibe = TextField(null=True)
    tone = TextField(null=True)
    style_notes = TextField(null=True)
    core_policy = JSONField(default=list)
    created_at = TextField(default=utc_now)
    updated_at = TextField(default=utc_now)

    class Meta:
        table_name = "pal_personas"


class UserPreferencesModel(BaseModel):
    preference_id = CharField(primary_key=True)
    language_preference = CharField(null=True)
    style_preference = TextField(null=True)
    timezone = CharField(null=True)
    preferences_blob = JSONField(default=dict)
    created_at = TextField(default=utc_now)
    updated_at = TextField(default=utc_now)

    class Meta:
        table_name = "user_preferences"


class PalStateModel(BaseModel):
    persona = CharField(primary_key=True, column_name="persona_id")
    status = CharField(default="idle", constraints=[Check("status IN ('idle', 'running', 'paused', 'blocked')")])
    top_of_mind_refs = JSONField(default=list)
    last_active_at = TextField(null=True)
    created_at = TextField(default=utc_now)
    updated_at = TextField(default=utc_now)

    class Meta:
        table_name = "pal_states"
