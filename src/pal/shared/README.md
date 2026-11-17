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
Memory, and Bunshin consume this protocol; none may define a private
tool-call or tool-result envelope. Received calls require an explicit call ID.
Only Pal-originated calls may allocate one through `new_tool_call()`.

Failed delivery may retain an owner's reported operation result and effect
receipt independently of its validation diagnostic. Execution renders those
fields as guidance metadata outside the normal-output budget.

Service consumers subscribe with `PortRegistry.observe(key)`. `ServiceRef` caches the published value; publication pushes replacements and withdrawal clears the slot. Consumers capture an instance for one operation; owners drain its work before closing resources.

`registration.py` owns qualified `(domain, name)` identities and the pure
identity-based claim/release reducer shared by registration owners and generated
formal relations.

`maintenance.py` declares the opaque maintenance owner token and the identity
predicate used by Core admission and generated Memory ownership models. Absence
is never ownership, and copying a token's diagnostic identifier grants no authority.
