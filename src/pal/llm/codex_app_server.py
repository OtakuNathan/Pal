from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CodexAppServerClientInfo:
    name: str = "pal"
    title: str = "Pal"
    version: str = "0.1.0"

    def to_params(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title, "version": self.version}


@dataclass
class CodexAppServerAuthMessages:
    client_info: CodexAppServerClientInfo = field(default_factory=CodexAppServerClientInfo)
    experimental_api: bool = False

    def initialize_request(self, request_id: int = 0) -> dict[str, Any]:
        params: dict[str, Any] = {"clientInfo": self.client_info.to_params()}
        if self.experimental_api:
            params["capabilities"] = {"experimentalApi": True}
        return {"method": "initialize", "id": request_id, "params": params}

    @staticmethod
    def initialized_notification() -> dict[str, Any]:
        return {"method": "initialized", "params": {}}

    @staticmethod
    def account_read_request(request_id: int, *, refresh_token: bool = False) -> dict[str, Any]:
        return {
            "method": "account/read",
            "id": request_id,
            "params": {"refreshToken": bool(refresh_token)},
        }

    @staticmethod
    def login_chatgpt_browser_request(request_id: int) -> dict[str, Any]:
        return {
            "method": "account/login/start",
            "id": request_id,
            "params": {"type": "chatgpt"},
        }

    @staticmethod
    def login_chatgpt_device_code_request(request_id: int) -> dict[str, Any]:
        return {
            "method": "account/login/start",
            "id": request_id,
            "params": {"type": "chatgptDeviceCode"},
        }

    @staticmethod
    def login_chatgpt_auth_tokens_request(
        request_id: int,
        *,
        access_token: str,
        chatgpt_account_id: str,
        chatgpt_plan_type: str,
    ) -> dict[str, Any]:
        return {
            "method": "account/login/start",
            "id": request_id,
            "params": {
                "type": "chatgptAuthTokens",
                "accessToken": access_token,
                "chatgptAccountId": chatgpt_account_id,
                "chatgptPlanType": chatgpt_plan_type,
            },
        }

    @staticmethod
    def logout_request(request_id: int) -> dict[str, Any]:
        return {"method": "account/logout", "id": request_id}

    @staticmethod
    def rate_limits_read_request(request_id: int) -> dict[str, Any]:
        return {"method": "account/rateLimits/read", "id": request_id}

    @staticmethod
    def chatgpt_auth_tokens_refresh_response(
        request_id: int,
        *,
        access_token: str,
        chatgpt_account_id: str,
        chatgpt_plan_type: str,
    ) -> dict[str, Any]:
        return {
            "id": request_id,
            "result": {
                "accessToken": access_token,
                "chatgptAccountId": chatgpt_account_id,
                "chatgptPlanType": chatgpt_plan_type,
            },
        }


def is_chatgpt_auth_tokens_refresh_request(message: dict[str, Any]) -> bool:
    return str(message.get("method") or "") == "account/chatgptAuthTokens/refresh" and "id" in message


def redact_codex_auth_message(message: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(message)
    params = redacted.get("params")
    if isinstance(params, dict):
        params = dict(params)
        for key in ("apiKey", "accessToken"):
            if key in params:
                params[key] = "<redacted>"
        redacted["params"] = params
    result = redacted.get("result")
    if isinstance(result, dict):
        result = dict(result)
        if "accessToken" in result:
            result["accessToken"] = "<redacted>"
        redacted["result"] = result
    return redacted
