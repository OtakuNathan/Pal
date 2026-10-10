from __future__ import annotations
import os
import tempfile
from pathlib import Path
from typing import Any, Literal
from pal.artifact import ArtifactManager, ArtifactRepository, register_with_core as register_artifact_with_core
from pal.core import CoreRuntimeState, MainContext
from pal.core.module_lifecycle import ModuleLifecycle
from pal.core.runtime_config import RuntimeConfig
from pal.core.runtime_state import RuntimeSnapshotCoordinator
from pal.execution import register_with_core as register_execution_with_core
from pal.foundation import PalV2Database
from pal.llm import LLMEndpointRepository, LLMRuntime, LLMCredentialResolver, RuntimeSettingRepository, build_default_endpoint_invoker
from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.repository import RuntimeSettingSnapshot
from pal.llm.secret_store import EncryptedFileSecretStore
from pal.lsp import build_lsp_plugin
from pal.bunshin.ipc import BUNSHIN_RUNTIME_DB_PATH_ENV
from pal.memory import L3ProviderSelector, MemoryService, build_ollama_embedding_provider_from_config, register_with_core as register_memory_with_core
from pal.skill import SkillRepository, SkillService, register_with_core as register_skill_with_core
from pal.skill.repository import ReadOnlySkillRepository
from pal.bunshin.llm_transport import ManagerProxyTransport
from pal.bunshin.llm_output_budget import BunshinEndpointResolver
from pal.bunshin.web_broker import BunshinBrokerWebClient
from pal.plugins.l3 import SQLiteVecL3Plugin, register_with_core as register_l3_with_core
from pal.web_fetch import BrowserServiceManager, WebFetchService, register_with_core as register_web_fetch_with_core
from pal.web_search import WebSearchProviderRepository, WebSearchService, register_with_core as register_web_search_with_core
from pal.wizard.runtime import ALL_MODELS, DEFAULT_LLM_ENDPOINTS, DEFAULT_WEB_SEARCH_PROVIDERS
from pal.bunshin.runner_components.models import BunshinRuntimeBundle
from pal.bunshin.runner_components.models import _BunshinFailureResult


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
    L3 is read-only in both modes; Bunshin memory candidates use the isolated
    in-memory sink owned by the logical role lifecycle.
    """

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
    register_execution_with_core(context)
    from pal.execution.worker_extensions import activate_worker_extension
    activate_worker_extension(context, Path(runtime_root), database_path=configured_db_path or None)
    register_artifact_with_core(context, artifact_service)
    memory_service = MemoryService(
        l3_selector=L3ProviderSelector(
            resolver=context.execution_runtime.l3_plugin_registry.require,
            active_provider_id="sqlite_vec_l3",
        ),
    )
    register_memory_with_core(context, memory_service)
    register_skill_with_core(
        context,
        SkillService(
            repository=ReadOnlySkillRepository() if read_only_database else SkillRepository(),
            runtime_root=Path(runtime_root),
        ),
    )
    memory_repository_args = {}
    from pal.memory.storage import MemoryStorage
    memory_storage = MemoryStorage(Path(runtime_root), read_only=True)
    if memory_storage.catalog_path.exists():
        # Workers never choose current. Only the host can create a durable pin;
        # a missing recovered binding must fail instead of switching versions.
        memory_repository_args["repository"] = memory_storage.open(
            memory_storage.pinned(memory_workflow_id), read_only=True)
    l3_plugin = SQLiteVecL3Plugin(
        service=memory_service,
        embedding_provider=build_ollama_embedding_provider_from_config(config),
        read_only=True,
        **memory_repository_args,
    )
    memory_service.l3_selector.active_provider_id = l3_plugin.provider_id
    register_l3_with_core(context, l3_plugin)
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
    for module_id in (
        "execution",
        "artifact",
        "memory",
        "skill",
        l3_plugin.module_id,
        "web_search",
        "web_fetch",
        "lsp",
    ):
        lifecycle.publish_module_capabilities(module_id)

    async def close() -> None:
        await close_role_runtime(memory_repository_args=memory_repository_args, l3_plugin=l3_plugin,
                                 llm_runtime=llm_runtime, context=context, database=database)

    return BunshinRuntimeBundle(
        llm_runtime=llm_runtime,
        execution_runtime=context.execution_runtime,
        memory_service=memory_service,
        module_registry=context.module_registry,
        runtime_state_coordinator=RuntimeSnapshotCoordinator(context.module_registry),
        config=config,
        close_async=close,
        memory_generation_id=l3_plugin.repository.generation_id,
    )


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


async def close_role_runtime(
    *, memory_repository_args: dict[str, Any], l3_plugin: SQLiteVecL3Plugin, llm_runtime: LLMRuntime | None,
    context: MainContext, database: PalV2Database,
) -> None:
    failures: list[Exception] = []
    if memory_repository_args:
        try:
            l3_plugin.repository.close()
        except Exception as exc:
            failures.append(exc)
    close_llm = getattr(llm_runtime, "close", None)
    if callable(close_llm):
        try:
            close_llm()
        except Exception as exc:
            failures.append(exc)
    for handle in tuple(context.module_registry.modules.values()):
        shutdown_async = getattr(handle, "shutdown_async", None)
        shutdown_sync = getattr(handle, "shutdown_sync", None)
        try:
            if callable(shutdown_async):
                await shutdown_async()
            elif callable(shutdown_sync):
                shutdown_sync()
        except Exception as exc:
            failures.append(exc)
    try:
        database.close()
    except Exception as exc:
        failures.append(exc)
    if failures:
        raise ExceptionGroup("bunshin runtime shutdown failed", failures)

