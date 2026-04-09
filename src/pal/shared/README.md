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
