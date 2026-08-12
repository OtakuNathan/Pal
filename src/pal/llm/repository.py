from __future__ import annotations

from dataclasses import dataclass, field

from pal.foundation import utc_now
from pal.llm.endpoint_spec import merge_endpoint_spec_payload
from pal.llm.models import LLMEndpointModel, PalRuntimeSettingModel


ACTIVE_LLM_ENDPOINT_SETTING_KEY = "active_llm_endpoint_id"
LEGACY_THINK_LEVEL_SETTING_KEY = "think_level"
THINK_LEVEL_SETTING_PREFIX = "think_level:"


class LLMEndpointRepository:
    def upsert(self, **payload) -> LLMEndpointModel:
        endpoint_id = str(payload["endpoint_id"] or "").strip()
        payload = {**payload, "endpoint_id": endpoint_id}
        instance = LLMEndpointModel.get_or_none(LLMEndpointModel.endpoint_id == endpoint_id)
        spec = merge_endpoint_spec_payload(payload, existing=instance)
        validated_payload = spec.to_payload()
        now = utc_now()
        if instance is None:
            return LLMEndpointModel.create(
                created_at=now,
                updated_at=now,
                **validated_payload,
            )

        for key, value in validated_payload.items():
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

    def list_all(self) -> list[LLMEndpointModel]:
        return list(
            LLMEndpointModel.select().order_by(
                LLMEndpointModel.priority,
                LLMEndpointModel.endpoint_id,
            )
        )

    def get(self, endpoint_id: str) -> LLMEndpointModel | None:
        normalized = str(endpoint_id or "").strip()
        if not normalized:
            return None
        return LLMEndpointModel.get_or_none(LLMEndpointModel.endpoint_id == normalized)

    def delete(self, endpoint_id: str) -> bool:
        normalized = str(endpoint_id or "").strip()
        if not normalized:
            raise ValueError("endpoint_id must be non-empty")
        deleted = (
            LLMEndpointModel.delete()
            .where(LLMEndpointModel.endpoint_id == normalized)
            .execute()
        )
        return bool(deleted)

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

    def delete(self, setting_key: str) -> bool:
        deleted = (
            PalRuntimeSettingModel.delete()
            .where(PalRuntimeSettingModel.setting_key == str(setting_key))
            .execute()
        )
        return bool(deleted)

    def get_think_level(self, endpoint_id: str) -> str | None:
        return self.get(_think_level_setting_key(endpoint_id))

    def set_think_level(self, endpoint_id: str, think_level: str) -> PalRuntimeSettingModel:
        normalized = str(think_level or "").strip()
        if not normalized:
            raise ValueError("think_level must be non-empty")
        return self.set(_think_level_setting_key(endpoint_id), normalized)

    def delete_think_level(self, endpoint_id: str) -> bool:
        return self.delete(_think_level_setting_key(endpoint_id))

    def delete_active_llm_endpoint_id(self) -> bool:
        return self.delete(ACTIVE_LLM_ENDPOINT_SETTING_KEY)

    def get_legacy_think_level(self) -> str | None:
        value = str(self.get(LEGACY_THINK_LEVEL_SETTING_KEY) or "").strip()
        return value or None

    def delete_legacy_think_level(self) -> bool:
        return self.delete(LEGACY_THINK_LEVEL_SETTING_KEY)

    def get_active_llm_endpoint_id(self) -> str | None:
        value = str(self.get(ACTIVE_LLM_ENDPOINT_SETTING_KEY) or "").strip()
        return value or None

    def set_active_llm_endpoint_id(self, endpoint_id: str) -> PalRuntimeSettingModel:
        normalized = str(endpoint_id).strip()
        return self.set(ACTIVE_LLM_ENDPOINT_SETTING_KEY, normalized)

    def ensure_defaults(self) -> None:
        return None


@dataclass
class RuntimeSettingSnapshot:
    """Read-through snapshot whose mutations remain local to one LLM runtime."""

    source: RuntimeSettingRepository
    endpoint_ids: tuple[str, ...] = ()
    _values: dict[str, str | None] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        keys = [
            ACTIVE_LLM_ENDPOINT_SETTING_KEY,
            *(_think_level_setting_key(item) for item in self.endpoint_ids),
        ]
        self._values = {key: self.source.get(key) for key in keys}

    def get(self, setting_key: str) -> str | None:
        if setting_key not in self._values:
            self._values[setting_key] = self.source.get(setting_key)
        return self._values[setting_key]

    def set(self, setting_key: str, setting_value: str) -> str:
        self._values[str(setting_key)] = str(setting_value)
        return str(setting_value)

    def delete(self, setting_key: str) -> bool:
        existed = self.get(setting_key) is not None
        self._values[str(setting_key)] = None
        return existed

    def get_think_level(self, endpoint_id: str) -> str | None:
        return self.get(_think_level_setting_key(endpoint_id))

    def set_think_level(self, endpoint_id: str, think_level: str) -> str:
        normalized = str(think_level or "").strip()
        if not normalized:
            raise ValueError("think_level must be non-empty")
        return self.set(_think_level_setting_key(endpoint_id), normalized)

    def get_active_llm_endpoint_id(self) -> str | None:
        value = str(self.get(ACTIVE_LLM_ENDPOINT_SETTING_KEY) or "").strip()
        return value or None

    def set_active_llm_endpoint_id(self, endpoint_id: str) -> str:
        return self.set(ACTIVE_LLM_ENDPOINT_SETTING_KEY, str(endpoint_id).strip())


def _think_level_setting_key(endpoint_id: str) -> str:
    normalized = str(endpoint_id or "").strip()
    if not normalized:
        raise ValueError("endpoint_id must be non-empty")
    return f"{THINK_LEVEL_SETTING_PREFIX}{normalized}"
