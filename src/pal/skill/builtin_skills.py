from __future__ import annotations

from pal.skill.contracts import SkillApplicabilitySTAR, SkillDescriptor


PAL_PLUGIN_DEVELOPMENT_SKILL_ID = "pal.plugin.development"
PAL_LLM_MODEL_HOOK_ENDPOINT_DEVELOPMENT_SKILL_ID = "pal.llm.model_hook_endpoint.development"
PAL_CHANNEL_PROVIDER_DEVELOPMENT_SKILL_ID = "pal.channel.provider.development"


PAL_PLUGIN_DEVELOPMENT_MANUAL = """# Pal Plugin Development

Use this skill when Pal needs to create, review, repair, or explain a Pal plugin and cannot rely on direct source-code access.

## Goal

A Pal plugin is a sidecar extension that adds capabilities, prompt fragments, providers, event sources, event handlers, or control handlers without editing Pal core. Prefer a plugin when the requested feature is optional, detachable, hot-refreshable, or owned by a domain boundary outside core.

Do not make a plugin for behavior that belongs in the runtime bus itself, the shared message contracts, or the security/control plane. Core should stay a coordinator; plugins should own feature behavior.

## Community Plugin Layout

Put community plugins under the runtime root:

```text
<runtime_root>/plugins/community/<plugin_id>/
  plugin.toml
  runtime.py
  capabilities.py        # optional, but recommended for capability providers
  README.md              # optional human notes
```

Use a unique `plugin_id`. Avoid reusing first-party IDs.

Minimal `plugin.toml`:

```toml
plugin_id = "demo_tools"
entrypoint = "runtime"
version = "0.1.0"
enabled_by_default = true
```

Optional:

```toml
reload_modules = ["runtime", "capabilities"]
```

For community plugins, Pal clears modules loaded from the plugin directory during refresh. `reload_modules` is still useful when the plugin imports helper modules through stable names.

## Runtime Entrypoint

The entrypoint module must expose `build_plugin`. Pal calls it by name-aware dependency injection. Supported argument names include:

- `context`: a `PluginBuildContext` containing `runtime_root`, `services`, and `plugin_dir`.
- `runtime_root`: the Pal runtime root path.
- `plugin_dir`: this plugin directory.
- any service name present in the plugin host service map, such as `memory_service`.

Example `runtime.py`:

```python
from dataclasses import dataclass

from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from capabilities import DemoProvider


@dataclass
class DemoPluginBundle:
    plugin_id: str = "demo_tools"
    version: str = "0.1.0"

    def register_with_core(self, context):
        provider = DemoProvider()
        handle = ModuleHandle(
            module_id="demo_tools",
            tier=MODULE_TIER_DETACHABLE,
            detachable=True,
            introspection_provider=provider,
            supports_lifecycle_capabilities=True,
        )
        context.register_module(handle)
        return handle


def build_plugin(context=None, runtime_root=None, plugin_dir=None):
    return DemoPluginBundle()
```

## Capability Provider

Expose operations by combining `@capability_node` on the provider class and `@capability_action` on methods. Use `CapabilityCall` for operation calls and return `CapabilityResult` with non-empty `llm_text`.

Example `capabilities.py`:

```python
from dataclasses import dataclass

from pal.execution.contracts import CapabilityCall, CapabilityResult
from pal.execution.tool_facade import StrictToolModel
from pal.execution.tool_semantics import INDIRECT_NONE
from pal.shared import OPERATION_NAMESPACE, RuntimeStatus, capability_action, capability_node


class EchoInput(StrictToolModel):
    message: str


@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="module",
    kind="module",
    source="plugin:demo_tools",
    target_kind="module",
)
@dataclass
class DemoProvider:
    module_id: str = "demo_tools"

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="demo",
        action_name="echo",
        guidance=ToolGuidance(
            purpose="Echo a short message back. Demo/test capability.",
            use_when="Testing capability routing or verifying the skill module is responsive.",
            do_not_use_when="Any real task — this is a demo tool only.",
            failure_next_steps="No failure modes. If echo doesn't work, the skill module may be detached.",
        ),
        InputModel=EchoInput,
        execution=INDIRECT_NONE,
    )
    def echo(self, call: CapabilityCall) -> CapabilityResult:
        message = str(call.args.get("message") or "")
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text=message,
            structured={"message": message},
            llm_text=f"Echo: {message}",
        )
```

The capability must declare exactly one public alias. Discover that alias with `search_tools` instead of guessing.

## Registering Other Surfaces

`ModuleHandle` is the plugin's contract with core:

- `introspection_provider`: capability provider.
- `prompt_fragment_providers`: prompt fragments owned by this module.
- `event_sources`: event sources owned by this module.
- `event_handlers`: event handlers keyed by event kind.
- `control_action_handlers`: deterministic control action handlers.
- `provider_refs`: named providers that execution runtime may route to.
- `ports`: internal service ports exposed as `<module_id>:<port_name>`.
- `cleanup_callbacks`: cleanup functions called on detach.

Keep each surface owned by the plugin's `module_id` so detach can withdraw the full subtree.

## Lifecycle and Hot Refresh

Attach, detach, and reattach must be clean:

1. Build a fresh bundle through `build_plugin`.
2. Return a `ModuleHandle` whose module ID is stable.
3. Let core/plugin host publish and withdraw capabilities; do not manually mutate the compiled index.
4. Put external resources in `cleanup_callbacks` or provider `detach` methods.
5. After code changes, use plugin detach/attach or core module reattach to load a fresh instance.

Useful operations:

- `plugin_rescan`: discover plugin manifests.
- `plugin_attach`: attach or refresh a plugin.
- `plugin_detach`: detach a plugin.
- `plugin_enable`: enable and attach a disabled plugin.
- `search_tools`: find capabilities after attach.
- `call_tool`: call a capability by discovered name.

## Safety and Product Rules

- Do not ask the user to do work Pal can do with existing capabilities.
- Do not bypass approval, access, identity, or constitutional boundaries.
- For high-risk actions, destructive filesystem changes, credentials, public network changes, or persistent system changes, route through the control/approval path.
- Keep large outputs in files or artifacts. Return short summaries and file paths.
- Prefer structured APIs over fragile string scraping.
- Keep plugin state in plugin-owned storage or the runtime root, not in Pal core globals.
- Use stable, minimal capability schemas. A small predictable tool beats a broad ambiguous one.
- Treat hardware, local devices, OS services, subprocesses, and network listeners as high-risk side effects. They must be behind explicit capability actions, admission checks, and approval policy when appropriate.
- `build_plugin` must not start unmanaged background work, touch hardware, mutate secrets, or perform irreversible I/O. It should construct providers and register lifecycle-owned resources only.
- Any sidecar, device handle, watcher, thread, task, or subprocess must be owned by the plugin lifecycle and stopped through `cleanup_callbacks` or provider detach. Detach must leave no live worker behind.
- Never leak credentials, raw device identifiers, tokens, private paths, or full hardware state into introspection. Expose minimal health/status fields and structured errors.

## Verification Checklist

Before calling the plugin done:

1. `plugin_rescan` sees the manifest with no scan errors.
2. `plugin_attach` returns ok and the plugin record is attached.
3. `search_tools` finds the new capability or prompt/source surface.
4. A representative capability call returns structured data and useful `llm_text`.
5. Detach removes the capability from discovery.
6. Attach again creates a fresh instance and restores the capability.
7. The plugin does not require the user to run commands manually unless approval or credentials are genuinely needed.
"""


