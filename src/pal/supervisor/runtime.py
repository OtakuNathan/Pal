from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pal.behavior import BehaviorAffordanceModel, BehaviorSkillModel
from pal.channel import ChannelEndpointRepository
from pal.channel.endpoints import DEFAULT_SOCKET_FILENAME
from pal.foundation import PalV2Database
from pal.identity import IdentityRepository
from pal.identity.models import PalPersonaModel, PalStateModel, UserPreferencesModel
from pal.llm import LLMEndpointRepository, RuntimeSettingRepository
from pal.llm.models import LLMEndpointModel, PalRuntimeSettingModel
from pal.memory import MemoryCaseModel, MemoryEmbeddingModel, MemoryEmbeddingVecModel, MemoryFactModel, MemoryTopicModel
from pal.plugins import PluginBundleModel
from pal.service import ServiceDefinitionModel, ServiceRunModel
from pal.web_fetch import WebFetchProviderModel, WebFetchProviderRepository
from pal.web_search import WebSearchProviderModel, WebSearchProviderRepository
from pal.channel.models import ChannelEndpointModel
from pal.supervisor.contracts import PalRegistration, ProvisionedRuntime, RuntimeLaunchSpec, SupervisorServicePort


ALL_MODELS = (
    PalPersonaModel,
    UserPreferencesModel,
    PalStateModel,
    ChannelEndpointModel,
    LLMEndpointModel,
    PalRuntimeSettingModel,
    MemoryFactModel,
    MemoryCaseModel,
    MemoryTopicModel,
    MemoryEmbeddingModel,
    MemoryEmbeddingVecModel,
    PluginBundleModel,
    ServiceDefinitionModel,
    ServiceRunModel,
    WebSearchProviderModel,
    WebFetchProviderModel,
    BehaviorAffordanceModel,
    BehaviorSkillModel,
)

DEFAULT_DB_FILENAME = "pal.sqlite3"
DEFAULT_PAL_ENTRYPOINT = "pal.main"


def default_channel_endpoints(runtime_root: Path) -> tuple[dict[str, object], ...]:
    return (
        {
            "endpoint_id": "socket_default",
            "channel_kind": "socket",
            "binding_key": str(runtime_root / DEFAULT_SOCKET_FILENAME),
            "enabled": True,
            "supports_typing": False,
            "supports_receipt_marker": False,
            "binding_metadata": {},
            "send_policy_blob": {},
        },
    )

DEFAULT_LLM_ENDPOINTS = (
    {
        "endpoint_id": "stub_llm_default",
        "provider": "stub",
        "model_id": "stub-model",
        "display_name": "Stub LLM",
        "api_mode": "openai_chat",
        "base_url": "stub://local/llm",
        "credential_ref": "stub-credential",
        "context_window": 8192,
        "max_output_tokens": 1024,
        "supports_reasoning": False,
        "supports_tools": True,
        "supports_streaming": False,
        "supports_vision": False,
        "input_modalities_blob": ["text"],
        "output_modalities_blob": ["text"],
        "priority": 0,
        "enabled": True,
        "capabilities_blob": {"stub": True},
        "notes": "Seeded stub endpoint for local runtime composition.",
    },
)

DEFAULT_WEB_SEARCH_PROVIDERS = (
    {
        "provider_id": "brave_search_default",
        "provider_kind": "brave_search",
        "display_name": "Brave Search",
        "enabled": True,
        "priority": 0,
        "settings_blob": {},
        "auth_material_blob": {},
        "notes": "Primary web search provider.",
    },
    {
        "provider_id": "duckduckgo_search_default",
        "provider_kind": "duckduckgo_search",
        "display_name": "DuckDuckGo Search",
        "enabled": True,
        "priority": 10,
        "settings_blob": {},
        "auth_material_blob": {},
        "notes": "Best-effort web search fallback provider.",
    },
)

DEFAULT_WEB_FETCH_PROVIDERS = (
    {
        "provider_id": "playwright_fetch_default",
        "provider_kind": "playwright_fetch",
        "display_name": "Playwright Fetch",
        "enabled": True,
        "priority": 0,
        "settings_blob": {"idle_timeout_seconds": 60, "max_concurrency": 2},
        "auth_material_blob": {},
        "notes": "Primary rendered web fetch provider.",
    },
    {
        "provider_id": "plain_http_fetch_default",
        "provider_kind": "plain_http_fetch",
        "display_name": "Plain HTTP Fetch",
        "enabled": True,
        "priority": 10,
        "settings_blob": {},
        "auth_material_blob": {},
        "notes": "Plain HTTP fallback provider.",
    },
)


@dataclass
class SupervisorService(SupervisorServicePort):
    registrations: list[PalRegistration] = field(default_factory=list)

    def register(self, registration: PalRegistration) -> None:
        self.registrations.append(registration)

    def provision_runtime(
        self,
        *,
        display_name: str,
        runtime_root: Path,
        db_filename: str,
        pal_entrypoint: str,
    ) -> PalRegistration:
        runtime_root.mkdir(parents=True, exist_ok=True)
        (runtime_root / "plugins").mkdir(parents=True, exist_ok=True)
        registration = PalRegistration(
            display_name=display_name,
            runtime=RuntimeLaunchSpec(
                db_path=runtime_root / db_filename,
                runtime_root=runtime_root,
                pal_entrypoint=pal_entrypoint,
            ),
        )
        self.register(registration)
        return registration

    def create_database(
        self,
        registration: PalRegistration,
    ) -> PalV2Database:
        # Supervisor owns the on-disk runtime association. Bootstrap only
        # composes in-process services from the already-associated database.
        database = PalV2Database(db_path=registration.runtime.db_path)
        database.initialize(ALL_MODELS)
        return database

    def seed_defaults(self, registration: PalRegistration) -> None:
        # Supervisor owns first-run provisioning. Initial persona and endpoint
        # truth are written before Pal composes its in-process runtime.
        _ = registration
        IdentityRepository().ensure_defaults()
        channel_repository = ChannelEndpointRepository()
        for payload in default_channel_endpoints(registration.runtime.runtime_root):
            channel_repository.upsert(**dict(payload))
        LLMEndpointRepository().ensure_defaults(DEFAULT_LLM_ENDPOINTS)
        WebSearchProviderRepository().ensure_defaults(DEFAULT_WEB_SEARCH_PROVIDERS)
        WebFetchProviderRepository().ensure_defaults(DEFAULT_WEB_FETCH_PROVIDERS)
        settings = RuntimeSettingRepository()
        settings.ensure_defaults()
        if settings.get("active_web_search_provider_id") is None:
            settings.set("active_web_search_provider_id", "brave_search_default")
        if settings.get("active_web_fetch_provider_id") is None:
            settings.set("active_web_fetch_provider_id", "playwright_fetch_default")

    def provision_stub_runtime(self, runtime_root: Path) -> ProvisionedRuntime:
        registration = self.provision_runtime(
            display_name="PalV2 Stub",
            runtime_root=runtime_root,
            db_filename=DEFAULT_DB_FILENAME,
            pal_entrypoint=DEFAULT_PAL_ENTRYPOINT,
        )
        database = self.create_database(registration)
        self.seed_defaults(registration)
        return ProvisionedRuntime(registration=registration, database=database)
