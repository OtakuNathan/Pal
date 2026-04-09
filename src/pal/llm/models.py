from __future__ import annotations

from peewee import BooleanField, Check, CharField, IntegerField, TextField
from playhouse.sqlite_ext import JSONField

from pal.foundation.persistence import BaseModel, utc_now


class LLMEndpointModel(BaseModel):
    endpoint_id = CharField(primary_key=True)
    provider = CharField()
    model_id = TextField()
    display_name = TextField(null=True)
    api_mode = CharField(constraints=[Check("api_mode IN ('openai_chat', 'anthropic_messages')")])
    base_url = TextField()
    auth_kind = CharField(
        default="api_key_ref",
        constraints=[Check("auth_kind IN ('api_key_ref', 'oauth', 'local_provider_auth')")],
    )
    credential_ref = TextField()
    context_window = IntegerField(null=True)
    max_output_tokens = IntegerField(null=True)
    supports_reasoning = BooleanField(default=False)
    supports_tools = BooleanField(default=True)
    supports_streaming = BooleanField(default=True)
    supports_vision = BooleanField(default=False)
    input_modalities_blob = JSONField(default=list)
    output_modalities_blob = JSONField(default=list)
    priority = IntegerField(default=0)
    enabled = BooleanField(default=True)
    capabilities_blob = JSONField(default=dict)
    notes = TextField(null=True)
    created_at = TextField(default=utc_now)
    updated_at = TextField(default=utc_now)

    class Meta:
        table_name = "llm_endpoints"
        indexes = (
            (("enabled", "priority"), False),
            (("enabled", "supports_tools", "supports_reasoning", "supports_streaming"), False),
        )


class PalRuntimeSettingModel(BaseModel):
    setting_key = CharField(primary_key=True)
    setting_value = TextField()
    updated_at = TextField(default=utc_now)

    class Meta:
        table_name = "pal_runtime_settings"
