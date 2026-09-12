from __future__ import annotations

from pal.skill.contracts import SkillApplicabilitySTAR, SkillDescriptor


PAL_PLUGIN_DEVELOPMENT_SKILL_ID = "pal.plugin.development"
PAL_LLM_MODEL_HOOK_ENDPOINT_DEVELOPMENT_SKILL_ID = "pal.llm.model_hook_endpoint.development"
PAL_CHANNEL_PROVIDER_DEVELOPMENT_SKILL_ID = "pal.channel.provider.development"
PAL_SELF_MAINTENANCE_SKILL_ID = "pal.self.maintenance"


PAL_SELF_MAINTENANCE_MANUAL = """# Pal Self Maintenance

Use this entry manual to explain how to configure Pal, or to carry out requested
self-modification, repair, extension development, and deployment. Pal is its own
configuration documentation: answer the user directly in their language with the
relevant commands, parameter meanings, where to run them, when changes take
effect, and how to verify them. Read contracts and CLI help yourself; do not make
reading a README or skill a prerequisite for the user.

## Explain or execute

A question such as "how do I configure this?" calls for an explanation, not a
mutation. Give a concrete example with clearly identified placeholders. Inspect
current public settings when needed to tailor the answer, without exposing secrets.
A request to implement or configure calls for action within its authorized scope.
Carry forward explicit requests, approved plans, constraints, and completed checks;
do not ask for the same approval again. Diagnosis alone does not authorize repair.
Ask only for missing information or authorization for an uncovered action after
preparing the relevant diagnosis, patch, or command preview. Capability policy and
execution-time approval gates always apply; this manual grants no permission.

Interactive setup, secret entry, service registration, and full host restart may
need the user. Prepare the commands and explain the exact remaining step and its
expected result. Do not delegate work Pal can already perform within the request.
When the user only wants instructions, explain these operations without running them.

## Establish the target and inspect the change

Distinguish the source checkout, installed Pal package/interpreter, selected
runtime root, and running service. A checkout edit does not prove the installed or
running code changed. Use live introspection for runtime state, source inspection
for implementation, and current capability discovery for usable tools and inputs.
Confirm the actual CLI version's `--help` before using unfamiliar flags.

Use an explicit `--runtime-root` on runtime-specific CLI commands. The current CLI
prefers `~/.pal` if that directory exists, otherwise `PAL_HOME`, then `~/.pal`.
Do not assume exporting PAL_HOME selects a different instance when ~/.pal exists.
Resolve the actual service name and its executable/root before preparing a restart;
installations may use pal.service, a pal@... service, launchd, or a manual process.

For a Git checkout inspect `git status --short`, working-tree and staged diffs,
and targeted history when needed. For installed/runtime files without Git, review
the scoped before/after changes instead. Preserve unrelated user edits. Classify the
owner and choose the smallest change that meets the request. Recall relevant repair
lessons when useful and check them against current errors and code. For prompt
changes inspect system/developer fragments, reminders, tool guidance, and relevant
skills together to catch contradictory instructions.

## CLI map

Commands below describe the current interface; flags follow the subcommand.
There is no general `pal config`, `pal channel add`, or arbitrary `pal tool-call`.
Discover runtime capabilities separately with `search_tools`. Package `--kind`
accepts `plugin`, `provider`, or `builtin`.

| Command | Purpose and effect |
| --- | --- |
| `pal setup` (aliases `wizard`, `wizzard`) | Interactive initial setup or reconfiguration: identity, endpoints, channel, embeddings, and optional OS service registration. It writes multiple surfaces and can start or replace a service. Use the narrow existing operation for a narrow edit; explain the wizard to a user requesting interactive configuration. |
| `pal setup --check`, `pal doctor` | Check local dependencies and report remediation. These are diagnostic entrypoints, not configuration reloads. |
| `pal setup --upgrade` | Offline runtime upgrade: LLM/browser schema migration, Bunshin cutover and generation-owned memory database separation, potentially archiving old data. Requires the host to be stopped and holds its runtime lock. Re-running preserves the published memory generation and user configuration. It is not a hot refresh or ordinary settings editor. |
| `pal llm list [--all] [--json]` | Inspect persisted endpoint configuration, optionally including disabled rows; not proof that the running endpoint cache has refreshed. |
| `pal llm add ENDPOINT [--replace]` | Add or update endpoint metadata and optionally credentials/active selection. Replace preserves omitted fields. Refresh the running LLM afterwards. |
| `pal llm delete ENDPOINT` | Delete an endpoint; shared credentials remain. Deleting the active endpoint selects the next enabled endpoint or clears active selection. Refresh afterwards. |
| `pal package build SOURCE [--output DIR]` | Build a .palpkg from package.toml and a wheel project. Building does not install or activate it. |
| `pal package install PATH...` | Install/prepare .palpkg or legacy provider wheels offline. Publication is protected by the runtime lock; it cannot replace files under an active Pal. |
| `pal package prepare NAME [--kind KIND]` | Prepare dependencies/hooks for an installed package; `--all-builtin` prepares built-ins. Check status afterwards; preparation is not proof of runtime activation. |
| `pal package status [NAME] [--kind KIND]` | Inspect installation records and failures. Runtime `package_status` also reports asynchronous jobs. |
| `pal provider install WHEEL... [--force]` | Install channel-provider wheels offline; force permits reinstalling the same version. It shares the package runtime lock. |
| `pal run` | Start the host for the selected existing runtime. Do not start another copy against an already running runtime. |
| `pal client --message TEXT`, `pal tty` | Send a message or open an interactive connection to a running Pal. Disconnecting a client does not restart Pal. Slash commands are control requests, not arbitrary tool calls. |
| `pal eval tools` | Run a tool-usability evaluation using configured LLM endpoints; it can consume API usage. It is not a configuration command. |
| `pal bunshin efficiency WORKFLOW_ID [--json]` | Read-only workflow telemetry. It does not configure Bunshin. |
| `pal browser-service` | Internal browser sidecar entrypoint with host/port/token/runtime arguments. Normal browser maintenance uses the browser/plugin owner instead of manually launching this command. |

### LLM configuration and user cooperation

Use `pal llm` for endpoint metadata instead of ad hoc SQL when its options cover
the request. `add` supports model/provider/display name, wire shape/base URL,
auth kind/credential ref, context/output limits, supported thinking levels/default,
priority, tools/streaming/vision, enabled state, notes, and `--set-active`.
Boolean options accept `--no-...`; for example `--no-enabled` prepares a disabled
endpoint. Enabling and choosing an endpoint are separate choices. `--set-active`
requires an enabled endpoint. Do not invent flags for arbitrary capabilities_blob
fields that the CLI does not expose.

Runnable command shapes (replace the example values with verified user choices):

```sh
pal llm list --all --json --runtime-root /path/to/runtime
pal llm add example --model-id example-model --provider example-provider --wire-shape openai_completion --base-url https://api.example.com/v1 --store-api-key --no-enabled --runtime-root /path/to/runtime
pal llm add example --replace --enabled --set-active --runtime-root /path/to/runtime
pal llm delete example --runtime-root /path/to/runtime
pal doctor --runtime-root /path/to/runtime
pal setup --runtime-root /path/to/runtime
pal setup --upgrade --runtime-root /path/to/runtime
pal package build /path/to/plugin-project --output /path/to/dist
pal package install /path/to/plugin.palpkg --runtime-root /path/to/runtime
pal package prepare example --kind plugin --runtime-root /path/to/runtime
pal package status example --kind plugin --runtime-root /path/to/runtime
pal provider install /path/to/provider.whl --force --runtime-root /path/to/runtime
pal tty --runtime-root /path/to/runtime
```

`--store-api-key` prompts securely in the user's terminal; `--api-key-stdin`
reads a secret from stdin. Never place a secret in command arguments or expose it
in chat/logs. `--api-key-env ENV_VAR` sets a reference: the running service must
actually have that variable. Exporting it in a new shell does not update the host's
environment. If changing the service environment requires a restart, hand that step
to the operator. Stored credentials and model hooks can use the LLM refresh path.

After the configuration is written and checked, explain: "Send
`/refresh_llm_endpoint` in your conversation with Pal to load these changes."
This reloads endpoints, runtime settings, model hooks under `llm/models/`, and
credentials, and refreshes participating dependent runtimes. Inspect the result
for errors and the effective endpoint. Without an explicit refresh request, leave
that timing to the user; with one, use the existing control path without requesting
the same approval again. `pal client` can deliver the slash command to the selected
runtime when that is the available authorized path. A fresh process also loads it.
Do not claim this command reloads all config.toml settings or Python implementation.

Use `llm_list`, `llm_show`, and `llm_active` to verify running endpoint metadata.
`llm_set_active_endpoint` or `/model ENDPOINT` selects an already loaded enabled
endpoint; `/think LEVEL` changes the supported thinking choice. These affect future
requests, not a request already in flight. `/control` lists available controls;
`/status` reports runtime statistics. `/reset` resets the conversation, not the host.
`/log start` and `/log end` control diagnostic logging; `/compact` and `/interrupt`
are conversation controls, not configuration activation mechanisms.

## Choose how a change becomes effective

Resident modules are core, execution, llm, channel (including the recovery socket),
identity, memory, control, and failure. Resident means the whole module cannot be
unloaded; some of its data has explicit refresh paths. Optional modules such as
skill, behavior, checklist, proactive, artifact, Bunshin, MCP, LSP, sqlite_vec_l3,
and web integrations belong to plugins. Check actual availability before use.

| Surface | Change and activation | Verification / limits |
| --- | --- | --- |
| Resident Python implementation, shared contracts, core system prompt | Update the actual installed source/package, test, and hand off a full host restart. | Hot-loading a plugin does not reload its resident dependencies. Reconnect and verify after the external restart. |
| Identity in durable storage | Setup exposes name, language, vibe, tone, core policy, timezone. After an authorized external edit, `identity_show` refreshes the resident projection. | Language/tone/preferences update in subsequent prompt assembly; system name and core policy are captured at startup and require host restart. There is no identity-write CLI subcommand. |
| config.toml `[read]`, `[budget]`, `[stagnation]`, `[llm]` | File configuration covers read/output limits, prompt/tool budgets, stagnation thresholds, LLM retry/timeouts/wait notices. Resident consumers load at startup; hand off restart. | Validate TOML and supported keys/types: the loader can silently fall back to defaults or ignore invalid fields. Endpoint refresh does not reload this file for the resident core. |
| config.toml `[memory]` embedding settings | Configure remote/local Ollama URLs, model, keep-alive, timeouts and fallback cooldown. Setup exposes remote URLs and model. Reattach `sqlite_vec_l3` to rebuild its embedding provider from the file. | Inspect the active memory provider and embedding health; changing a model does not prove existing embeddings were rebuilt. Index refresh is a distinct operation, not a config reload. |
| Existing plugin source | `plugin_attach` on an already attached enabled plugin reloads its generation; inspect declared reload_modules and ownership. | Check load errors, attached state and the affected capabilities; dependencies may be suspended/reloaded. Do not promise zero interruption. |
| New plugin / manifest changes | `plugin_rescan` discovers metadata; then attach the enabled plugin. `plugin_enable` enables and attaches a disabled plugin. | Rescan alone does not reload existing code. Verify discovery, lifecycle result, and a representative operation. |
| Online package installation | Prefer `package_install` and follow its job with `package_status`; `package_prepare` repairs dependencies. The installer coordinates the relevant lifecycle owner. | Inspect both preparation and activation results. Preserve intentionally inactive state and avoid another attach/reload if installation already performed it. |
| New/removed/enabled/disabled channel provider | `channel_provider_rescan` discovers physical provider changes and eligible endpoint rows. | Inspect scan/load errors, provider mapping, endpoint health/auth/backlog. A provider without configured usable endpoint rows is not a working channel. |
| Existing channel provider code/manifest/resources | `channel_reload_provider` explicitly stops transports, unloads code, loads and reattaches that provider. | Failure leaves code unloaded and capabilities withdrawn while hubs retain queued delivery; fix and retry. Do not promise automatic rollback to the old generation. |
| One channel connection or authorization | `channel_restart_endpoint` rebuilds the connection without code reload. Use channel enable/disable/attach/detach and set_auth_material capabilities for their named operations. | Inspect endpoint state, authorization, health and backlog. The recovery socket has a resident boundary. |
| MCP server configuration | Edit TOML/JSON under `plugins/mcp/`, then `mcp_rescan`; use mcp_attach/mcp_detach for configured server connections. | Rescan reconnects changed enabled server configurations and updates tool discovery; inspect mcp_server_list/read. Editing manager implementation requires plugin reload. |
| LSP server configuration | Runtime overrides under `plugins/lsp/servers/`, then `lsp_rescan`; prepare/doctor the target workspace. | Rescan updates config and invalidates changed sessions; workspace preparation/use starts the needed server. Templates have their own development skill. |
| Bunshin profile/family customization | Use catalog read, set/reset_profile_override, set/reset_family_override, and catalog_refresh capabilities. | Overrides affect future Tasks, not existing snapshots. Catalog refresh reloads catalog data, not arbitrary sidecar Python code. |
| Core mode and cache reminder | `core_configure` changes in-memory mode; `core_configure_cache_warm_deadline` persists reminder settings. | Inspect core_observe/core_cache_warm_deadline; mode is not a permanent config change. Reminder changes apply to scheduling, not a host restart. |
| Search / browser settings | Use web_search provider config/auth/enable/active operations; browser_extension_manage controls local browser extensions. | Verify provider health/operation. Browser extension changes close current tabs; navigate again and verify the extension behavior. |
| Memory provider, learned behavior and skills | Use their live management tools. Facts belong to memory, routing rules to behavior, reusable procedures to skills. | A learned behavior change does not edit system policy. Declared built-in skills are owned by source and republished by the module; do not treat a database edit as a durable built-in override. |
| Host environment / OS service | Prepare the actual systemd/launchd/manual-service change and external restart instructions. | Daemon-reload alone does not replace a running process. Never stop, restart, kill, or schedule a delayed restart of Pal's own host from its active turn. |

## Route to specialist manuals

Search and inject only the matching manual if it is not already in context:

- `pal.plugin.development`: plugin layout, build_plugin/start(scope), capability
  contracts, packaging/private dependencies, RAII cleanup and hot reload.
- `pal.channel.provider.development`: transport communication plus provider.toml,
  provider registration, matching channel_endpoints records, auth, ingress/replies,
  control interactions and endpoint/provider lifecycle. Implementing transport
  communication alone does not integrate a channel into Pal.
- `pal.llm.model_hook_endpoint.development`: exact-model hooks, endpoint metadata,
  isolated request tests and refresh handoff.
- `pal.lsp.template.development`: LSP template schema, configuration and workspace
  verification; this skill is available when its owning LSP module is mounted.

Use current tool guidance for surfaces without a dedicated skill. If a skill or
capability is unavailable, inspect its owning plugin and the current tool surface;
do not invent it or infer that all self-maintenance is forbidden. Keep explaining
from verified evidence and identify the specific missing capability. For custom
endpoint rows or fields without a dedicated CLI/tool, inspect the repository/schema,
prepare an exact scoped patch, and apply only within the user's authorization.
Do not rerun the whole setup wizard merely to work around a missing narrow setter.

## Verify and hand off

For screenshot-based self-inspection, save the screenshot through the appropriate
browser/desktop capability, then call `artifact_import` with the local image path.
The import copies the file into current-conversation artifacts and attaches a
reference for prompt projection. `browser_screenshot` returns its source path in
`artifact.local_cached_path`. A path or stored_artifact_id alone is not pixel
input. Inspect pixels only when the next model request attaches the image inline
and the model supports vision; read_artifact is text-only. Image import checks
current-turn capabilities through core and rejects missing or unsupported vision
before creating an artifact. On that error, discover an OCR or image-analysis
tool with search_tools and verify that it accepts local paths. OCR extracts text;
it does not establish full visual inspection. If no suitable tool exists, explain
the limitation and the concrete endpoint-switching steps. Missing vision support,
processing failure, size limits, or an expired reference must be reported rather
than treated as a successful visual check.

Review the final working-tree/staged diff, including new files, and exercise the
changed behavior with focused tests. Reuse passing checks unless later changes
invalidate them. For lifecycle work verify the actual loaded version/behavior and
reported errors, not just a successful compile or saved file. An uncertain mutation
result calls for state reconciliation before any retry.

Before a required user step, make changes durable and provide: the target runtime,
what is ready, the exact command or conversation action, expected result, and the
follow-up check. Full host restart always belongs to the user or external supervisor.
Explain how to reconnect afterwards; do not guess the service name or restart it
through a delayed shell job. Avoid installing into Pal's Python to satisfy private
plugin dependencies; use the package owner.

Report separately what was edited, what was validated, what the running instance
has loaded, and what remains pending. Do not claim that a written configuration is
already effective. Store repair lessons or procedures only through the appropriate
knowledge tools and authorization; live state is not durable truth.
"""