PAL_LLM_MODEL_HOOK_ENDPOINT_DEVELOPMENT_MANUAL = """# Pal LLM Model Hook and Endpoint Development

Use this skill when Pal needs to add, review, repair, or explain an LLM endpoint or exact-model request hook.

## Boundary

Wire rendering belongs to Pal's three built-in shape codecs. A hook may tune one exact model; it must not select transports, providers, credentials, or endpoints.

The production refresh/load step is user-controlled. Prepare files and isolated tests without switching the active endpoint unless the user asks.

## Runtime Hook Location

Put runtime-root model-hook source under:

```text
<runtime_root>/llm/models/
```

Supported layouts:

```text
<runtime_root>/llm/models/my_exact_model.py
```

Each module exports a `MODEL_HOOK` for one exact `model_id`:

```python
from pal.llm import ModelHook

MODEL_HOOK = ModelHook(
    model_id="my-exact-model",
    developer_instructions=("Follow this model-specific instruction.",),
)
```

## Hook Contract

Keep hooks small and deterministic. They may add developer instructions or use `adjust_messages(messages)` and `adjust_tools(tools)` to replace only those immutable IR tuples. Exact `model_id` equality is the only match rule. Generation policy, endpoint, provider, credential, wire shape, routing metadata, and every other request field are read-only. Hooks must not perform network calls, read secrets, or mutate databases.

## Endpoint Row

Create or update endpoint metadata only after checking the existing endpoint list. Required fields include:

- `endpoint_id`: stable unique ID, such as `my_provider_gpt_5`.
- `provider`: display, credential, and telemetry identity only.
- `model_id`: exact model identifier used by model-hook lookup.
- `wire_shape`: exactly `openai_completion`, `openai_response`, or `anthropic_messages`.
- `base_url`: provider base URL, without credentials.
- `auth_kind`: `api_key_ref`, `oauth`, or `local_provider_auth`.
- `credential_ref`: secret reference such as `my_provider:api-key`; never store the key in the model hook.
- capability flags: `supports_tools`, `supports_streaming`, `supports_vision`, and optional `capabilities_blob`.
- `thinking_levels_blob` and `default_thinking_level`: explicit endpoint-supported enum values.

Prefer preparing a clear endpoint patch or SQL preview when no dedicated endpoint-management capability exists. Mutating the production database or secrets requires explicit user approval.

## Verification Workflow

Before asking the user to refresh:

1. Inspect `pal.llm.model_hooks.ModelHook` and the endpoint row.
2. Write the exact-model hook under the runtime-root model directory.
3. Compile it with `python -m py_compile <hook_file>`.
4. Load it through `ModelHookRegistry` in an isolated test.
5. Build a representative `LLMRequestIR` and assert only messages or tool definitions change.
6. Confirm routing, provider, credential, and wire shape cannot change.
8. If endpoint metadata changes are needed, verify them against a temporary database or produce an exact patch for user approval.
9. Do not switch the active endpoint during development.

## Handoff

When the model hook and endpoint are ready, report:

- hook file path, exact model ID, and endpoint ID
- tests or smoke checks run
- endpoint/database/secret changes made or still pending
- any load errors found in isolated checks
- a clear statement that the running endpoint was not switched

If the user has not asked for a refresh, hand off with: "Please run `/refresh_llm_endpoint` when you want to load the verified endpoint and model hook." If the user explicitly asks you to refresh, use the normal LLM refresh path and report model-hook load errors.
"""


