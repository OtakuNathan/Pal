# proactive

Owns:
- proactive task definition and run semantics
- schedule boundary
- future output delivery target selection

Does not own:
- channel transport
- tool execution runtime
- worker lifecycle
- memory truth

Exposes:
- `ServiceDefinition`
- `ProactiveTriggerEvent`
- `ServiceManager`
- `ScheduleEngine`
- `ServiceEventSource`
