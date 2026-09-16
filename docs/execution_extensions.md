# Plugin-owned execution

Pal starts with its built-in execution runtime. An optional plugin can return a
`ModuleHandle.execution_extension` requiring `execution:extensions`. The plugin
host installs the contribution under its normal lifecycle fence; only one
replacement can be active. Existing consumers keep the stable `ExecutionSlot`.

The extension implements:

- `build_runtime(previous)`: prepare an ExecutionRuntime-compatible implementation.
  Reuse the provided executor; do not allocate a second shared executor.
- `build_provider(runtime)` and `build_state_port(runtime)`: supply the execution
  capability subtree and checkpoint/reset port.
- `activate(context, handle, runtime)`: add plugin-owned ports, event sources,
  handlers and control actions to the owning module handle. The host publishes
  these through its existing registries. Do not execute user work during activation.
- `check_detach(runtime)`: reject teardown while owned work cannot be safely released.
  The host calls this before withdrawing the plugin's visible surfaces.
- `close(runtime)`: synchronous idle teardown of extension-owned resources. Do not
  close the shared executor or logical state; they return to the built-in runtime.
  Async resource shutdown belongs in runtime `prepare_shutdown_async` and
  `shutdown_async` for whole-host shutdown.

The replacement preserves common logical state, paging, lifecycle locks and
unrelated capability subtrees. Publication compiles a new execution registry
generation. Activation failure restores the prior implementation/provider/state
port. Idle detach compiles the original provider again against the current logical
state. Extension code must clean up any resources allocated before raising.
The owning plugin's binary dependencies remain loaded until process exit.

Bunshin uses generic runtime hooks for role capability projection, session
observation, cancellation and terminal verification evidence. Descriptor metadata
`preserve_role_contract` and `preserve_role_invocation_mode` preserve an extension's
schema and indirect controls. The extension's role projection receives the common
role guidance first; it must retain the host's scope restrictions. These metadata
flags do not grant execution authority or bypass approval.

An installed plugin may declare:

```toml
[execution]
role_entrypoint = "example_plugin.worker:activate"
worker_modules = ["example_binary"]
```

Only enabled, attached package records contribute. Worker activation calls the
entrypoint with its own MainContext, after default execution registration. Linux
sandbox construction binds the package and declared dependency modules read-only.
Missing declared dependencies fail worker startup; no silent backend substitution
or command replay occurs. The plugin owns which resident features are available
inside its worker contribution. Package reload and code replacement must respect
Pal's existing lifecycle and running-worker constraints.

Execution diagnostics come from `execution_diagnostics()` under the runtime debug
snapshot's `execution` key. Tool metadata `background_execution` lets activity
reporting identify retained execution without importing a particular extension.