PAL_CHANNEL_PROVIDER_DEVELOPMENT_MANUAL = """# Pal Channel Provider Development

Use this skill when Pal needs to add, review, repair, or explain a channel integration, channel endpoint, or runtime-root channel provider.

## Boundary

Channel providers belong to the `channel` subsystem. They are not Pal plugins
and are not loaded from `site-packages`. The recovery socket is the only
concrete endpoint kept in Pal core. Every detachable provider is loaded from the
selected runtime root by `ChannelEndpointProviderManager`, which is the single
LLM/core-facing management entrypoint. A provider owns the concrete endpoint
lifecycle and endpoint-specific introspection.

Keep these boundaries:

- `channel_kind` is a persisted endpoint type discriminator used by `channel_endpoints`; it should not become the LLM-facing abstraction.
- `ChannelEndpointProviderManager` maps endpoint type keys to providers and dispatches attach, detach, restart, inspect, auth, backlog, and health by endpoint id.
- The provider decides how attach/detach/restart/introspection work for its channel.
- Endpoint implementations normalize ingress, send replies, report auth/health/backlog, and render any channel-specific interactions.
- Do not put channel transport logic in `core`, `control`, `llm`, or `memory`.

## Runtime Provider Location

Runtime-root channel providers live under:

```text
<runtime_root>/channel/providers/<provider_id>/
  provider.toml
  runtime.py
  README.md        # optional
```

Do not create a second discovery layout unless the channel manager explicitly supports it.
The provider directory is loaded as a Python package through `importlib`, so
helper modules should normally use relative imports.

Provider-owned mutable state belongs under:

```text
<runtime_root>/data/channel/<endpoint_id>/
```

Use `context.endpoint_data_root(record.endpoint_id)` to derive it. Keep only the
thin endpoint registration, lifecycle projection, binding key, and management
metadata in Pal's central channel repository. Native callback maps, sidecar
sockets, checkpoints, and other provider-private state stay in the provider's
endpoint data directory.

Minimal `provider.toml`:

```toml
provider_id = "demo_chat"
entrypoint = "runtime.py"
version = "0.1.0"
enabled = true

reload_modules = ["runtime"]
```

`provider_id` must be stable and unique. `entrypoint` must point to a Python file inside the provider directory. `enabled = false` keeps the provider discoverable on disk but not loaded. `reload_modules` is advisory for endpoint restart and hot-refresh diagnostics.

## Provider Entrypoint

The entrypoint should expose `build_channel_provider`. Pal calls it with name-aware injection. Useful argument names are:

- `context`: `ChannelProviderBuildContext`
- `manager`: the `ChannelEndpointProviderManager`
- `runtime_root`: the Pal runtime root
- `provider_dir`: this provider directory
- `manifest`: the parsed `RuntimeChannelProviderManifest`

Minimal `runtime.py` using `FactoryChannelProvider`:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pal.channel import ChannelEndpointQueueBase, EndpointConfig, FactoryChannelProvider
from pal.channel.models import ChannelEndpointModel


class DemoChatEndpoint(ChannelEndpointQueueBase):
    def normalize_raw(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            return {"text": str(payload.get("text") or "")}
        return {"text": str(payload)}

    def send_reply(self, response_handle, text: str) -> None:
        # Replace this with the transport send path for the channel.
        _ = response_handle, text

    def inspect_health(self) -> dict[str, Any]:
        return {
            "healthy": True,
            "channel_kind": self.endpoint.channel_kind,
            "endpoint_id": self.endpoint.endpoint_id,
        }

    def inspect_auth_state(self) -> dict[str, Any]:
        return {
            "authorized": bool(self.paired),
            "endpoint_id": self.endpoint.endpoint_id,
        }


@dataclass(frozen=True)
class DemoChatEndpointFactory:
    channel_kind: str = "demo_chat"
    reload_modules: tuple[str, ...] = ("runtime",)

    def create(self, record: ChannelEndpointModel, *, runtime_root: Path):
        _ = runtime_root
        endpoint = DemoChatEndpoint(
            endpoint=EndpointConfig(
                endpoint_id=record.endpoint_id,
                channel_kind=record.channel_kind,
                binding_key=record.binding_key,
                send_policy=dict(record.send_policy_blob or {}),
            )
        )
        endpoint.enabled = bool(record.enabled)
        endpoint.attached = record.detached_at is None
        endpoint.paired = bool((record.binding_metadata or {}).get("paired", False))
        return endpoint


def build_channel_provider(context):
    factory = DemoChatEndpointFactory()
    return FactoryChannelProvider(
        provider_id=context.manifest.provider_id,
        endpoint_types=(factory.channel_kind,),
        factory=factory,
        reload_modules=factory.reload_modules,
    )
```

Use a custom `ChannelProvider` implementation instead of `FactoryChannelProvider` when the channel needs provider-owned attach/detach semantics, external sidecars, multi-step auth, pairing flows, or nonstandard introspection.

## Endpoint Row

The provider only handles endpoint types it declares in `endpoint_types`. A usable endpoint still needs a `channel_endpoints` row:

- `endpoint_id`: stable unique id, such as `demo_chat_main`.
- `channel_kind`: endpoint type key, such as `demo_chat`; this must match the provider's endpoint type.
- `binding_key`: channel-specific target, path, chat id, workspace id, or account binding.
- `enabled`: true when Pal should hydrate the endpoint.
- `binding_metadata`: channel-owned structured metadata; do not store raw secrets unless the existing channel explicitly does so.
- `send_policy_blob`: optional delivery/chunking policy.

If there is no endpoint-management capability for the needed row, prepare a clear SQL or repository patch and ask before mutating the production database.

## Interactions and Commands

Control interactions are channel-neutral. Prefer the base `ChannelEndpointQueueBase` hooks first:

- `apply_control_catalog`
- `apply_interaction_status`
- `open_or_update_interaction`
- `resolve_interaction`
- `emit_interaction_result`

Only override channel-specific rendering, such as inline keyboards, slash-command menus, callback payloads, receipts, typing indicators, or transport-specific message editing. Avoid hard-coding Telegram-only assumptions into shared control or core code.

The shared contract is typed data, not UI widgets. Core/control may produce `InteractionMessageSpec`, `InteractionButtonSpec`, `InteractionResult`, status kinds, attachments, and text; the provider decides how those become inline keyboards, menus, edits, reactions, receipts, native commands, or no-op fallbacks. Do not introduce channel-specific callback payloads, button shapes, or slash-command parsing into `core`, `control`, `minion`, `llm`, or `memory`.

## Lifecycle and Hot Reload

Provider rescan means:

1. Scan `<runtime_root>/channel/providers/*/provider.toml`.
2. Load enabled providers not already registered.
3. Register provider ids and endpoint type ownership in `ChannelEndpointProviderManager`.
4. Optionally hydrate enabled, attached endpoint rows.
5. Republish channel introspection capabilities so LLM can see new endpoint ids.

Endpoint restart means rebuilding a runtime endpoint instance through its provider. Do not use endpoint restart as provider discovery.

Useful operations:

- `channel_provider_rescan`: discover runtime-root channel providers and optionally attach enabled endpoints.
- `channel_list`: list configured channel endpoints and provider ids.
- `channel_endpoint_inspect`: inspect one endpoint.
- `channel_endpoint_auth_state`: inspect authorization without revealing secrets.
- `channel_endpoint_health`: inspect network and delivery health.
- `channel_endpoint_backlog`: inspect queue sizes.
- `channel_attach`: attach one endpoint through its provider.
- `channel_detach`: detach one endpoint through its provider.
- `channel_reload_provider`: restart one endpoint runtime instance.

## Verification Workflow

Before calling a channel provider done:

1. Inspect current channel provider manager and endpoint contracts.
2. Write `provider.toml` and provider source under `<runtime_root>/channel/providers/<provider_id>/`.
3. Compile provider source with `python -m py_compile`.
4. Test provider loading in isolation or with a temporary runtime root first.
5. Add or preview the `channel_endpoints` row for the provider's endpoint type.
6. Run `channel_provider_rescan` and check `runtime_provider_load_errors`.
7. Verify `channel_list` shows `provider_id`.
8. Verify endpoint `inspect`, `auth_state`, `health`, and `backlog`.
9. Dogfood through the real channel if safe; for socket, send `/control` before sending LLM-consuming messages.
10. Detach and attach the endpoint once to prove provider-owned lifecycle is reversible.

## Safety Notes

- Do not expose tokens or secrets in introspection payloads.
- Do not start multiple long-polling instances for the same external account or Telegram bot token.
- Do not silently mutate production channel endpoint rows, credentials, or live polling state without user approval.
- Keep provider failures structured and visible through `runtime_provider_load_errors` or endpoint health.
- Prefer small deterministic transport adapters. Put large protocol clients or sidecars behind provider-owned lifecycle code.
- A provider that touches hardware, local IPC, OS devices, sensors, cameras, microphones, serial ports, GPIO, Bluetooth, or other privileged resources must make that ownership explicit in introspection and cleanly release the resource on detach/reload.
- Ingress should attach a provider-owned `control_scope_key` when it needs custom conversation grouping. Shared routing must consume that key without understanding the channel's internal identifiers.
"""


