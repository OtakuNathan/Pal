from __future__ import annotations
import os
import tempfile
import asyncio
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Literal
from pal.artifact import ArtifactManager, ArtifactRepository, register_with_core as register_artifact_with_core
from pal.core.contracts import CoreRuntimeState
from pal.core.main_context import MainContext
from pal.core.module_lifecycle import ModuleLifecycle
from pal.core.runtime_config import RuntimeConfig
from pal.core.runtime_state import RuntimeSnapshotCoordinator
from pal.execution import register_with_core as register_execution_with_core
from pal.foundation import PalV2Database
from pal.foundation.workers import join_owned_task
from pal.llm import LLMEndpointRepository, LLMRuntime, LLMCredentialResolver, RuntimeSettingRepository, build_default_endpoint_invoker
from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.repository import RuntimeSettingSnapshot
from pal.llm.secret_store import EncryptedFileSecretStore
from pal.lsp import build_lsp_plugin
from pal.bunshin.ipc import BUNSHIN_RUNTIME_DB_PATH_ENV
from pal.memory import MemoryService, register_with_core as register_memory_with_core
from pal.skill import SkillRepository, SkillService, register_with_core as register_skill_with_core
from pal.skill.repository import ReadOnlySkillRepository
from pal.bunshin.llm_transport import ManagerProxyTransport
from pal.bunshin.llm_output_budget import BunshinEndpointResolver
from pal.bunshin.web_broker import BunshinBrokerWebClient
from pal.plugins.l3 import register_with_core as register_l3_with_core
from pal.web_fetch import BrowserServiceManager, WebFetchService, register_with_core as register_web_fetch_with_core
from pal.web_search import WebSearchProviderRepository, WebSearchService, register_with_core as register_web_search_with_core
from pal.wizard.runtime import ALL_MODELS, DEFAULT_LLM_ENDPOINTS, DEFAULT_WEB_SEARCH_PROVIDERS
from pal.bunshin.runner_components.models import BunshinRuntimeBundle
from pal.bunshin.runner_components.models import _BunshinFailureResult
from pal.bunshin.runner_components.memory_runtime import RoleMemoryRuntime, build_role_memory


def build_slim_bunshin_runtime(
    runtime_root: Path,
    *,
    run_id: str = "",
    llm_authority: Literal["manager_proxy", "host", "none"],
    memory_workflow_id: str = "",
    snapshot_root: Path | None = None,
    max_output_tokens_override: int | None = None,
) -> BunshinRuntimeBundle:
    """Build one runtime with an explicit LLM owner.

    Role processes own the complete shared LLM pipeline and proxy only encoded
    provider frames through Manager. Their shared database view is read-only.
    Roles borrow their workflow generation read-only. Memory candidates use
    a separate in-memory sink; shared database generations are never copied.
    """

    with ExitStack() as cleanup:
        return _build_role_runtime(runtime_root, run_id=run_id, llm_authority=llm_authority,
            memory_workflow_id=memory_workflow_id, snapshot_root=snapshot_root,
            max_output_tokens_override=max_output_tokens_override, cleanup=cleanup)


