from __future__ import annotations

from pal.foundation import utc_now
from pal.plugins.models import PluginBundleModel


class PluginBundleRepository:
    def upsert_discovered(
        self,
        *,
        plugin_id: str,
        entrypoint: str,
        version: str,
        filesystem_path: str,
        enabled_by_default: bool,
    ) -> PluginBundleModel:
        now = utc_now()
        instance = PluginBundleModel.get_or_none(PluginBundleModel.plugin_id == plugin_id)
        if instance is None:
            return PluginBundleModel.create(
                plugin_id=plugin_id,
                entrypoint=entrypoint,
                version=version,
                filesystem_path=filesystem_path,
                enabled=enabled_by_default,
                attached=False,
                config_blob={},
                last_load_status="discovered",
                created_at=now,
                updated_at=now,
            )
        instance.entrypoint = entrypoint
        instance.version = version
        instance.filesystem_path = filesystem_path
        instance.updated_at = now
        instance.save()
        return instance

    def get(self, plugin_id: str) -> PluginBundleModel | None:
        return PluginBundleModel.get_or_none(PluginBundleModel.plugin_id == plugin_id)

    def list_all(self) -> list[PluginBundleModel]:
        return list(PluginBundleModel.select().order_by(PluginBundleModel.plugin_id))

    def list_enabled(self) -> list[PluginBundleModel]:
        query = PluginBundleModel.select().where(PluginBundleModel.enabled == True).order_by(PluginBundleModel.plugin_id)
        return list(query)

    def set_enabled(self, plugin_id: str, enabled: bool) -> PluginBundleModel | None:
        instance = self.get(plugin_id)
        if instance is None:
            return None
        instance.enabled = enabled
        instance.updated_at = utc_now()
        instance.last_load_status = "disabled" if not enabled else instance.last_load_status
        instance.save()
        return instance

    def set_attached(self, plugin_id: str, attached: bool) -> PluginBundleModel | None:
        instance = self.get(plugin_id)
        if instance is None:
            return None
        instance.attached = attached
        instance.updated_at = utc_now()
        instance.last_load_status = "attached" if attached else "detached"
        instance.save()
        return instance

    def set_load_status(self, plugin_id: str, *, status: str, error_text: str | None = None) -> PluginBundleModel | None:
        instance = self.get(plugin_id)
        if instance is None:
            return None
        instance.last_load_status = status
        instance.last_error = error_text
        instance.updated_at = utc_now()
        instance.save()
        return instance
