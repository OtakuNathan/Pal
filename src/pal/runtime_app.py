from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass
from pathlib import Path

from pal.bootstrap import StubRuntimeHandle, compose_runtime
from pal.channel import ChannelEndpointRepository
from pal.supervisor.runtime import DEFAULT_DB_FILENAME, DEFAULT_PAL_ENTRYPOINT
from pal.supervisor import PalRegistration, SupervisorService


def open_runtime(runtime_root: Path) -> StubRuntimeHandle:
    supervisor = SupervisorService()
    db_path = runtime_root / DEFAULT_DB_FILENAME
    if db_path.exists():
        registration = supervisor.provision_runtime(
            display_name="PalV2",
            runtime_root=runtime_root,
            db_filename=DEFAULT_DB_FILENAME,
            pal_entrypoint=DEFAULT_PAL_ENTRYPOINT,
        )
        database = supervisor.create_database(registration)
        if not ChannelEndpointRepository().list_all():
            supervisor.seed_defaults(registration)
    else:
        provisioned = supervisor.provision_stub_runtime(runtime_root)
        registration = provisioned.registration
        database = provisioned.database
    return compose_runtime(
        supervisor=supervisor,
        registration=registration,
        database=database,
    )


@dataclass
class PalRuntimeApp:
    handle: StubRuntimeHandle
    debug_prompt: bool = False
    idle_sleep_seconds: float = 0.02

    async def run(self) -> None:
        stop_event = asyncio.Event()
        self.handle.core.debug_prompt = self.debug_prompt
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass
        await self.handle.channel_runtime.start_async()
        try:
            while not stop_event.is_set():
                processed = await self.handle.core.run_until_idle_async(max_iterations=128)
                if not processed:
                    await asyncio.sleep(self.idle_sleep_seconds)
        finally:
            await self.handle.channel_runtime.stop_async()
            self.handle.database.close()


def build_runtime_app(runtime_root: Path, *, debug_prompt: bool = False) -> PalRuntimeApp:
    return PalRuntimeApp(handle=open_runtime(runtime_root), debug_prompt=debug_prompt)
