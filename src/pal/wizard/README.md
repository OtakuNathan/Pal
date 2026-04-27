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
