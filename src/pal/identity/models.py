from __future__ import annotations

from peewee import CharField, TextField
from playhouse.sqlite_ext import JSONField

from pal.foundation.persistence import BaseModel, utc_now


class PalPersonaModel(BaseModel):
    persona_id = CharField(primary_key=True)
    display_name = TextField()
    language = CharField(default="en")
    vibe = TextField(null=True)
    tone = TextField(null=True)
    core_policy = JSONField(default=list)
    created_at = TextField(default=utc_now)
    updated_at = TextField(default=utc_now)

    class Meta:
        table_name = "pal_personas"


class UserPreferencesModel(BaseModel):
    preference_id = CharField(primary_key=True)
    style_preference = TextField(null=True)
    timezone = CharField(null=True)
    preferences_blob = JSONField(default=dict)
    created_at = TextField(default=utc_now)
    updated_at = TextField(default=utc_now)

    class Meta:
        table_name = "user_preferences"

