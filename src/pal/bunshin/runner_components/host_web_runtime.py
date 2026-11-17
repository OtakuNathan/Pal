"""Manager-owned web services, without a role or workflow memory dependency."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from pal.core.contracts import CoreRuntimeState
from pal.core.main_context import MainContext
from pal.core.module_lifecycle import ModuleLifecycle
from pal.execution import register_with_core as register_execution
from pal.execution.backend import build_execution_runtime
from pal.foundation import PalV2Database
from pal.llm import RuntimeSettingRepository
from pal.web_fetch import BrowserServiceManager, WebFetchService, register_with_core as register_fetch
from pal.web_search import WebSearchProviderRepository, WebSearchService, register_with_core as register_search
from pal.wizard.runtime import ALL_MODELS
from pal.bunshin.runner_components.runtime_build import RuntimeCleanup, close_registered_modules


@dataclass
class HostWebRuntime:
    context: MainContext
    database: PalV2Database
    _closer: RuntimeCleanup | None = field(default=None, init=False, repr=False)

    @property
    def execution_runtime(self):
        return self.context.execution_runtime

    async def close(self):
        if self._closer is None:
            self._closer = RuntimeCleanup(role_memory=None, llm_runtime=None, context=self.context, database=self.database)
        await self._closer.close()


def build_host_web_runtime(runtime_root: Path) -> HostWebRuntime:
    with ExitStack() as cleanup:
        database = PalV2Database(runtime_root / "pal.sqlite3")
        cleanup.callback(database.close)
        database.initialize(ALL_MODELS)
        context = MainContext(execution_runtime=build_execution_runtime())
        cleanup.callback(context.execution_runtime.shutdown)
        cleanup.callback(close_registered_modules, context)
        context.execution_runtime.configure_runtime_root(runtime_root)
        register_execution(context)
        register_search(context, WebSearchService(repository=WebSearchProviderRepository(),
                                                 settings_repository=RuntimeSettingRepository()))
        register_fetch(context, WebFetchService(browser_manager=BrowserServiceManager(runtime_root=runtime_root)))
        lifecycle = ModuleLifecycle(context, CoreRuntimeState())
        for module_id in ("execution", "web_search", "web_fetch"):
            lifecycle.publish_module_capabilities(module_id)
        cleanup.pop_all()
        return HostWebRuntime(context, database)
