from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from pal.artifact import ArtifactHotStateModel, ArtifactRecordModel, ArtifactRepresentationModel
from pal.behavior import BehaviorAffordanceModel
from pal.channel import ChannelEndpointRepository
from pal.channel.endpoints import DEFAULT_SOCKET_FILENAME
from pal.foundation import PalV2Database
from pal.identity import IdentityRepository
from pal.identity.models import PalPersonaModel, UserPreferencesModel
from pal.llm import LLMEndpointRepository, RuntimeSettingRepository
from pal.llm.models import LLMEndpointModel, PalRuntimeSettingModel
from pal.memory import MemoryCaseModel, MemoryEmbeddingModel, MemoryEmbeddingVecModel, MemoryFactModel, MemoryTopicModel
from pal.plugins import PluginBundleModel
from pal.proactive import ProactiveDefinitionModel, ProactiveRunModel
from pal.skill import SkillModel
from pal.web_fetch import WebFetchProviderModel, WebFetchProviderRepository
from pal.web_search import WebSearchProviderModel, WebSearchProviderRepository
from pal.channel.models import ChannelEndpointModel
from pal.core.runtime_config import RuntimeConfig
from pal.wizard.contracts import PalRegistration, ProvisionedRuntime, RuntimeLaunchSpec, WizardServicePort


ALL_MODELS = (
    PalPersonaModel,
    UserPreferencesModel,
    ChannelEndpointModel,
    LLMEndpointModel,
    PalRuntimeSettingModel,
    MemoryFactModel,
    MemoryCaseModel,
    MemoryTopicModel,
    MemoryEmbeddingModel,
    MemoryEmbeddingVecModel,
    PluginBundleModel,
    ProactiveDefinitionModel,
    ProactiveRunModel,
    WebSearchProviderModel,
    WebFetchProviderModel,
    BehaviorAffordanceModel,
    SkillModel,
    ArtifactRecordModel,
    ArtifactRepresentationModel,
    ArtifactHotStateModel,
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
            "detached_at": None,
            "supports_typing": False,
            "supports_receipt_marker": False,
            "binding_metadata": {},
            "send_policy_blob": {},
        },
    )


def ensure_recovery_socket_channel(repository: ChannelEndpointRepository, runtime_root: Path) -> ChannelEndpointModel:
    payload = dict(default_channel_endpoints(runtime_root)[0])
    existing_binding = repository.find_by_binding(
        channel_kind=str(payload["channel_kind"]),
        binding_key=str(payload["binding_key"]),
    )
    if existing_binding is not None and existing_binding.endpoint_id != payload["endpoint_id"]:
        payload["endpoint_id"] = existing_binding.endpoint_id
    return repository.upsert(**payload)


