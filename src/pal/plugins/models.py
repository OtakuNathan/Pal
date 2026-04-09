from __future__ import annotations

from peewee import BooleanField, CharField, TextField
from playhouse.sqlite_ext import JSONField

from pal.foundation.persistence import BaseModel, utc_now


class PluginBundleModel(BaseModel):
    plugin_id = CharField(primary_key=True)
    entrypoint = TextField()
    version = TextField()
    filesystem_path = TextField()
    enabled = BooleanField(default=True)
    attached = BooleanField(default=False)
    config_blob = JSONField(default=dict)
    last_load_status = CharField(default="discovered")
    last_error = TextField(null=True)
    created_at = TextField(default=utc_now)
    updated_at = TextField(default=utc_now)

    class Meta:
        table_name = "plugin_bundles"
