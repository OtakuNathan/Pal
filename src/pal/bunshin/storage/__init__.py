"""SQLite stores organized by durable responsibility.

BunshinV2Repository composes the store dependency graph. An application operation
uses ``repository.transaction()`` when several stores must publish atomically.
Its BunshinUnitOfWork exposes bound stores, not a raw SQLite connection. Only the
outer database context commits or rolls back; bound sessions expire on exit.

Schema, serialization, fencing checks, idempotency keys and outbox publication
remain storage responsibilities. Stores must not import application handlers or
semantic orchestration components.
"""
