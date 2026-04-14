from __future__ import annotations

from pal.foundation import utc_now
from pal.web_search.models import WebSearchProviderModel


class WebSearchProviderRepository:
    def upsert(self, **payload) -> WebSearchProviderModel:
        provider_id = str(payload["provider_id"])
        instance = WebSearchProviderModel.get_or_none(WebSearchProviderModel.provider_id == provider_id)
        now = utc_now()
        if instance is None:
            return WebSearchProviderModel.create(created_at=now, updated_at=now, **payload)
        for key, value in payload.items():
            setattr(instance, key, value)
        instance.updated_at = now
        instance.save()
        return instance

    def ensure_defaults(self, payloads: list[dict] | tuple[dict, ...]) -> None:
        for payload in payloads:
            self.upsert(**dict(payload))

    def get(self, provider_id: str) -> WebSearchProviderModel | None:
        return WebSearchProviderModel.get_or_none(WebSearchProviderModel.provider_id == str(provider_id))

    def list_all(self) -> list[WebSearchProviderModel]:
        query = WebSearchProviderModel.select().order_by(WebSearchProviderModel.priority, WebSearchProviderModel.provider_id)
        return list(query)

    def list_enabled(self) -> list[WebSearchProviderModel]:
        query = (
            WebSearchProviderModel.select()
            .where(WebSearchProviderModel.enabled == True)
            .order_by(WebSearchProviderModel.priority, WebSearchProviderModel.provider_id)
        )
        return list(query)

    def set_enabled(self, provider_id: str, enabled: bool) -> WebSearchProviderModel | None:
        instance = self.get(provider_id)
        if instance is None:
            return None
        instance.enabled = bool(enabled)
        instance.updated_at = utc_now()
        instance.save()
        return instance

    def merge_settings(self, provider_id: str, patch: dict[str, object]) -> WebSearchProviderModel | None:
        instance = self.get(provider_id)
        if instance is None:
            return None
        updated = dict(instance.settings_blob or {})
        updated.update(dict(patch))
        instance.settings_blob = updated
        instance.updated_at = utc_now()
        instance.save()
        return instance

    def merge_auth_material(self, provider_id: str, patch: dict[str, object]) -> WebSearchProviderModel | None:
        instance = self.get(provider_id)
        if instance is None:
            return None
        updated = dict(instance.auth_material_blob or {})
        updated.update(dict(patch))
        instance.auth_material_blob = updated
        instance.updated_at = utc_now()
        instance.save()
        return instance
