from __future__ import annotations

from pal.foundation import utc_now
from pal.web_fetch.models import WebFetchProviderModel


class WebFetchProviderRepository:
    def upsert(self, **payload) -> WebFetchProviderModel:
        provider_id = str(payload["provider_id"])
        instance = WebFetchProviderModel.get_or_none(WebFetchProviderModel.provider_id == provider_id)
        now = utc_now()
        if instance is None:
            return WebFetchProviderModel.create(created_at=now, updated_at=now, **payload)
        for key, value in payload.items():
            setattr(instance, key, value)
        instance.updated_at = now
        instance.save()
        return instance

    def ensure_defaults(self, payloads: list[dict] | tuple[dict, ...]) -> None:
        for payload in payloads:
            self.upsert(**dict(payload))

    def get(self, provider_id: str) -> WebFetchProviderModel | None:
        return WebFetchProviderModel.get_or_none(WebFetchProviderModel.provider_id == str(provider_id))

    def list_all(self) -> list[WebFetchProviderModel]:
        query = WebFetchProviderModel.select().order_by(WebFetchProviderModel.priority, WebFetchProviderModel.provider_id)
        return list(query)

    def list_enabled(self) -> list[WebFetchProviderModel]:
        query = (
            WebFetchProviderModel.select()
            .where(WebFetchProviderModel.enabled == True)
            .order_by(WebFetchProviderModel.priority, WebFetchProviderModel.provider_id)
        )
        return list(query)

    def set_enabled(self, provider_id: str, enabled: bool) -> WebFetchProviderModel | None:
        instance = self.get(provider_id)
        if instance is None:
            return None
        instance.enabled = bool(enabled)
        instance.updated_at = utc_now()
        instance.save()
        return instance

    def merge_settings(self, provider_id: str, patch: dict[str, object]) -> WebFetchProviderModel | None:
        instance = self.get(provider_id)
        if instance is None:
            return None
        updated = dict(instance.settings_blob or {})
        updated.update(dict(patch))
        instance.settings_blob = updated
        instance.updated_at = utc_now()
        instance.save()
        return instance

    def merge_auth_material(self, provider_id: str, patch: dict[str, object]) -> WebFetchProviderModel | None:
        instance = self.get(provider_id)
        if instance is None:
            return None
        updated = dict(instance.auth_material_blob or {})
        updated.update(dict(patch))
        instance.auth_material_blob = updated
        instance.updated_at = utc_now()
        instance.save()
        return instance
