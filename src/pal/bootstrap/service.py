from __future__ import annotations

from dataclasses import dataclass

from pal.bootstrap.contracts import RuntimeComposerPort
from pal.channel import (
    ChannelEndpointRepository,
    ChannelRuntime,
    build_default_factory_registry,
    register_with_core as register_channel_with_core,
)
from pal.control import ControlPlane, register_with_core as register_control_with_core
from pal.core import PalCore, register_with_core as register_core_with_core
from pal.execution import register_with_core as register_execution_with_core
from pal.failure import FailureRuntime, register_with_core as register_failure_with_core
from pal.foundation import PalV2Database
from pal.identity import IdentityRepository, IdentityService, register_with_core as register_identity_with_core
from pal.llm import (
    EndpointResolver,
    LLMEndpointRepository,
    LLMRuntime,
    LiteLLMCredentialResolver,
    LiteLLMEndpointInvoker,
    RuntimeSettingRepository,
    register_with_core as register_llm_with_core,
)
from pal.llm.secret_store import EncryptedFileSecretStore
from pal.memory import L3ProviderSelector, MemoryService, register_with_core as register_memory_with_core
from pal.plugins import PluginHost, register_with_core as register_plugins_with_core
from pal.service import ServiceManager, ServiceRepository, ServiceRunner, register_with_core as register_service_with_core
from pal.supervisor import PalRegistration, SupervisorService


@dataclass
class StubRuntimeHandle:
    supervisor: SupervisorService
    registration: PalRegistration
    database: PalV2Database
    core: PalCore
    channel_runtime: ChannelRuntime
    identity_service: IdentityService
    llm_runtime: LLMRuntime
    memory_service: MemoryService
    plugin_host: PluginHost
    service_manager: ServiceManager
    service_repository: ServiceRepository
    service_runner: ServiceRunner
    control_plane: ControlPlane
    failure_runtime: FailureRuntime

    async def stop_async(self) -> None:
        await self.channel_runtime.stop_async()
        for handle in tuple(self.core.context.module_registry.modules.values()):
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
        self.database.close()


def compose_runtime(
    *,
    supervisor: SupervisorService,
    registration: PalRegistration,
    database: PalV2Database,
) -> StubRuntimeHandle:
    # Bootstrap only wires the in-process runtime graph. Any first-run
    # provisioning and database-file ownership live in supervisor.
    identity_service = IdentityService(repository=IdentityRepository())
    llm_repository = LLMEndpointRepository()
    runtime_settings_repository = RuntimeSettingRepository()
    channel_repository = ChannelEndpointRepository()

    core = PalCore()
    channel_runtime = ChannelRuntime()
    secrets_path = registration.runtime.runtime_root / "secrets.json"
    secret_store = EncryptedFileSecretStore(secrets_path=str(secrets_path))
    credential_resolver = LiteLLMCredentialResolver(secret_store=secret_store)
    llm_runtime = LLMRuntime(
        endpoint_resolver=EndpointResolver(repository=llm_repository),
        settings_repository=runtime_settings_repository,
        endpoint_invoker=LiteLLMEndpointInvoker(credentials=credential_resolver),
    )
    memory_service = MemoryService(
        l3_selector=L3ProviderSelector(resolver=core.context.execution_runtime.l3_plugin_registry.require)
    )
    runtime_settings_repository.ensure_defaults()
    plugin_host = PluginHost(
        context=core.context,
        runtime_root=registration.runtime.runtime_root,
        services={"memory_service": memory_service},
    )
    service_repository = ServiceRepository()
    service_manager = ServiceManager(repository=service_repository)
    service_runner = ServiceRunner(repository=service_repository)
    control_plane = ControlPlane()
    failure_runtime = FailureRuntime()
    endpoint_factories = build_default_factory_registry()

    register_core_with_core(core)
    register_execution_with_core(core.context)
    register_channel_with_core(core.context, channel_runtime)
    register_identity_with_core(core.context, identity_service)
    register_llm_with_core(core.context, llm_runtime)
    register_memory_with_core(core.context, memory_service)
    register_plugins_with_core(core.context, plugin_host)
    register_service_with_core(core.context, service_manager, service_runner)
    register_control_with_core(core.context, control_plane)
    register_failure_with_core(core, failure_runtime)
    for record in channel_repository.list_all():
        runtime_endpoint = endpoint_factories.create(
            record,
            runtime_root=registration.runtime.runtime_root,
        )
        if runtime_endpoint is not None:
            channel_runtime.register_endpoint(runtime_endpoint)
    for stored in service_repository.list_definitions():
        service_manager.hydrate(stored.definition, next_due_at_utc=stored.next_due_at_utc)
    plugin_host.bootstrap()

    for module_id in ("core", "execution", "channel", "identity", "llm", "memory", "plugins", "service", "control", "failure"):
        core.publish_module_capabilities(module_id)

    return StubRuntimeHandle(
        supervisor=supervisor,
        registration=registration,
        database=database,
        core=core,
        channel_runtime=channel_runtime,
        identity_service=identity_service,
        llm_runtime=llm_runtime,
        memory_service=memory_service,
        plugin_host=plugin_host,
        service_manager=service_manager,
        service_repository=service_repository,
        service_runner=service_runner,
        control_plane=control_plane,
        failure_runtime=failure_runtime,
    )


@dataclass
class RuntimeComposer(RuntimeComposerPort):
    def compose_runtime(
        self,
        *,
        supervisor: SupervisorService,
        registration: PalRegistration,
        database: PalV2Database,
    ) -> StubRuntimeHandle:
        return compose_runtime(
            supervisor=supervisor,
            registration=registration,
            database=database,
        )
