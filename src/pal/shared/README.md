# shared

Owns:
- stable cross-module constants and ownership boundaries
- types that are semantically shared across multiple subsystems

Does not own:
- business logic
- persistence implementations
- runtime orchestration

Exposes:
- runtime-vs-durable ownership constants
- minimal cross-module stable contracts
- shared introspection call/result/port contracts
- the single provider-neutral Tool Protocol IR and tagged invocation result

`tool_protocol.py` owns tool definitions, calls, transcript results, execution
results, effect/retry outcomes, and recovery affordances. LLM, Execution,
Memory, and Minion consume this protocol; none may define a private
tool-call or tool-result envelope. Received calls require an explicit call ID.
Only Pal-originated calls may allocate one through `new_tool_call()`.