def _build_role_runtime(runtime_root, *, run_id, llm_authority, memory_workflow_id,
                        snapshot_root, max_output_tokens_override, cleanup):
    if llm_authority not in {"manager_proxy", "host", "none"}:
        raise ValueError(f"unsupported bunshin LLM authority: {llm_authority}")
    if llm_authority == "host" and os.environ.get("PAL_BUNSHIN_SANDBOXED") == "1":
        raise PermissionError("sandboxed bunshin roles cannot construct a host LLM runtime")
    if llm_authority == "manager_proxy" and not str(run_id or "").strip():
        raise ValueError("manager-proxy bunshin runtime requires run_id")
    read_only_database = llm_authority == "manager_proxy"
    configured_db_path = str(os.environ.get(BUNSHIN_RUNTIME_DB_PATH_ENV) or "").strip()
    database = PalV2Database(
        db_path=Path(configured_db_path) if configured_db_path else Path(runtime_root) / "pal.sqlite3",
        read_only=read_only_database,
    )
    cleanup.callback(database.close)
    database.initialize(ALL_MODELS)
    llm_repository = LLMEndpointRepository()
    web_search_repository = WebSearchProviderRepository()
    if not read_only_database:
        if not llm_repository.list_enabled():
            llm_repository.ensure_defaults(DEFAULT_LLM_ENDPOINTS)
        if not web_search_repository.list_all():
            web_search_repository.ensure_defaults(DEFAULT_WEB_SEARCH_PROVIDERS)
    settings = RuntimeSettingRepository()
    if not read_only_database:
        settings.ensure_defaults()
        if settings.get("active_web_search_provider_id") is None:
            enabled = web_search_repository.list_enabled()
            if enabled:
                settings.set("active_web_search_provider_id", enabled[0].provider_id)

    config = RuntimeConfig.load(Path(runtime_root))
    from pal.execution.backend import build_execution_runtime
    context = MainContext(execution_runtime=build_execution_runtime())
    cleanup.callback(context.execution_runtime.shutdown)
    cleanup.callback(close_registered_modules, context)
    context.execution_runtime.configure_runtime_root(Path(runtime_root))
    # Each role owns writable output storage inside its existing run mount.
    # Never expose the resident's or another role's output directory.
    from pal.execution.result_snapshots import ResultSnapshotStore
    if snapshot_root is not None:
        context.execution_runtime.result_snapshots = ResultSnapshotStore(snapshot_root)
    elif run_id:
        context.execution_runtime.result_snapshots = ResultSnapshotStore(
            Path(tempfile.gettempdir()) / "pal-role-output" / run_id)
    lifecycle = ModuleLifecycle(context, CoreRuntimeState())
    artifact_service = ArtifactManager(
        runtime_root=Path(runtime_root),
        repository=ArtifactRepository(),
        writable=not read_only_database,
    )
    llm_runtime = build_role_llm(llm_authority=llm_authority, runtime_root=runtime_root, run_id=run_id,
                                 llm_repository=llm_repository, settings=settings, config=config,
                                 max_output_tokens_override=max_output_tokens_override)
    if llm_runtime is not None:
        cleanup.callback(llm_runtime.close)
    register_execution_with_core(context)
    from pal.execution.worker_extensions import activate_worker_extension
    activate_worker_extension(context, Path(runtime_root), database_path=configured_db_path or None)
    register_artifact_with_core(context, artifact_service)
    memory_service = MemoryService(

    )
    register_memory_with_core(context, memory_service)
    register_skill_with_core(
        context,
        SkillService(
            repository=ReadOnlySkillRepository() if read_only_database else SkillRepository(),
            runtime_root=Path(runtime_root),
        ),
    )
    role_memory = None
    if memory_workflow_id:
        role_memory = build_role_memory(Path(runtime_root), service=memory_service, workflow_id=memory_workflow_id)
        cleanup.callback(role_memory.close)
        register_l3_with_core(context, role_memory.provider)
    broker_web = (
        BunshinBrokerWebClient(runtime_root=Path(runtime_root), run_id=run_id)
        if os.environ.get("PAL_BUNSHIN_WEB_BROKER") == "1"
        else None
    )
    register_web_search_with_core(
        context,
        WebSearchService(
            repository=web_search_repository,
            settings_repository=settings,
        ),
        query_delegate=broker_web.search if broker_web is not None else None,
    )
    register_web_fetch_with_core(
        context,
        WebFetchService(
            browser_manager=BrowserServiceManager(runtime_root=Path(runtime_root)),
        ),
        read_delegate=broker_web.read if broker_web is not None else None,
    )
    # All roles borrow the resident LSP manager, regardless of LLM authority.
    build_lsp_plugin(runtime_root=Path(runtime_root), client_only=True).register_with_core(context)
    module_ids = (
        "execution",
        "artifact",
        "memory",
        "skill",
        "web_search",
        "web_fetch",
        "lsp",
    )
    if role_memory is not None:
        module_ids += (role_memory.provider.module_id,)
    for module_id in module_ids:
        lifecycle.publish_module_capabilities(module_id)

    closer = RuntimeCleanup(role_memory=role_memory, llm_runtime=llm_runtime, context=context, database=database)

    bundle = BunshinRuntimeBundle(
        llm_runtime=llm_runtime,
        execution_runtime=context.execution_runtime,
        memory_service=memory_service,
        module_registry=context.module_registry,
        runtime_state_coordinator=RuntimeSnapshotCoordinator(context.module_registry),
        config=config,
        close_async=closer.close,
        memory_generation_id=role_memory.provider.repository.generation_id if role_memory is not None else "",
    )

    cleanup.pop_all()
    return bundle


