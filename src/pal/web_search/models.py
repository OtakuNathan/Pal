from __future__ import annotations

from peewee import BooleanField, CharField, IntegerField, TextField
from playhouse.sqlite_ext import JSONField

from pal.foundation.persistence import BaseModel, utc_now


class WebSearchProviderModel(BaseModel):
    provider_id = CharField(primary_key=True)
    provider_kind = CharField()
    display_name = TextField(null=True)
    enabled = BooleanField(default=True)
    priority = IntegerField(default=0)
    settings_blob = JSONField(default=dict)
    auth_material_blob = JSONField(default=dict)
    notes = TextField(null=True)
    created_at = TextField(default=utc_now)
    updated_at = TextField(default=utc_now)

    class Meta:
        table_name = "web_search_providers"
        indexes = ((("enabled", "priority"), False),)
