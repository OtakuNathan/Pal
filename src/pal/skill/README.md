# skill

Skill owns declared-skill projection and its repository. It contributes one
`module_observer` with its ModuleHandle. Publication replays existing modules,
then notifies the observer of later capability publication and withdrawal.

Reload rebuilds declarations from the currently published module generations,
while retaining user-authored skills. Declared-skill discovery stays inside
Skill; Core, PluginHost and MCP do not call Skill's registration methods.
Failed publication unwinds the candidate projection and its resources through
the same lifecycle owner.
