"""Resident boundary for optional remote backends (no RPC dependency)."""
from typing import Protocol


class RemoteFailure(RuntimeError):
    def __init__(self, code, message, *, effect='not_started', operation_id=''):
        super().__init__(message)
        self.code, self.effect, self.operation_id = code, effect, operation_id


class RemotePort(Protocol):
    async def request(self, target: int, method: str, params: dict, epoch: str | None = None) -> dict: ...
    async def list(self, refresh: bool = False) -> list[dict]: ...
