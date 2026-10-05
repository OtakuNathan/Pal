# wizard

Owns:
- lifecycle registration and launch specs
- runtime-root to database-file association
- creation of the runtime database handle
- first-run provisioning and initial defaults

Does not own:
- in-process module governance
- `PalCore` lifecycle
- user-facing message routing
- memory
- tool execution
- normal chat handling
- process supervision (handled by systemd)

Exposes:
- `RuntimeLaunchSpec`
- `PalRegistration`
- `ProvisionedRuntime`
- `WizardService`

Boundary:
- `wizard` lives outside the Pal runtime
- it does not register with `PalCore`
- it does not publish capabilities into `Execution`
- it does not participate in the in-process `MainLoop`

## LLM thinking configuration

Setup collects the exact endpoint-supported thinking levels and one default
from that list. It shares shape-aware validation with `pal llm add` and the
endpoint repository; it does not maintain effort mappings. Invalid levels are
shown with the shape's accepted vocabulary, and invalid defaults with the
endpoint's declared choices. Interactive prompts ask again rather than silently
filtering or replacing the input. Seeding validates every thinking declaration
before changing setup state.

For an existing runtime, use `pal llm add ENDPOINT --replace` for a narrow endpoint
edit, then `/refresh_llm_endpoint` to load the configuration. Accepted keywords
are not proof of remote model support. See the
[LLM contract](../../../docs/pal_llm_contract.md#thinking-selection-validation-and-wire-encoding)
for the wire fields, switches, budgets, and examples.

## ChatGPT plan setup

`pal wizard --llm --runtime-root <dir>` (also `setup` and `wizzard`) manages only
LLM endpoints in an existing runtime. It does not provision channels, plugins,
identity, embeddings, or OS services. Initial setup includes the same endpoint
flow in the full wizard.

Choose **Use ChatGPT plan / manage accounts**, then **Continue with ChatGPT**.
Choose a local browser or **Remote / headless** login. SSH sessions and Linux
hosts without a display default to remote login; no X server is needed there.
For remote login, enter the SSH destination (or an alias in your browser computer's
SSH config), SSH port and callback port. The wizard starts a loopback listener,
prints an SSH forwarding command to run on your browser computer, then prints
the authorization link to open on that computer. Keep the tunnel open until
authorization completes, then stop it with Ctrl+C. Both ends use the same callback
port, which must be free on both computers. If forwarding fails, cancel the wizard
attempt and retry with another port. Remote login waits up to ten minutes.

The temporary callback listener binds only to `127.0.0.1`; tokens are exchanged
and saved on the host running Pal. This is OAuth through an SSH tunnel, not device
code login or credential migration. Pal never reads Codex credentials. Account records
are saved after authorization, while endpoint changes require successful text
and harmless tool-round-trip checks and final confirmation. Cancelling preserves
the active endpoint; a successfully authorized account remains available later.

The model picker uses the selected account's live catalog, including its
`supported_reasoning_levels` and `default_reasoning_level`. The wizard offers
the effort values Pal can encode, reports unsupported catalog values explicitly,
and lets the user choose the default. `off` in Pal means omitting the effort
parameter, not the provider's `none`; it is not substituted for catalog values.
Missing reasoning metadata requires explicit configuration. Local context/output
budgets are not provider-enforced output limits. Existing API endpoints remain available for
manual selection. Sign-out clears local tokens and attempts remote revocation;
an unconfirmed revocation is reported explicitly.

After saving endpoint metadata, send `/refresh_llm_endpoint` in Pal. New Python
implementation code requires a host restart first; configuration refresh alone
cannot load it. Subsequent token renewals and reauthorization of the same saved
registration are picked up on the next request without a restart.
