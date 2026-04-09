from __future__ import annotations

from pal.channel.models import ChannelEndpointModel
from pal.foundation import utc_now


class ChannelEndpointRepository:
    def upsert(self, **payload) -> ChannelEndpointModel:
        endpoint_id = str(payload["endpoint_id"])
        instance = ChannelEndpointModel.get_or_none(ChannelEndpointModel.endpoint_id == endpoint_id)
        now = utc_now()
        if instance is None:
            return ChannelEndpointModel.create(
                created_at=now,
                updated_at=now,
                **payload,
            )

        for key, value in payload.items():
            setattr(instance, key, value)
        instance.updated_at = now
        instance.save()
        return instance

    def get(self, endpoint_id: str) -> ChannelEndpointModel | None:
        return ChannelEndpointModel.get_or_none(ChannelEndpointModel.endpoint_id == endpoint_id)

    def list_all(self, *, channel_kind: str | None = None) -> list[ChannelEndpointModel]:
        query = ChannelEndpointModel.select()
        if channel_kind:
            query = query.where(ChannelEndpointModel.channel_kind == channel_kind)
        return list(query.order_by(ChannelEndpointModel.channel_kind, ChannelEndpointModel.endpoint_id))

    def find_by_binding(self, *, channel_kind: str, binding_key: str) -> ChannelEndpointModel | None:
        return ChannelEndpointModel.get_or_none(
            (ChannelEndpointModel.channel_kind == channel_kind)
            & (ChannelEndpointModel.binding_key == binding_key)
        )

    def list_enabled(self, *, channel_kind: str | None = None) -> list[ChannelEndpointModel]:
        query = ChannelEndpointModel.select().where(ChannelEndpointModel.enabled == True)
        if channel_kind:
            query = query.where(ChannelEndpointModel.channel_kind == channel_kind)
        return list(query.order_by(ChannelEndpointModel.channel_kind, ChannelEndpointModel.endpoint_id))

    def set_enabled(self, endpoint_id: str, enabled: bool) -> ChannelEndpointModel | None:
        instance = self.get(endpoint_id)
        if instance is None:
            return None
        instance.enabled = enabled
        instance.updated_at = utc_now()
        instance.save()
        return instance

    def set_attached(self, endpoint_id: str, attached: bool) -> ChannelEndpointModel | None:
        instance = self.get(endpoint_id)
        if instance is None:
            return None
        instance.detached_at = None if attached else utc_now()
        instance.updated_at = utc_now()
        instance.save()
        return instance

    def merge_binding_metadata(self, endpoint_id: str, patch: dict[str, object]) -> ChannelEndpointModel | None:
        instance = self.get(endpoint_id)
        if instance is None:
            return None
        metadata = dict(instance.binding_metadata or {})
        metadata.update(dict(patch))
        instance.binding_metadata = metadata
        instance.updated_at = utc_now()
        instance.save()
        return instance