def builtin_declared_skills(*, module_id: str = "skill") -> tuple[SkillDescriptor, ...]:
    return (
        SkillDescriptor(
            skill_id=PAL_PLUGIN_DEVELOPMENT_SKILL_ID,
            module_id=module_id,
            title="Pal Plugin Development",
            summary="Develop, review, and hot-refresh Pal plugins without direct source-code access.",
            manual_text=PAL_PLUGIN_DEVELOPMENT_MANUAL,
            activation_terms=(
                "pal plugin",
                "plugin development",
                "develop plugin",
                "create plugin",
                "extend capability",
                "capability extension",
                "build_plugin",
                "ModuleHandle",
                "capability_node",
                "capability_action",
                "hot refresh",
            ),
            capability_refs=(
                "plugin_rescan",
                "plugin_attach",
                "plugin_detach",
                "plugin_enable",
                "search_tools",
                "call_tool",
            ),
            applicability_star=SkillApplicabilitySTAR(
                situation="Pal needs to extend itself through a plugin or understand an existing plugin boundary.",
                task="Create or repair a detachable plugin with capabilities and clean lifecycle behavior.",
                action="Use the plugin layout, build_plugin contract, ModuleHandle surfaces, and verification checklist.",
                result="The plugin attaches cleanly, exposes discoverable capabilities, and hot-refreshes without core edits.",
            ),
            use_when=(
                "Use when the user asks Pal to add a plugin, extend capabilities, repair plugin hot refresh, "
                "or explain how a Pal plugin should be structured."
            ),
            avoid_when="Avoid when the change clearly belongs in core runtime contracts or security policy.",
            source_format="internal_skill",
            source_refs=("pal.skill.builtin_skills",),
            metadata={"internal": True},
        ),
        SkillDescriptor(
            skill_id=PAL_LLM_MODEL_HOOK_ENDPOINT_DEVELOPMENT_SKILL_ID,
            module_id=module_id,
            title="Pal LLM Model Hook and Endpoint Development",
            summary="Develop and validate exact-model hooks and matching endpoint rows safely.",
            manual_text=PAL_LLM_MODEL_HOOK_ENDPOINT_DEVELOPMENT_MANUAL,
            activation_terms=(
                "llm endpoint",
                "model hook",
                "endpoint hook",
                "runtime model hook",
                "new model provider",
                "add llm provider",
                "llm/models",
                "refresh_llm_endpoint",
            ),
            capability_refs=(
                "llm_list",
                "llm_show",
                "llm_set_active_endpoint",
            ),
            applicability_star=SkillApplicabilitySTAR(
                situation="Pal needs to tune one exact model or add validated endpoint metadata.",
                task="Create or update a runtime-root exact-model hook and matching endpoint metadata without destabilizing PalCore.",
                action="Use the model-hook contract, endpoint checklist, isolated tests, and user-controlled refresh handoff.",
                result="The model hook and endpoint are ready to load, with tests completed and production refresh left to the user.",
            ),
            use_when=(
                "Use when the user asks Pal to add an LLM endpoint, add an exact-model request hook, "
                "or prepare a new model integration using one of Pal's built-in wire shapes."
            ),
            avoid_when=(
                "Avoid for Pal plugins, channel endpoints such as Telegram/socket, or ordinary endpoint switching that "
                "does not require model-hook code."
            ),
            source_format="internal_skill",
            source_refs=("pal.skill.builtin_skills", "docs/pal_llm_contract.md"),
            metadata={"internal": True, "requires_user_refresh": True},
        ),
        SkillDescriptor(
            skill_id=PAL_CHANNEL_PROVIDER_DEVELOPMENT_SKILL_ID,
            module_id=module_id,
            title="Pal Channel Provider Development",
            summary="Develop and validate runtime-root channel providers, endpoint rows, and channel-specific interactions.",
            manual_text=PAL_CHANNEL_PROVIDER_DEVELOPMENT_MANUAL,
            activation_terms=(
                "channel provider",
                "channel endpoint",
                "channel integration",
                "new channel",
                "add channel",
                "runtime channel provider",
                "channel/providers",
                "provider.toml",
                "ChannelEndpointProviderManager",
                "FactoryChannelProvider",
                "ChannelEndpointQueueBase",
                "telegram channel",
                "socket channel",
            ),
            capability_refs=(
                "channel_provider_rescan",
                "channel_list",
                "channel_endpoint_inspect",
                "channel_endpoint_auth_state",
                "channel_endpoint_health",
                "channel_endpoint_backlog",
                "channel_attach",
                "channel_detach",
                "channel_reload_provider",
            ),
            applicability_star=SkillApplicabilitySTAR(
                situation="Pal needs to add or repair a channel integration without pushing transport details into core.",
                task="Create a runtime-root channel provider, endpoint implementation, and matching endpoint metadata.",
                action="Use the provider.toml layout, build_channel_provider contract, lifecycle boundary, and dogfood checklist.",
                result="The channel provider rescans, hydrates endpoints, exposes provider-owned introspection, and can be attached or detached safely.",
            ),
            use_when=(
                "Use when the user asks Pal to create, review, repair, or hot-load a channel provider, channel endpoint, "
                "slash-command handling path, inline interaction rendering, or runtime-root channel integration."
            ),
            avoid_when=(
                "Avoid for ordinary Pal plugins, LLM model hooks, or changes that only switch an existing endpoint "
                "without adding provider or endpoint code."
            ),
            sanitization_notes=(
                "Provider secrets must remain write-only.",
                "Live polling or external account attachment needs explicit user approval.",
            ),
            source_format="internal_skill",
            source_refs=("pal.skill.builtin_skills", "src/pal/channel/README.md"),
            metadata={"internal": True, "runtime_root_layout": "channel/providers/<provider_id>/provider.toml"},
        ),
    )
