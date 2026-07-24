from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pal.execution.contracts import CapabilityResult
from pal.minion.ipc import ROLE_GATEWAY_TOKEN_ENV, MinionRoleGatewayClient
from pal.shared import IntrospectionResult


def web_result_to_payload(result: CapabilityResult) -> dict[str, Any]:
    return {
        "status": str(result.status),
        "text": str(result.text or ""),
        "llm_text": str(result.llm_text or ""),
        "structured": dict(result.structured or {}),
    }


def web_result_from_payload(payload: dict[str, Any]) -> IntrospectionResult:
    return IntrospectionResult(
        status=str(payload.get("status") or "error"),
        text=str(payload.get("text") or ""),
        llm_text=str(payload.get("llm_text") or payload.get("text") or "web request failed"),
        structured=dict(payload.get("structured") or {}),
    )


@dataclass(frozen=True)
class MinionBrokerWebClient:
    runtime_root: Path
    run_id: str
    request_timeout_seconds: float = 300.0

    @property
    def _client(self) -> MinionRoleGatewayClient:
        if os.environ.get("PAL_MINION_WEB_BROKER") != "1":
            raise RuntimeError("sandboxed web access requires the host web broker")
        access_token = str(os.environ.get(ROLE_GATEWAY_TOKEN_ENV) or "").strip()
        if not access_token:
            raise RuntimeError(
                "sandboxed Minion has no assignment-scoped role gateway token"
            )
        return MinionRoleGatewayClient(
            runtime_root=Path(self.runtime_root),
            access_token=access_token,
            request_timeout_seconds=self.request_timeout_seconds,
        )

    def search(self, args: dict[str, object]) -> IntrospectionResult:
        return self._request("web_search", args)

    def read(self, args: dict[str, object]) -> IntrospectionResult:
        return self._request("web_read", args)

    def _request(
        self,
        method: str,
        args: dict[str, object],
    ) -> IntrospectionResult:
        response = self._client.request_sync(
            method,
            {
                "run_id": self.run_id,
                "args": dict(args),
            },
        )
        return web_result_from_payload(dict(response.get("result") or {}))


__all__ = [
    "MinionBrokerWebClient",
    "web_result_from_payload",
    "web_result_to_payload",
]
