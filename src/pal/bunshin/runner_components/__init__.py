"""Composed owners for one role invocation.

``composition`` is the only assembly point. ``AgentSession`` advances the turn
continuation; ``Control`` owns Manager decisions; ``SessionCheckpoints`` owns
checkpoint identity and persistence. Artifact, memory, research, and tool-session
owners retain their own state and expose explicit restore/update operations.
LLM/output adapters consume reporting and heartbeat owners, never BunshinRunner.

The runner entry point owns runtime construction and final shutdown. Native
execution sessions satisfy ``contracts.RoleExecutionSessions`` and retain their
own process and output-delivery authority.
"""
