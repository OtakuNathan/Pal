"""Turn-owned routing offered to tools by the resident or role host."""
from __future__ import annotations

from typing import Any, Protocol

from pal.execution.contracts import CapabilityResult
from pal.foundation import AttachmentSpec
from pal.shared.ports import PortKey


class TurnIOPort(Protocol):
    async def send_attachment_for_turn(
        self, turn_id: str | None, attachment: AttachmentSpec,
    ) -> CapabilityResult: ...

    def artifact_scope_for_turn(self, turn_id: str | None) -> str | None: ...

    def llm_capabilities_for_turn(self, turn_id: str | None) -> dict[str, Any]: ...

    def capture_delivery_binding(self, turn_id: str | None) -> dict[str, Any]: ...


TURN_IO = PortKey[TurnIOPort]("core:turn_io", TurnIOPort)


class TurnIOHost(Protocol):
    @property
    def turn_io(self) -> TurnIOPort | None: ...
