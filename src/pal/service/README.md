# service

Owns:
- service definition and service-run semantics
- schedule boundary
- future output delivery target selection

Does not own:
- channel transport
- tool execution runtime
- worker lifecycle
- memory truth

Exposes:
- `ServiceDefinition`
- `ServiceTriggerEvent`
- `ServiceManager`
- `ScheduleEngine`
- `ServiceEventSource`
