# identity

Owns:
- persona bootstrap truth
- user preferences truth
- minimal durable pal state
- domain DTOs for pal identity

Does not own:
- llm prompt assembly
- channel transport
- memory packs
- execution logic
- task orchestration

Exposes:
- identity profiles and identity service
- `IdentityRepository`
- `IdentityService`
