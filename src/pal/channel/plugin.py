"""Channel contributions owned by a PluginHost generation.

The resident manager owns endpoint identity and delivery. A contribution owns
only loaded provider code and transports; closing it never removes physical hubs.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pal.core.module_registry import ModuleHandle, MODULE_TIER_DETACHABLE
from pal.shared import RuntimeStatus
from pal.channel.provider_manager import (
    ChannelEndpointProviderManager, ChannelProvider, ChannelProviderBuildContext, DiscoveredChannelProvider,
    RuntimeChannelProviderHandle, RuntimeChannelProviderManifest,
    _read_runtime_provider_manifest,
)


if TYPE_CHECKING:
    from pal.plugins.contracts import PluginManifest
    from pal.plugins.host import PluginHost


@dataclass
class ChannelExtension:
    provider: ChannelProvider | None = None
    legacy_manifest: RuntimeChannelProviderManifest | None = None
    manager: ChannelEndpointProviderManager | None = None
    provider_id: str = ''

    def install(self, manager: ChannelEndpointProviderManager, plugin_id: str, manifest: PluginManifest) -> None:
        if plugin_id in manager.runtime_provider_handles:
            raise RuntimeError("channel provider cleanup remains owned")
        if plugin_id in manager.providers:
            raise ValueError('channel plugin conflicts with a resident provider')
        self.manager, self.provider_id = manager, plugin_id
        manager.plugin_provider_ids.add(plugin_id)
        if self.legacy_manifest is not None:
            handle = manager._build_runtime_provider_handle(self.legacy_manifest)
        else:
            provider = self.provider
            if provider is None or provider.provider_id != plugin_id:
                raise ValueError('channel provider_id must equal its owning plugin_id')
            if tuple(provider.endpoint_types) != manifest.channel_endpoint_types:
                raise ValueError('channel endpoint types must match the plugin manifest')
            provider_manifest = RuntimeChannelProviderManifest(
                plugin_id, manifest.entrypoint, manifest.version, True, manifest.filesystem_path)
            context = ChannelProviderBuildContext(manager.runtime_root, Path(manifest.filesystem_path),
                                                  provider_manifest, manager)
            handle = RuntimeChannelProviderHandle(provider_manifest, provider, (), context.cleanup_callbacks, context)
            if plugin_id in manager.runtime_provider_handles:
                raise RuntimeError('channel provider cleanup remains owned')
            manager.runtime_provider_handles[plugin_id] = handle
        replacement = DiscoveredChannelProvider(handle.manifest, tuple(handle.provider.endpoint_types))
        manager._replace_discovered_provider(plugin_id, replacement)
        manager._activate_runtime_provider_handle(handle)
        expected = {r.endpoint_id for r in manager.repository.list_all()
                    if r.channel_kind in replacement.endpoint_types and r.enabled and r.detached_at is None}
        restored = set(manager.hydrate_provider(plugin_id))
        if expected - restored:
            raise RuntimeError('channel endpoint startup failed: ' + ', '.join(sorted(expected - restored)))

    def close(self) -> None:
        if self.manager is None:
            return
        _, errors = self.manager._stop_provider_transports(self.provider_id, reason='plugin_detach')
        if errors:
            raise RuntimeError('; '.join(errors))
        errors = self.manager._unload_runtime_provider(self.provider_id)
        if errors:
            raise RuntimeError('; '.join(errors))


class LegacyChannelPlugin:
    """Read old provider packages in place; their generation belongs to PluginHost."""
    def __init__(self, manifest: PluginManifest):
        self.manifest = manifest

    def start(self, scope):
        provider_manifest = _read_runtime_provider_manifest(Path(self.manifest.filesystem_path) / 'provider.toml')
        if provider_manifest.provider_id != self.manifest.plugin_id:
            raise ValueError('provider identity changed; rescan plugins before loading')
        return ModuleHandle(module_id=self.manifest.module_id, tier=MODULE_TIER_DETACHABLE,
                            detachable=True, channel_extension=ChannelExtension(legacy_manifest=provider_manifest))


def scan_channel_plugins(host: PluginHost) -> None:
    """Adapt legacy metadata without moving files or importing provider code."""
    from pal.plugins.contracts import PluginManifest, PLUGIN_LIFECYCLE_RAII_V1
    manager = host.context.port_registry.get('channel:provider_manager')
    if manager is None:
        return
    manager.plugin_host = host
    scan = manager._scan_runtime_provider_manifests()
    host.scan_errors.extend(scan['errors'])
    seen = set()
    for path in sorted(scan['seen_paths']):
        try:
            legacy = _read_runtime_provider_manifest(Path(path))
            plugin_id = legacy.provider_id
            if plugin_id in seen:
                raise ValueError(f'duplicate channel plugin: {plugin_id}')
            seen.add(plugin_id)
            if plugin_id in manager.providers and plugin_id not in manager.discovered_runtime_providers:
                raise ValueError(f'channel plugin conflicts with resident provider: {plugin_id}')
            existing = host.manifests.get(plugin_id)
            if existing is not None and not existing.legacy_channel:
                raise ValueError(f'channel provider conflicts with plugin: {plugin_id}')
            if plugin_id not in scan['enabled'] and plugin_id not in scan['disabled']:
                continue
            manifest = PluginManifest(plugin_id=plugin_id, entrypoint='', version=legacy.version,
                filesystem_path=legacy.filesystem_path, enabled_by_default=legacy.enabled,
                lifecycle_protocol=PLUGIN_LIFECYCLE_RAII_V1, module_id='channel_provider.' + plugin_id,
                requires_ports=('channel:provider_manager',), legacy_channel=True)
            host.manifests[plugin_id] = manifest
            host.third_party_repository.upsert_discovered(plugin_id=plugin_id, entrypoint='',
                version=manifest.version, filesystem_path=manifest.filesystem_path,
                enabled_by_default=legacy.enabled)
            row = host.third_party_repository.get(plugin_id)
            host.third_party_repository.set_config(plugin_id, {
                **dict(row.config_blob or {}), 'module_id': manifest.module_id,
                'lifecycle_protocol': manifest.lifecycle_protocol,
                'requires_ports': list(manifest.requires_ports), 'legacy_channel': True})
            manager.plugin_provider_ids.add(plugin_id)
        except Exception as exc:
            host.scan_errors.append(f'{path}: {type(exc).__name__}: {exc}')
    # Malformed existing manifests are retained, never interpreted as removals.
    for plugin_id in tuple(manager.plugin_provider_ids):
        manifest = host.manifests.get(plugin_id)
        if manifest is None:
            continue
        path = Path(manifest.filesystem_path) / ('provider.toml' if manifest.legacy_channel else 'plugin.toml')
        if path.exists():
            continue
        try:
            result = host.detach(plugin_id)
            if result['status'] != RuntimeStatus.OK:
                raise RuntimeError(result.get('error', 'channel plugin cleanup failed'))
            manager._remove_discovered_provider(plugin_id, reason='plugin_removed')
            manager.plugin_provider_ids.discard(plugin_id)
            host.manifests.pop(plugin_id, None)
        except Exception as exc:
            host.scan_errors.append(f'{plugin_id}: {type(exc).__name__}: {exc}')
    for manifest in tuple(host.manifests.values()):
        if not manifest.channel_endpoint_types:
            continue
        try:
            if manifest.plugin_id in manager.providers and manifest.plugin_id not in manager.discovered_runtime_providers:
                raise ValueError("channel plugin conflicts with resident provider")
            manager.plugin_provider_ids.add(manifest.plugin_id)
            provider_manifest = RuntimeChannelProviderManifest(manifest.plugin_id, manifest.entrypoint,
                manifest.version, manifest.enabled_by_default, manifest.filesystem_path)
            if manifest.plugin_id not in manager.discovered_runtime_providers:
                manager._register_discovered_provider(DiscoveredChannelProvider(provider_manifest, manifest.channel_endpoint_types))
                manager._ensure_provider_hubs(manifest.plugin_id, manifest.channel_endpoint_types)
        except Exception as exc:
            host.scan_errors.append(f'{manifest.plugin_id}: {type(exc).__name__}: {exc}')