def _choose_primary_wizard_channel(records: list[ChannelEndpointModel]) -> ChannelEndpointModel | None:
    attached = [record for record in records if record.detached_at is None]
    return (
        next((record for record in attached if record.channel_kind != "socket"), None)
        or next(iter(attached), None)
        or next((record for record in records if record.channel_kind != "socket"), None)
        or next(iter(records), None)
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
class WizardService(WizardServicePort):
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
        (runtime_root / "plugins" / "_builtin").mkdir(parents=True, exist_ok=True)
        (runtime_root / "plugins" / "community").mkdir(parents=True, exist_ok=True)
        registration = PalRegistration(
            display_name=display_name,
            runtime=RuntimeLaunchSpec(
                db_path=runtime_root / db_filename,
                runtime_root=runtime_root,
                pal_entrypoint=pal_entrypoint,
            ),
        )
        self.register(registration)
        self.provision_builtin_plugins(registration)
        self.provision_runtime_channel_providers(registration)
        return registration

    def create_database(
        self,
        registration: PalRegistration,
    ) -> PalV2Database:
        # Wizard owns the on-disk runtime association. Bootstrap only
        # composes in-process services from the already-associated database.
        database = PalV2Database(db_path=registration.runtime.db_path)
        database.initialize(ALL_MODELS)
        return database

    def seed_defaults(self, registration: PalRegistration) -> None:
        # Wizard owns first-run provisioning. Initial persona and endpoint
        # truth are written before Pal composes its in-process runtime.
        _ = registration
        IdentityRepository().ensure_defaults()
        channel_repository = ChannelEndpointRepository()
        ensure_recovery_socket_channel(channel_repository, registration.runtime.runtime_root)
        LLMEndpointRepository().ensure_defaults(DEFAULT_LLM_ENDPOINTS)
        WebSearchProviderRepository().ensure_defaults(DEFAULT_WEB_SEARCH_PROVIDERS)
        WebFetchProviderRepository().ensure_defaults(DEFAULT_WEB_FETCH_PROVIDERS)
        settings = RuntimeSettingRepository()
        settings.ensure_defaults()
        if settings.get("active_web_search_provider_id") is None:
            settings.set("active_web_search_provider_id", "brave_search_default")
        if settings.get("active_web_fetch_provider_id") is None:
            settings.set("active_web_fetch_provider_id", "playwright_fetch_default")

    def provision_builtin_plugins(self, registration: PalRegistration) -> None:
        from pal.plugins.host import _source_plugins_root

        source_root = _source_plugins_root()
        builtin_root = registration.runtime.runtime_root / "plugins" / "_builtin"
        builtin_root.mkdir(parents=True, exist_ok=True)

        for source_dir in sorted(source_root.iterdir()):
            if not source_dir.is_dir():
                continue
            manifest = source_dir / "plugin.toml"
            if not manifest.exists():
                continue
            plugin_id = source_dir.name
            target_dir = builtin_root / plugin_id
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(manifest, target_dir / "plugin.toml")

        sentinel = builtin_root / ".managed"
        if not sentinel.exists():
            sentinel.write_text("# Managed by Pal. Do not modify manually.\n", encoding="utf-8")

    def provision_runtime_channel_providers(
        self,
        registration: PalRegistration,
    ) -> None:
        """Seed missing detachable channel providers without overwriting local edits."""
        source_root = Path(__file__).resolve().parents[3] / "providers"
        if not source_root.is_dir():
            return
        target_root = registration.runtime.runtime_root / "channel" / "providers"
        target_root.mkdir(parents=True, exist_ok=True)
        for source_dir in sorted(source_root.iterdir()):
            if not source_dir.is_dir() or not (source_dir / "provider.toml").is_file():
                continue
            target_dir = target_root / source_dir.name
            if target_dir.exists():
                continue
            shutil.copytree(
                source_dir,
                target_dir,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )

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

    def load_existing_wizard_data(self, runtime_root: Path) -> object | None:
        """Read current runtime configuration as wizard defaults.

        This is intentionally best-effort: if the runtime does not exist or
        cannot be opened, setup should continue as a new configuration flow.
        """

        from pal.channel import ChannelEndpointRepository
        from pal.identity import IdentityRepository
        from pal.llm import LLMEndpointRepository, RuntimeSettingRepository
        from pal.wizard.prompts import WizardChannel, WizardCollectedData, WizardIdentity, WizardLLMEndpoint, WizardMemoryEmbedding

        runtime_root = Path(runtime_root)
        db_path = runtime_root / DEFAULT_DB_FILENAME
        if not db_path.exists():
            candidates = sorted(runtime_root.glob("*.sqlite3"))
            if len(candidates) != 1:
                return None
            db_path = candidates[0]
        database = PalV2Database(db_path=db_path)
        try:
            database.initialize(ALL_MODELS)
            identity_repo = IdentityRepository()
            persona = identity_repo.get_persona()
            preferences = identity_repo.get_user_preferences()
            identity = WizardIdentity(
                display_name=str(getattr(persona, "display_name", "") or "Pal"),
                language=str(getattr(persona, "language", "") or "en"),
                vibe=getattr(persona, "vibe", None),
                tone=getattr(persona, "tone", None),
                core_policy=list(getattr(persona, "core_policy", None) or []),
                timezone=str(getattr(preferences, "timezone", "") or ""),
            )

            endpoints = [
                WizardLLMEndpoint(
                    endpoint_id=endpoint.endpoint_id,
                    model_id=endpoint.model_id,
                    api_mode=endpoint.api_mode,
                    base_url=endpoint.base_url,
                    api_key=None,
                    context_window=endpoint.context_window,
                    max_output_tokens=endpoint.max_output_tokens,
                    supports_reasoning=endpoint.supports_reasoning,
                    supports_tools=endpoint.supports_tools,
                    supports_streaming=endpoint.supports_streaming,
                    supports_vision=endpoint.supports_vision,
                    priority=endpoint.priority,
                    provider=endpoint.provider,
                    auth_kind=endpoint.auth_kind,
                    credential_ref=endpoint.credential_ref,
                    capabilities_blob=dict(endpoint.capabilities_blob or {}),
                    notes=endpoint.notes,
                )
                for endpoint in LLMEndpointRepository().list_enabled()
            ]
            active_endpoint_id = RuntimeSettingRepository().get_active_llm_endpoint_id()
            if not active_endpoint_id and endpoints:
                active_endpoint_id = endpoints[0].endpoint_id

            channel_record = _choose_primary_wizard_channel(ChannelEndpointRepository().list_enabled())
            if channel_record is None:
                channel_payload = dict(default_channel_endpoints(Path(runtime_root))[0])
                channel = WizardChannel(
                    endpoint_id=str(channel_payload["endpoint_id"]),
                    channel_kind=str(channel_payload["channel_kind"]),
                    binding_key=str(channel_payload["binding_key"]),
                    binding_metadata=dict(channel_payload.get("binding_metadata") or {}),
                    supports_typing=bool(channel_payload.get("supports_typing")),
                    supports_receipt_marker=bool(channel_payload.get("supports_receipt_marker")),
                )
            else:
                channel = WizardChannel(
                    endpoint_id=channel_record.endpoint_id,
                    channel_kind=channel_record.channel_kind,
                    binding_key=channel_record.binding_key,
                    binding_metadata=dict(channel_record.binding_metadata or {}),
                    supports_typing=bool(channel_record.supports_typing),
                    supports_receipt_marker=bool(channel_record.supports_receipt_marker),
                )
            config = RuntimeConfig.load(runtime_root)
            memory_embedding = WizardMemoryEmbedding(
                remote_ollama_base_urls=list(config.embedding_ollama_remote_base_urls),
                model_name=config.embedding_ollama_model_name,
            )

            return WizardCollectedData(
                identity=identity,
                endpoints=endpoints,
                channel=channel,
                active_endpoint_id=active_endpoint_id or "",
                memory_embedding=memory_embedding,
            )
        except Exception:
            return None
        finally:
            database.close()

    def seed_from_wizard(self, registration: PalRegistration, collected: object) -> None:
        """Seed the database from wizard-collected data."""
        from pal.wizard.prompts import WizardCollectedData
        from pal.wizard.config_file import upsert_memory_embedding_config
        from pal.llm.secret_store import EncryptedFileSecretStore, SecretRef
        from pal.channel import ChannelEndpointRepository
        from pal.identity import IdentityRepository
        from pal.identity.repository import DEFAULT_PERSONA_ID
        from pal.llm import LLMEndpointRepository, RuntimeSettingRepository
        from pal.web_search import WebSearchProviderRepository
        from pal.web_fetch import WebFetchProviderRepository

        data: WizardCollectedData = collected
        runtime_root = registration.runtime.runtime_root

        # 1. Identity — ensure_defaults creates if missing, then upsert fields
        idata = data.identity
        id_repo = IdentityRepository()
        id_repo.ensure_defaults(
            display_name=idata.display_name,
            language=idata.language,
            vibe=idata.vibe,
            tone=idata.tone,
            core_policy=idata.core_policy or None,
            timezone=idata.timezone,
        )
        persona = id_repo.get_persona(DEFAULT_PERSONA_ID)
        if persona is not None:
            from pal.foundation import utc_now
            now = utc_now()
            persona.display_name = idata.display_name
            persona.language = idata.language
            persona.vibe = idata.vibe
            persona.tone = idata.tone
            persona.core_policy = list(idata.core_policy or ())
            persona.updated_at = now
            persona.save()
        id_repo.update_user_preferences(timezone=idata.timezone)

        # 2. LLM endpoints
        llm_repo = LLMEndpointRepository()
        secrets_path = runtime_root / "secrets.json"
        secret_store = EncryptedFileSecretStore(str(secrets_path))

        for ep in data.endpoints:
            provider = str(getattr(ep, "provider", "") or "").strip()
            base_url = str(ep.base_url or "")
            if provider:
                pass
            elif base_url.lower().startswith("codex://"):
                provider = "codex_cli"
            elif "anthropic" in ep.api_mode:
                provider = "anthropic"
            elif "openai" in ep.api_mode or ep.api_mode == "openai_chat":
                # Try to infer from base_url
                if "deepseek" in base_url:
                    provider = "deepseek"
                elif "zhipu" in base_url or "z.ai" in base_url or "bigmodel.cn" in base_url:
                    provider = "zhipu"
                elif "moonshot" in base_url or "kimi" in base_url:
                    provider = "moonshot"
                else:
                    provider = "openai"
            else:
                provider = ep.endpoint_id

            auth_kind = str(getattr(ep, "auth_kind", "") or "api_key_ref")
            credential_ref = getattr(ep, "credential_ref", None)
            if credential_ref is None:
                credential_ref = f"{ep.endpoint_id}:api-key" if auth_kind == "api_key_ref" else ""

            # Store API key in secret store
            if ep.api_key:
                secret_store.set_secret(
                    SecretRef(service=ep.endpoint_id, account="api-key"),
                    ep.api_key,
                )

            payload = {
                "endpoint_id": ep.endpoint_id,
                "provider": provider,
                "model_id": ep.model_id,
                "display_name": ep.endpoint_id,
                "api_mode": ep.api_mode,
                "base_url": base_url,
                "auth_kind": auth_kind,
                "credential_ref": credential_ref,
                "context_window": ep.context_window or 8192,
                "max_output_tokens": ep.max_output_tokens or 4096,
                "supports_reasoning": ep.supports_reasoning,
                "supports_tools": ep.supports_tools,
                "supports_streaming": ep.supports_streaming,
                "supports_vision": ep.supports_vision,
                "input_modalities_blob": (
                    ["text", "image"] if ep.supports_vision else ["text"]
                ),
                "output_modalities_blob": ["text"],
                "priority": ep.priority,
                "enabled": True,
                "capabilities_blob": dict(getattr(ep, "capabilities_blob", None) or {}),
                "notes": getattr(ep, "notes", None) or "Configured via setup wizard.",
            }
            llm_repo.upsert(**payload)

        # 3. Set active endpoint
        settings = RuntimeSettingRepository()
        settings.ensure_defaults()
        settings.set_active_llm_endpoint_id(data.active_endpoint_id)

        # 4. Channel
        ch = data.channel
        channel_repo = ChannelEndpointRepository()
        channel_payload: dict = {
            "endpoint_id": ch.endpoint_id,
            "channel_kind": ch.channel_kind,
            "binding_key": ch.binding_key,
            "enabled": True,
            "detached_at": None,
            "supports_typing": ch.supports_typing,
            "supports_receipt_marker": ch.supports_receipt_marker,
            "binding_metadata": ch.binding_metadata,
            "send_policy_blob": {},
        }
        channel_repo.upsert(**channel_payload)
        if not (ch.channel_kind == "socket" and ch.endpoint_id == "socket_default"):
            ensure_recovery_socket_channel(channel_repo, runtime_root)

        # 5. Web search/fetch defaults (same as seed_defaults)
        WebSearchProviderRepository().ensure_defaults(DEFAULT_WEB_SEARCH_PROVIDERS)
        WebFetchProviderRepository().ensure_defaults(DEFAULT_WEB_FETCH_PROVIDERS)
        if settings.get("active_web_search_provider_id") is None:
            settings.set("active_web_search_provider_id", "brave_search_default")
        if settings.get("active_web_fetch_provider_id") is None:
            settings.set("active_web_fetch_provider_id", "playwright_fetch_default")

        # 6. Runtime config owned by the wizard
        upsert_memory_embedding_config(
            runtime_root,
            remote_ollama_base_urls=data.memory_embedding.remote_ollama_base_urls,
            model_name=data.memory_embedding.model_name,
        )