PAL_PLUGIN_DEVELOPMENT_MANUAL = """# Pal Plugin Development

Use this skill when Pal needs to create, review, repair, or explain a Pal plugin and cannot rely on direct source-code access.

## Goal

A Pal plugin is a sidecar extension that adds capabilities, prompt fragments, providers, event sources, event handlers, or control handlers without editing Pal core. Prefer a plugin when the requested feature is optional, detachable, hot-refreshable, or owned by a domain boundary outside core.

Do not make a plugin for behavior that belongs in the pinned runtime bus itself or the shared message contracts. Core, execution, llm, channel (including the socket entrypoint), identity, memory, control, and failure are resident; other feature modules are plugins. Core should stay a coordinator; plugins should own feature behavior.

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
lifecycle_protocol = "raii.v1"
module_id = "demo_tools"
requires_plugins = []
requires_ports = []
```

Optional:

```toml
reload_modules = ["runtime", "capabilities"]
```

For community plugins, Pal clears modules loaded from the plugin directory during refresh. `reload_modules` is still useful when the plugin imports helper modules through stable names.

## Packaging and private dependencies

Use `pal package build` with a project-local `package.toml` to build a `.palpkg`.
Declare `id`, `kind="plugin"`, `version`, `python="venv"`, and `host_files`
(the runtime manifest and host-side management/IPC files). The backend wheel
comes from the project's pyproject.toml; its Python dependencies belong only to
the package venv. Optional `hooks="install_hooks.py"` supplies check/prepare/verify
functions returning a JSON object with boolean `ok`. Check must work before the
backend is installed. Prepare/verify use its private interpreter.

Use indirect `package_install` to install and activate a local package in a running Pal, then
`package_status` to follow its job and actual activation state. CLI installation
is offline and cannot replace files while that runtime is running. Do not repeat
attach/reload if the installation already activated the requested generation. Use `package_prepare` to retry dependency
preparation. Do not manually pip-install plugin dependencies into Pal's Python.

`PluginBuildContext.environment` supplies `python_executable` and `child_env()`
for the plugin's existing sidecar manager. Use both when spawning the backend;
do not resolve the Python symlink or inject its site-packages into Pal's sys.path.
The host-side adapter imports only Pal's existing APIs/dependencies. Sidecar
resources remain owned by the existing start(scope)/cleanup lifecycle.
Adapters with no extra Python backend can declare `python="host"`; this mode
never installs Python dependencies. Builtin modules keep their current packaging.

## Runtime Entrypoint

The entrypoint module must expose `build_plugin`. Pal calls it by name-aware dependency injection. Supported argument names include:

- `context`: a `PluginBuildContext` containing `runtime_root`, `services`, and `plugin_dir`.
- `runtime_root`: the Pal runtime root path.
- `plugin_dir`: this plugin directory.
- any service name present in the plugin host service map, such as `memory_service` or `runtime_db_path`.

Example `runtime.py`:

```python
from dataclasses import dataclass

from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from capabilities import DemoProvider


@dataclass
class DemoPluginBundle:
    plugin_id: str = "demo_tools"
    version: str = "0.1.0"

    def start(self, scope):
        provider = DemoProvider()
        handle = ModuleHandle(
            module_id="demo_tools",
            tier=MODULE_TIER_DETACHABLE,
            detachable=True,
            introspection_provider=provider,
        )
        # Resource acquisition belongs here. Register cleanup immediately so
        # rollback and detach both release it in reverse order.
        # scope.defer(resource.close)
        scope.context.register_module(handle)
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
5. After code changes, call `plugin_attach` on the already attached enabled plugin to reload it. Rescan first if manifest metadata changed. Explicit detach is for taking the plugin offline, not a prerequisite for every reload.

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
- Keep destructive changes, credentials, public network changes, and persistent system changes within explicit user authorization and execution-time capability policy. Continue within existing authorization without requesting the same approval again.
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
- models that reject ordinary sampling controls declare them in `capabilities_blob.unsupported_request_parameters`; codecs omit those fields without changing Core policy.
- `thinking_levels_blob` and `default_thinking_level`: explicit endpoint-supported enum values.

## Official GPT-6 Astra Profile

Prepare Astra without activating it:

```text
pal llm add gpt-6-astra --api-key-env OPENAI_API_KEY --no-enabled
```

The exact-model preset uses OpenAI Responses, the official 1,050,000-token context and 128,000-token output limits, vision/tool/stream support, reasoning levels `low`, `medium`, `high`, `xhigh`, and `max`, and omits unsupported `temperature`, `top_p`, and `top_logprobs` request parameters. Keep the endpoint disabled until API access and credentials are available; enabling or selecting it remains an explicit operator action.

OpenRouter exposes the same model as `openai/gpt-6-astra`. Configure that exact model id with the OpenRouter base URL and credential; Pal applies the same Responses and unsupported-parameter profile. OpenRouter advertises the full 1,050,000-token context, while a deployment may deliberately cap it at 272,000 tokens to avoid the higher long-context pricing tier.

For endpoint configuration, use `pal llm list --all --json`, `pal llm add ENDPOINT --replace`, and `pal llm delete ENDPOINT` with the verified `--runtime-root`. Add without --replace creates a new endpoint; replace preserves omitted fields. Use --store-api-key for secure user input or --api-key-stdin for authorized secret input, never secrets in argv. Use --enabled/--no-enabled and --set-active only as requested. CLI writes require `/refresh_llm_endpoint` to refresh a running instance. For a field the CLI cannot express, inspect the current repository/schema and prepare a scoped patch. Existing explicit authorization covers its agreed actions; ask only for uncovered database or credential changes.

## Verification Workflow

Before asking the user to refresh:

1. Inspect `pal.llm.model_hooks.ModelHook` and the endpoint row.
2. Write the exact-model hook under the runtime-root model directory.
3. Compile it with `python -m py_compile <hook_file>`.
4. Load it through `ModelHookRegistry` in an isolated test.
5. Build a representative `LLMRequestIR` and assert only messages or tool definitions change.
6. Confirm routing, provider, credential, and wire shape cannot change.
7. Verify endpoint CLI changes against a temporary database, or review the exact patch for fields without CLI support.
8. Do not switch the active endpoint during development unless the user requested that switch.

## Handoff

When the model hook and endpoint are ready, report:

- hook file path, exact model ID, and endpoint ID
- tests or smoke checks run
- endpoint/database/secret changes made or still pending
- any load errors found in isolated checks
- whether the running endpoint was switched/refreshed, with its confirmed result or pending user step

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

Providers can share the `.palpkg` installation pipeline using `kind="provider"`.
Their lifecycle still belongs to this channel manager. A private Python backend
uses `ChannelProviderBuildContext.environment.python_executable` and
`environment.child_env()`; no provider dependencies are installed into Pal's
Python environment. Legacy provider wheel installation remains supported.

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

`provider_id` must be stable and unique. `entrypoint` must point to a Python file inside the provider directory. `enabled = false` keeps the provider discoverable on disk but not loaded. `reload_modules` is retained for diagnostics; endpoint restart does not evict provider modules.

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

There is no generic `pal channel add` CLI. If no current endpoint-management capability covers the row, inspect the repository/schema and prepare an exact scoped patch. Apply it within existing explicit authorization; ask only if that production change is not covered. Do not rerun the entire setup wizard for one custom endpoint row.

## Interactions and Commands

Control interactions are channel-neutral. Prefer the base `ChannelEndpointQueueBase` hooks first:

- `apply_control_catalog`
- `apply_interaction_status`
- `open_or_update_interaction`
- `resolve_interaction`
- `emit_interaction_result`

Only override channel-specific rendering, such as inline keyboards, slash-command menus, callback payloads, receipts, typing indicators, or transport-specific message editing. Avoid hard-coding Telegram-only assumptions into shared control or core code.

The shared contract is typed data, not UI widgets. Core/control may produce `InteractionMessageSpec`, `InteractionButtonSpec`, `InteractionResult`, status kinds, attachments, and text; the provider decides how those become inline keyboards, menus, edits, reactions, receipts, native commands, or no-op fallbacks. Do not introduce channel-specific callback payloads, button shapes, or slash-command parsing into `core`, `control`, `bunshin`, `llm`, or `memory`.

## Lifecycle and Hot Reload

Provider rescan means:

1. Scan `<runtime_root>/channel/providers/*/provider.toml` for additions, removals, enabled and disabled providers.
2. Keep already discovered providers running; rescan does not detect or reload in-place source changes. Discover eligible new endpoint rows for known providers.
3. Build newly discovered providers, validate provider ids and endpoint type ownership, and hydrate enabled endpoint rows that are not durably detached.
4. Runtime-detach removed/disabled providers without deleting durable endpoint rows. Malformed manifests are reported rather than treating a known provider as physically removed.
5. Republish introspection capabilities and inspect scan/load errors.

Use `channel_reload_provider` for an existing provider's source, manifest, or provider-wide resource changes. It withdraws endpoint capabilities, stops transports, unloads the old provider, loads the replacement, and restores previously attached endpoints. On failure the provider stays unloaded and capabilities stay withdrawn; endpoint hubs retain queued delivery. Fix the reported problem and retry. This path does not promise an atomic switch or automatic rollback to the old provider.

Provider build code may call `context.register_cleanup(callback)` and expose optional `attach(context)` / `detach(context)` hooks. Own resources through that lifecycle so load failure and unload can release them.

Use `channel_restart_endpoint` to rebuild one connection through its already loaded provider. It does not reload provider modules or discover new providers. Do not add a redundant endpoint restart after a successful provider reload.

For installation, `pal provider install WHEEL --runtime-root ROOT` and `pal package install PATH --runtime-root ROOT` are offline commands guarded by the runtime lock. In a running Pal use `package_install`, follow `package_status`, and inspect the owner's activation result before adding any rescan/reload. A successful package installation can already perform the provider load.

Never stop, restart, or kill your own hosting service or process from inside the active turn. If a change to Pal core or the recovery socket genuinely requires a full process restart, make the work durable and hand that restart off to the user or an external supervisor.

Useful operations:

- `channel_provider_rescan`: discover additions/removals/enabled/disabled providers and eligible endpoint rows; leave existing provider code loaded.
- `channel_list`: list configured channel endpoints and provider ids.
- `channel_endpoint_inspect`: inspect one endpoint.
- `channel_endpoint_auth_state`: inspect authorization without revealing secrets.
- `channel_endpoint_health`: inspect network and delivery health.
- `channel_endpoint_backlog`: inspect queue sizes.
- `channel_attach`: attach one endpoint through its provider.
- `channel_detach`: detach one endpoint through its provider.
- `channel_reload_provider`: explicitly unload and reload one known runtime-root provider by provider id.
- `channel_restart_endpoint`: restart one endpoint runtime instance without reloading provider code.

## Verification Workflow

Before calling a channel provider done:

1. Inspect current channel provider manager and endpoint contracts.
2. Write `provider.toml` and provider source under `<runtime_root>/channel/providers/<provider_id>/`.
3. Compile provider source with `python -m py_compile`.
4. Test provider loading in isolation or with a temporary runtime root first.
5. Add or preview the `channel_endpoints` row for the provider's endpoint type.
6. For a new provider use `channel_provider_rescan`; for in-place code changes use `channel_reload_provider`. Skip a duplicate load if package installation already performed it. Check load errors and the operation result; do not restart the Pal service.
7. Verify `channel_list` shows `provider_id`.
8. Verify endpoint `inspect`, `auth_state`, `health`, and `backlog`.
9. Dogfood through the real channel if safe; for socket, send `/control` before sending LLM-consuming messages.
10. Detach and attach the endpoint once to prove provider-owned lifecycle is reversible.

## Safety Notes

- Do not expose tokens or secrets in introspection payloads.
- Do not start multiple long-polling instances for the same external account or Telegram bot token.
- Production endpoint rows, credentials, and live polling changes need explicit authorization covering the action. Reuse existing authorization within its scope; do not ask for the same approval again.
- Keep provider failures structured and visible through `runtime_provider_load_errors` or endpoint health.
- Prefer small deterministic transport adapters. Put large protocol clients or sidecars behind provider-owned lifecycle code.
- A provider that touches hardware, local IPC, OS devices, sensors, cameras, microphones, serial ports, GPIO, Bluetooth, or other privileged resources must make that ownership explicit in introspection and cleanly release the resource on detach/reload.
- Ingress should attach a provider-owned `control_scope_key` when it needs custom conversation grouping. Shared routing must consume that key without understanding the channel's internal identifiers.
"""


