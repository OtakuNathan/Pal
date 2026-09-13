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
