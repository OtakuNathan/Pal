from __future__ import annotations

from peewee import BooleanField, CharField, IntegerField, TextField
from playhouse.sqlite_ext import JSONField

from pal.foundation.persistence import BaseModel, utc_now


class ChannelEndpointModel(BaseModel):
    endpoint_id = CharField(primary_key=True)
    channel_kind = CharField()
    binding_key = TextField()
    enabled = BooleanField(default=True)
    max_message_chars = IntegerField(null=True)
    preferred_parse_mode = CharField(null=True)
    segment_by_default = BooleanField(default=False)
    preserve_code_blocks = BooleanField(default=True)
    supports_typing = BooleanField(default=False)
    supports_receipt_marker = BooleanField(default=False)
    supports_message_edit = BooleanField(default=False)
    binding_metadata = JSONField(default=dict)
    send_policy_blob = JSONField(default=dict)
    created_at = TextField(default=utc_now)
    updated_at = TextField(default=utc_now)
    detached_at = TextField(null=True)

    class Meta:
        table_name = "channel_endpoints"
        indexes = ((("channel_kind", "binding_key"), True),)