def builtin_declared_skills(*, module_id: str = "skill") -> tuple[SkillDescriptor, ...]:
    return (
        SkillDescriptor(
            skill_id=PAL_SELF_MAINTENANCE_SKILL_ID,
            module_id=module_id,
            title="Pal Self Maintenance",
            summary="Explain Pal configuration directly to users and carry out authorized self-modification with precise CLI, reload, and restart guidance.",
            manual_text=PAL_SELF_MAINTENANCE_MANUAL,
            activation_terms=(
                "pal self maintenance", "self maintenance", "repair pal", "pal source repair",
                "prompt boundary", "system prompt refactoring", "自我维护", "维护 Pal",
                "修复 Pal", "维护流程", "提示词边界", "内置维护 skill",
                "自我修改", "如何配置 Pal", "怎么配置 Pal", "Pal怎么配置", "pal cli", "configure pal", "pal configuration",
            ),
            capability_refs=("search_tools", "skill_search", "skill_inject", "run_shell"),
            applicability_star=SkillApplicabilitySTAR(
                situation="The user asks how to configure Pal or requests modification of Pal itself.",
                task="Explain the exact configuration steps or implement an authorized change with an accurate activation outcome.",
                action="Inspect evidence and diffs, choose the owning surface, implement, verify, and load or hand off.",
                result="The requested maintenance is verified without redundant approval or unsafe self-restart.",
            ),
            use_when="Use for Pal configuration questions, CLI guidance, self-modification, source/config repairs, and prompt-boundary refactoring.",
            avoid_when="Avoid for ordinary user projects or runtime status questions that need only live introspection.",
            source_format="internal_skill",
            source_refs=(
                "pal.skill.builtin_skills", "pal.core.prompt", "pal.main",
                "pal.core.runtime_config", "docs/pal_skill_contract.md",
            ),
            metadata={"internal": True},
        ),
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
                "channel_restart_endpoint",
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