async def _bunshin_noop_failure_handler(*args: Any, **kwargs: Any) -> _BunshinFailureResult:
    _ = args
    _ = kwargs
    return _BunshinFailureResult(user_feedback="bunshin turn failed before a normal reply could be produced")


def build_role_llm(
    *, llm_authority: Literal["manager_proxy", "host", "none"], runtime_root: Path, run_id: str,
    llm_repository: LLMEndpointRepository, settings: RuntimeSettingRepository, config: RuntimeConfig,
    max_output_tokens_override: int | None = None,
) -> LLMRuntime | None:
    if llm_authority == "manager_proxy":
        endpoint_resolver = BunshinEndpointResolver(
            repository=llm_repository, max_output_tokens_override=max_output_tokens_override,
        )
        local_settings = RuntimeSettingSnapshot(
            settings,
            endpoint_ids=tuple(
                endpoint.endpoint_id for endpoint in endpoint_resolver.endpoints
            ),
        )
        llm_runtime = LLMRuntime(
            endpoint_resolver=endpoint_resolver,
            settings_repository=local_settings,  # type: ignore[arg-type]
            endpoint_invoker=ShapeEndpointInvoker(
                transport=ManagerProxyTransport(
                    runtime_root=Path(runtime_root),
                    run_id=run_id,
                )
            ),
            config=config,
        )
    elif llm_authority == "host":
        llm_runtime = LLMRuntime(
            endpoint_resolver=BunshinEndpointResolver(
                repository=llm_repository, max_output_tokens_override=max_output_tokens_override,
            ),
            settings_repository=settings,
            endpoint_invoker=build_default_endpoint_invoker(
                credentials=LLMCredentialResolver(secret_store=EncryptedFileSecretStore(secrets_path=str(Path(runtime_root) / "secrets.json"))),
                runtime_root=runtime_root,
            ),
            config=config,
        )
    else:
        llm_runtime = None
    return llm_runtime


class RuntimeCleanup:
    """One cleanup owner; only unsuccessful actions remain eligible for retry."""

    def __init__(self, *, role_memory: RoleMemoryRuntime | None, llm_runtime: LLMRuntime | None,
                 context: MainContext, database: PalV2Database) -> None:
        self.context = context
        self._task: asyncio.Task[None] | None = None
        self._pending: dict[str, tuple[Callable[[], None] | None, Callable[[], Awaitable[None]] | None]] = {}
        if role_memory is not None:
            self._pending["memory"] = (role_memory.close, None)
        if llm_runtime is not None:
            self._pending["llm"] = (llm_runtime.close, None)
        for name, handle in context.module_registry.modules.items():
            self._pending[f"module:{name}"] = (handle.shutdown_sync, handle.shutdown_async)
        self._pending["database"] = (database.close, None)

    async def close(self) -> None:
        if (self._task is None or (self._task.done()
                and (self._task.cancelled() or self._task.exception() is not None))):
            self._task = asyncio.create_task(self._close(), name="bunshin-runtime-close")
        await join_owned_task(self._task)

    async def _close(self) -> None:
        async with self.context.execution_runtime.lifecycle_gate.write_async():
            failures: list[Exception] = []
            for name, (sync, asynchronous) in tuple(self._pending.items()):
                try:
                    if asynchronous is not None:
                        await asynchronous()
                    elif sync is not None:
                        sync()
                except Exception as exc:
                    failures.append(exc)
                else:
                    self._pending.pop(name)
            if failures:
                raise ExceptionGroup("bunshin runtime shutdown failed", failures)


def close_registered_modules(context):
    # Construction has not started asynchronous module work. Every owning
    # resource has its synchronous close registered before publication begins.
    with ExitStack() as cleanup:
        for module_id, handle in context.module_registry.modules.items():
            if module_id != "execution" and handle.shutdown_sync is not None:
                cleanup.callback(handle.shutdown_sync)
