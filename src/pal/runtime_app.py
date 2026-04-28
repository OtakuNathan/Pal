from __future__ import annotations

import asyncio
import shutil
import signal
from dataclasses import dataclass
from pathlib import Path

from pal.bootstrap import StubRuntimeHandle, compose_runtime
from pal.channel import ChannelEndpointRepository
from pal.wizard.runtime import DEFAULT_DB_FILENAME, DEFAULT_PAL_ENTRYPOINT
from pal.wizard import PalRegistration, WizardService


def _ensure_plugin_layout(runtime_root: Path, wizard: WizardService, registration: PalRegistration) -> None:
    builtin_dir = runtime_root / "plugins" / "_builtin"
    community_dir = runtime_root / "plugins" / "community"
    old_plugins = runtime_root / "plugins"

    # Upgrade: if flat plugins/ exists without _builtin/community subdirs
    if old_plugins.exists() and not builtin_dir.exists():
        community_dir.mkdir(parents=True, exist_ok=True)
        for item in list(old_plugins.iterdir()):
            if item.is_dir() and item.name not in ("_builtin", "community"):
                target = community_dir / item.name
                if not target.exists():
                    shutil.move(str(item), str(target))

    if not builtin_dir.exists() or not any(builtin_dir.glob("*/plugin.toml")):
        wizard.provision_builtin_plugins(registration)


def open_runtime(runtime_root: Path) -> StubRuntimeHandle:
    wizard = WizardService()
    db_path = runtime_root / DEFAULT_DB_FILENAME
    if db_path.exists():
        registration = wizard.provision_runtime(
            display_name="PalV2",
            runtime_root=runtime_root,
            db_filename=DEFAULT_DB_FILENAME,
            pal_entrypoint=DEFAULT_PAL_ENTRYPOINT,
        )
        database = wizard.create_database(registration)
        _ensure_plugin_layout(runtime_root, wizard, registration)
        if not ChannelEndpointRepository().list_all():
            wizard.seed_defaults(registration)
    else:
        provisioned = wizard.provision_stub_runtime(runtime_root)
        registration = provisioned.registration
        database = provisioned.database
    return compose_runtime(
        wizard=wizard,
        registration=registration,
        database=database,
    )


@dataclass
class PalRuntimeApp:
    handle: StubRuntimeHandle
    idle_sleep_seconds: float = 0.02

    async def run(self) -> None:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass
        await self.handle.channel_runtime.start_async()
        publish_catalog = getattr(self.handle.core, "publish_control_catalog_async", None)
        if callable(publish_catalog):
            await publish_catalog()
        try:
            while not stop_event.is_set():
                processed = await self.handle.core.run_until_idle_async(max_iterations=128)
                if not processed:
                    await asyncio.sleep(self.idle_sleep_seconds)
        finally:
            await self.handle.stop_async()


def build_runtime_app(runtime_root: Path) -> PalRuntimeApp:
    return PalRuntimeApp(handle=open_runtime(runtime_root))
