# worker

Owns:
- worker runtime boundary
- execution-context acceptance
- worker-plane ipc handler names

Does not own:
- user-facing channel communication
- persona or memory truth
- supervisor lifecycle

Exposes:
- `WorkerRuntime`
- `WorkerIPCHandlers`
