# control

Owns:
- deterministic control parsing
- explicit governance actions
- approval and pause/resume/cancel style control semantics

Does not own:
- tool execution
- open-ended reasoning
- durable truth
- business object implementations
- domain-specific interaction rendering such as bunshin workflow buttons or memory candidate approvals

Exposes:
- `ControlEvent`
- `ControlAction`
- `ControlPlane`

Plugins declare slash commands in `ModuleHandle.control_commands` as a tuple of
`ControlCommandSpec`. The handler produces a deterministic `ControlAction`; the
plugin's `control_action_handlers` executes its owned action through Core.
Commands do not add LLM tools or bypass normal control admission.

PluginHost installs the complete declaration only after startup and capability
publication succeed. Names and aliases are validated together; a collision with
another plugin or resident command rejects the entire declaration. Unload and
rollback withdraw the command lease before resource cleanup. Cleanup from a
retired generation cannot unregister a replacement's commands.

Set `show_in_panel=True` and provide a description to publish a command to all
channel menus; `panel_button=True` also adds a control-panel button. Control owns
one catalog and coalesces registration/removal into Core events. Core delivers
updates to channels without a model call. Providers render the catalog rather
than maintaining independent command lists.
