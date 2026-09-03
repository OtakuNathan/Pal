from __future__ import annotations

from dataclasses import dataclass

from pal.bootstrap.contracts import RuntimeComposerPort
from pal.channel import (
    ChannelRuntime,
    register_with_core as register_channel_with_core,
)
from pal.core import PalCore, register_with_core as register_core_with_core
from pal.core.runtime_config import RuntimeConfig
from pal.execution import register_with_core as register_execution_with_core
from pal.foundation import PalV2Database
from pal.llm import (
    EndpointResolver,
    LLMEndpointRepository,
    LLMRuntime,
    LLMCredentialResolver,
    RuntimeSettingRepository,
    build_default_endpoint_invoker,
    register_with_core as register_llm_with_core,
)
from pal.llm.secret_store import EncryptedFileSecretStore
from pal.bunshin.harnesses import BunshinHarnessRegistry
from pal.plugins import PluginHost
from pal.shared.text_search import warmup_jieba
from pal.wizard import PalRegistration, WizardService


@dataclass
class StubRuntimeHandle:
    wizard: WizardService
    registration: PalRegistration
    database: PalV2Database
    core: PalCore
    channel_runtime: ChannelRuntime
    channel_provider_manager: object
    llm_runtime: LLMRuntime
    plugin_host: PluginHost

    def _optional_port(self, key: str):
        return self.core.context.port_registry.get(key)

    def _required_port(self, key: str):
        return self.core.context.require_port(key)

    @property
    def identity_service(self):
        return self._required_port("identity:identity")

    @property
    def memory_service(self):
        return self._required_port("memory:memory")

    @property
    def control_plane(self):
        return self._required_port("control:control")

    @property
    def failure_runtime(self):
        return self._required_port("failure:failure")

    @property
    def proactive_manager(self):
        return self._optional_port("proactive:proactive_manager")

    @property
    def proactive_runner(self):
        return self._optional_port("proactive:proactive_runner")

    @property
    def proactive_repository(self):
        manager = self.proactive_manager
        return getattr(manager, "repository", None)

    @property
    def behavior_service(self):
        return self._optional_port("behavior:behavior")

    @property
    def skill_service(self):
        return self._optional_port("skill:skill")

    @property
    def artifact_service(self):
        return self._optional_port("artifact:artifact")

    async def stop_async(self) -> None:
        self.plugin_host.shutdown()
        for module_id, handle in tuple(self.core.context.module_registry.modules.items()):
            if module_id == "channel":
                continue
            shutdown_async = getattr(handle, "shutdown_async", None)
            shutdown_sync = getattr(handle, "shutdown_sync", None)
            if callable(shutdown_async):
                try:
                    await shutdown_async()
                except Exception:
                    continue
            elif callable(shutdown_sync):
                try:
                    shutdown_sync()
                except Exception:
                    continue
        provider_stopper = getattr(self.channel_provider_manager, "stop_async", None)
        if callable(provider_stopper):
            await provider_stopper()
        else:
            await self.channel_runtime.stop_async()
        self.database.close()


def compose_runtime(
    *,
    wizard: WizardService,
    registration: PalRegistration,
    database: PalV2Database,
) -> StubRuntimeHandle:
    # Bootstrap only wires the in-process runtime graph. Any first-run
    # provisioning and database-file ownership live in wizard.
    warmup_jieba()
    llm_repository = LLMEndpointRepository()
    runtime_settings_repository = RuntimeSettingRepository()

    config = RuntimeConfig.load(registration.runtime.runtime_root)
    core = PalCore(config=config)
    core.context.execution_runtime.runtime_root = registration.runtime.runtime_root
    channel_runtime = ChannelRuntime()
    secrets_path = registration.runtime.runtime_root / "secrets.json"
    secret_store = EncryptedFileSecretStore(secrets_path=str(secrets_path))
    credential_resolver = LLMCredentialResolver(secret_store=secret_store)
    llm_runtime = LLMRuntime(
        endpoint_resolver=EndpointResolver(repository=llm_repository),
        settings_repository=runtime_settings_repository,
        endpoint_invoker=build_default_endpoint_invoker(
            credentials=credential_resolver,
            runtime_root=registration.runtime.runtime_root,
        ),
        config=config,
    )
    bunshin_harness_registry = BunshinHarnessRegistry()
    runtime_settings_repository.ensure_defaults()
    plugin_host = PluginHost(
        context=core.context,
        runtime_root=registration.runtime.runtime_root,
        services={
            "bunshin_harness_registry": bunshin_harness_registry,
            "runtime_db_path": registration.runtime.db_path,
        },
    )
    register_core_with_core(core)
    register_execution_with_core(core.context)
    register_channel_with_core(
        core.context,
        channel_runtime,
        runtime_root=registration.runtime.runtime_root,
    )
    register_llm_with_core(core.context, llm_runtime)
    plugin_host.publish_management_capabilities()
    plugin_host.bootstrap()
    channel_provider_manager = core.context.require_port("channel:provider_manager")
    channel_provider_manager.plugin_host = plugin_host
    channel_provider_manager.rescan_providers()

    for module_id in ("core", "execution", "channel", "llm"):
        core.publish_module_capabilities(module_id)

    return StubRuntimeHandle(
        wizard=wizard,
        registration=registration,
        database=database,
        core=core,
        channel_runtime=channel_runtime,
        channel_provider_manager=channel_provider_manager,
        llm_runtime=llm_runtime,
        plugin_host=plugin_host,
    )


@dataclass
class RuntimeComposer(RuntimeComposerPort):
    def compose_runtime(
        self,
        *,
        wizard: WizardService,
        registration: PalRegistration,
        database: PalV2Database,
    ) -> StubRuntimeHandle:
        return compose_runtime(
            wizard=wizard,
            registration=registration,
            database=database,
        )
