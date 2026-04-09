from __future__ import annotations

from pal.foundation import utc_now
from pal.llm.models import LLMEndpointModel, PalRuntimeSettingModel


DEFAULT_THINK_LEVEL = "balanced"
ACTIVE_LLM_ENDPOINT_SETTING_KEY = "active_llm_endpoint_id"


class LLMEndpointRepository:
    def upsert(self, **payload) -> LLMEndpointModel:
        endpoint_id = str(payload["endpoint_id"])
        instance = LLMEndpointModel.get_or_none(LLMEndpointModel.endpoint_id == endpoint_id)
        now = utc_now()
        if instance is None:
            return LLMEndpointModel.create(
                created_at=now,
                updated_at=now,
                **payload,
            )

        for key, value in payload.items():
            setattr(instance, key, value)
        instance.updated_at = now
        instance.save()
        return instance

    def ensure_defaults(self, payloads: list[dict] | tuple[dict, ...]) -> None:
        for payload in payloads:
            self.upsert(**dict(payload))

    def list_enabled(self) -> list[LLMEndpointModel]:
        query = (
            LLMEndpointModel.select()
            .where(LLMEndpointModel.enabled == True)
            .order_by(LLMEndpointModel.priority, LLMEndpointModel.endpoint_id)
        )
        return list(query)

    def get_primary_enabled(self) -> LLMEndpointModel | None:
        query = (
            LLMEndpointModel.select()
            .where(LLMEndpointModel.enabled == True)
            .order_by(LLMEndpointModel.priority, LLMEndpointModel.endpoint_id)
            .limit(1)
        )
        return query.first()


class RuntimeSettingRepository:
    def get(self, setting_key: str) -> str | None:
        setting = PalRuntimeSettingModel.get_or_none(PalRuntimeSettingModel.setting_key == setting_key)
        if setting is None:
            return None
        return setting.setting_value

    def set(self, setting_key: str, setting_value: str) -> PalRuntimeSettingModel:
        now = utc_now()
        instance = PalRuntimeSettingModel.get_or_none(PalRuntimeSettingModel.setting_key == setting_key)
        if instance is None:
            return PalRuntimeSettingModel.create(
                setting_key=setting_key,
                setting_value=setting_value,
                updated_at=now,
            )
        instance.setting_value = setting_value
        instance.updated_at = now
        instance.save()
        return instance

    def get_think_level(self) -> str:
        return str(self.get("think_level") or DEFAULT_THINK_LEVEL)

    def set_think_level(self, think_level: str) -> PalRuntimeSettingModel:
        return self.set("think_level", str(think_level).strip() or DEFAULT_THINK_LEVEL)

    def get_active_llm_endpoint_id(self) -> str | None:
        value = str(self.get(ACTIVE_LLM_ENDPOINT_SETTING_KEY) or "").strip()
        return value or None

    def set_active_llm_endpoint_id(self, endpoint_id: str) -> PalRuntimeSettingModel:
        normalized = str(endpoint_id).strip()
        return self.set(ACTIVE_LLM_ENDPOINT_SETTING_KEY, normalized)

    def ensure_defaults(self, *, think_level: str = DEFAULT_THINK_LEVEL) -> None:
        if self.get("think_level") is None:
            self.set_think_level(think_level)
