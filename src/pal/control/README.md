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
- domain-specific interaction rendering such as minion workflow buttons or memory candidate approvals

Exposes:
- `ControlEvent`
- `ControlAction`
- `ControlPlane`
