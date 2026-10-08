"""ChatGPT plan access: public OAuth client and runtime-owned sessions.

No Codex credentials or app-server are involved. Secrets stay in the owning
runtime's secret store; endpoint rows hold references only.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import webbrowser
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode
from uuid import uuid4

import httpx

from pal.llm.secret_store import SecretRef, SecretStorePort, TransactionalSecretStorePort
from pal.foundation.diagnostics import diagnostic_text
from pal.shared.json_values import thaw_json

PROFILE = "openai_chatgpt"
API_URL = "https://api.openai.com/v1"
ISSUER = "https://auth.openai.com"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
USAGE_URL = "https://chatgpt.com/settings/usage"
QUOTA_CODE = "subscription_sharing_usage_limit_exceeded"
UNSUPPORTED_PARAMETERS = frozenset({
    "background", "conversation", "max_output_tokens", "max_tool_calls", "metadata",
    "moderation", "multi_agent", "prompt", "prompt_cache_retention", "safety_identifier",
    "temperature", "top_logprobs", "top_p", "truncation", "user", "previous_response_id",
})
_TERMINAL_REFRESH = frozenset({
    "invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
    "refresh_token_invalidated", "refresh_token_reused",
})
_INDEX = SecretRef("pal.chatgpt", "accounts")
_HOST = SecretRef("pal.chatgpt", "host")


def is_chatgpt(value: Any) -> bool:
    capabilities = value if isinstance(value, Mapping) else value.capabilities_blob
    return (capabilities or {}).get("access_profile") == PROFILE


class ChatGPTError(RuntimeError):
    """Structured failure retaining redacted provider diagnostics."""

    def __init__(self, code: str, *, status: int | None = None, param: str = "", request_id: str = "", diagnostic: str = ""):
        self.code = str(code or "unknown_error")
        self.status = status
        self.param = str(param or "")
        self.request_id = str(request_id or "")
        self.diagnostic = diagnostic_text(diagnostic, limit=None)
        super().__init__(self.user_message + ("\n" + self.diagnostic if self.diagnostic else ""))

    @property
    def retryable(self) -> bool:
        return self.code in {"network_error", "subscription_sharing_usage_unavailable",
                             "subscription_sharing_user_unavailable"} or (
            self.status is not None and self.status >= 500
        )

    @property
    def user_message(self) -> str:
        if self.code == QUOTA_CODE:
            return (
                "ChatGPT 订阅或 Pal 应用用量已达限制，订阅请求已暂停。"
                "请用 /model <endpoint_id> 手动选择其他 endpoint；"
                "额度恢复后重新选择订阅 endpoint 才会解除暂停。管理用量：" + USAGE_URL
            )
        if self.code in {"reauthorization_required", "subscription_sharing_invalid_user"} or self.status == 401:
            return "ChatGPT 授权已失效或尚未配置，请运行 pal wizard --llm --runtime-root <dir> 重新授权。"
        if self.code == "plan_permission_missing":
            return "已登录 ChatGPT，但尚未授予订阅使用权限。请在 pal wizard --llm 中重新授权。"
        return f"ChatGPT 请求未完成（{self.code}）。未切换 endpoint；可使用 /model 手动选择。"

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "status": self.status, "param": self.param, "request_id": self.request_id,
                **({"diagnostic": self.diagnostic} if self.diagnostic else {})}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ChatGPTError:
        return cls(str(value.get("code") or "unknown_error"), status=value.get("status"),
                   param=str(value.get("param") or ""), request_id=str(value.get("request_id") or ""),
                   diagnostic=str(value.get("diagnostic") or ""))


def response_error(payload: Mapping[str, Any], *, status: int | None = None, request_id: str = "") -> ChatGPTError:
    detail = payload.get("error")
    detail = detail if isinstance(detail, Mapping) else payload
    code = detail.get("code")
    if not code and isinstance(payload.get("error"), str):
        code = payload["error"]
    return ChatGPTError(str(code or "request_failed"), status=status,
                        param=str(detail.get("param") or ""), request_id=request_id,
                        diagnostic=json.dumps(thaw_json(payload), ensure_ascii=False, default=str))


def exception_error(exc: BaseException, *, include_http: bool = False) -> ChatGPTError | None:
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, ChatGPTError):
            return exc
        if include_http:
            from openai import APIStatusError
            if isinstance(exc, APIStatusError):
                body = exc.body if isinstance(exc.body, Mapping) else {}
                if "code" in body and "error" not in body:
                    body = {"error": body}
                return response_error(body, status=exc.status_code, request_id=str(exc.request_id or ""))
        exc = exc.__cause__ or exc.__context__
    return None


def _record_ref(client_id: str) -> SecretRef:
    return SecretRef("pal.chatgpt", client_id)


def credential_ref(client_id: str) -> str:
    return f"pal.chatgpt:{client_id}"


def _epoch(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            from datetime import datetime
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    return 0.0


class ChatGPTAuthService:
    def __init__(self, store: SecretStorePort, *, client: Any = None, clock: Callable[[], float] = time.time):
        self.store = store
        self.client = client
        self.clock = clock

    def _transaction(self):
        if not isinstance(self.store, TransactionalSecretStorePort):
            raise ChatGPTError("transactional_secret_store_required")
        return self.store.transaction()

    def _read(self, ref: SecretRef) -> dict[str, Any]:
        raw = self.store.get_secret(ref)
        try:
            value = json.loads(raw or "{}")
        except ValueError:
            raise ChatGPTError("reauthorization_required") from None
        if not isinstance(value, dict):
            raise ChatGPTError("reauthorization_required")
        return value

    def _write(self, ref: SecretRef, value: Mapping[str, Any]) -> None:
        self.store.set_secret(ref, json.dumps(dict(value)))

    def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            if self.client is None:
                with httpx.Client(timeout=20, follow_redirects=False) as client:
                    response = client.request(method, url, **kwargs)
            else:
                response = self.client.request(method, url, **kwargs)
        except httpx.HTTPError:
            raise ChatGPTError("network_error") from None
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {}
        if response.status_code >= 300:
            raise response_error(body if isinstance(body, Mapping) else {}, status=response.status_code,
                                 request_id=response.headers.get("x-request-id", ""))
        if not isinstance(body, dict):
            raise ChatGPTError("invalid_server_response")
        return body

    def host_id(self) -> str:
        with self._transaction():
            value = self.store.get_secret(_HOST)
            if not value:
                value = f"urn:uuid:{uuid4()}"
                self.store.set_secret(_HOST, value)
            return value

    def accounts(self) -> list[dict[str, Any]]:
        with self._transaction():
            index = self._read(_INDEX)
            return [self._public(self._read(_record_ref(key))) for key in index.get("ids", [])]

    @staticmethod
    def _public(record: Mapping[str, Any]) -> dict[str, Any]:
        return {key: record.get(key) for key in (
            "client_id", "email", "subject", "scopes", "needs_reauthorization", "usage_paused",
        )}

    def _save_tokens(self, record: dict[str, Any], tokens: Mapping[str, Any]) -> None:
        if not tokens.get("access_token") or not tokens.get("refresh_token"):
            raise ChatGPTError("invalid_token_response")
        record.update({key: tokens[key] for key in ("access_token", "refresh_token", "id_token") if tokens.get(key)})
        record["expires_at"] = self.clock() + float(tokens.get("expires_in", 3600))
        record["earliest_refresh_at"] = _epoch(tokens.get("earliest_refresh_at"))
        if "scope" in tokens:
            record["scopes"] = str(tokens["scope"]).split()
        record["needs_reauthorization"] = False
        self._write(_record_ref(record["client_id"]), record)

    def access_token(self, ref: SecretRef, *, allow_paused: bool = False) -> str:
        with self._transaction():
            record = self._read(ref)
            if (record.get("auth_provider") != PROFILE or not record.get("refresh_token")
                    or not record.get("access_token") or record.get("needs_reauthorization")
                    or record.get("issuer") != ISSUER or not record.get("subject")):
                raise ChatGPTError("reauthorization_required")
            if record.get("usage_paused") and not allow_paused:
                raise ChatGPTError(QUOTA_CODE)
            if "chatgpt.tokens.use.direct" not in record.get("scopes", []):
                raise ChatGPTError("plan_permission_missing")
            now = self.clock()
            if now >= float(record.get("expires_at", 0)) - 120 and now >= float(record.get("earliest_refresh_at", 0)):
                try:
                    tokens = self._request("POST", ISSUER + "/api/accounts/oauth/token", data={
                        "grant_type": "refresh_token", "client_id": record["client_id"],
                        "refresh_token": record["refresh_token"], "resource": API_URL,
                    })
                except ChatGPTError as exc:
                    if exc.code in _TERMINAL_REFRESH:
                        self._clear_tokens(record)
                        self._write(ref, record)
                        raise ChatGPTError("reauthorization_required") from None
                    raise
                self._save_tokens(record, tokens)
            if self.clock() >= float(record.get("expires_at", 0)):
                raise ChatGPTError("token_not_refreshable")
            if "chatgpt.tokens.use.direct" not in record.get("scopes", []):
                raise ChatGPTError("plan_permission_missing")
            return str(record["access_token"])

    @staticmethod
    def _clear_tokens(record: dict[str, Any]) -> None:
        for key in ("access_token", "refresh_token", "id_token"):
            record.pop(key, None)
        record["needs_reauthorization"] = True

    def pause(self, ref: SecretRef) -> None:
        with self._transaction():
            record = self._read(ref)
            if record:
                record["usage_paused"] = True
                self._write(ref, record)

    def resume(self, ref: SecretRef) -> None:
        with self._transaction():
            record = self._read(ref)
            if record:
                record["usage_paused"] = False
                self._write(ref, record)

    def models(self, client_id: str) -> list[dict[str, Any]]:
        token = self.access_token(_record_ref(client_id), allow_paused=True)
        body = self._request("GET", API_URL + "/models", headers={"Authorization": "Bearer " + token})
        return [dict(model) for model in body.get("models", [])
                if isinstance(model, Mapping) and model.get("visibility") == "list" and model.get("slug")]

    def _discovery(self) -> dict[str, Any]:
        discovery = self._request("GET", ISSUER + "/.well-known/openid-configuration")
        if discovery.get("issuer") != ISSUER:
            raise ChatGPTError("invalid_issuer")
        # This client only sends its credentials to the trusted issuer.
        for key in ("jwks_uri", "revocation_endpoint"):
            if not str(discovery.get(key, "")).startswith(ISSUER + "/"):
                raise ChatGPTError("invalid_discovery")
        return discovery

    def _verify_identity(self, token: str, client_id: str, nonce: str) -> dict[str, Any]:
        import jwt
        discovery = self._discovery()
        jwks = self._request("GET", discovery["jwks_uri"])
        try:
            header = jwt.get_unverified_header(token)
            keys = [key for key in jwks.get("keys", []) if key.get("kid") == header.get("kid")]
            if len(keys) != 1:
                raise ValueError("Unknown signing key")
            key = jwt.PyJWK.from_dict(keys[0])
            claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=client_id, issuer=ISSUER,
                                options={"require": ["exp", "iss", "aud", "sub", "nonce"]})
            if not hmac.compare_digest(str(claims["nonce"]).encode(), nonce.encode()):
                raise ValueError("Invalid nonce")
            return claims
        except (jwt.PyJWTError, ValueError, KeyError):
            raise ChatGPTError("invalid_identity") from None

    def login(self, client_id: str | None = None, *, open_browser: Callable[[str], Any] | None = webbrowser.open,
              show_url: Callable[[str], None] = print, timeout: float = 180,
              callback_port: int = 0, on_listener_ready: Callable[[int], None] | None = None) -> dict[str, Any]:
        """Run one bounded browser transaction; never replace identity before validation."""
        if not 0 <= callback_port <= 65535:
            raise ValueError("Callback port must be between 0 and 65535")
        host_id = self.host_id()
        with self._transaction():
            previous = self._read(_record_ref(client_id)) if client_id else {}
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        result: dict[str, str] = {}

        class Callback(BaseHTTPRequestHandler):
            def do_GET(self):
                from urllib.parse import urlsplit
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                valid = parsed.path == "/auth/callback" and len(query.get("state", [])) == 1 and hmac.compare_digest(query["state"][0].encode(), state.encode())
                if valid:
                    result.update({key: values[0] for key, values in query.items() if len(values) == 1})
                self.send_response(200 if valid else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Return to the Pal terminal." if valid else b"Invalid authorization callback.")

            def log_message(self, *args):
                pass

        class CallbackServer(HTTPServer):
            def get_request(self):
                connection, address = super().get_request()
                connection.settimeout(1)
                return connection, address

        with CallbackServer(("127.0.0.1", callback_port), Callback) as server:
            redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
            params = {"client_id": client_id or "dynamic_agent_client", "ext_agent_host_id": host_id,
                      "response_type": "code", "redirect_uri": redirect, "scope": SCOPES,
                      "resource": API_URL, "state": state, "nonce": nonce,
                      "code_challenge_method": "S256", "code_challenge": challenge}
            if not client_id:
                params["agent_name_hint"] = "Pal"
            # Keep the URL safe to display: do not include retained ID tokens.
            if client_id and "chatgpt.tokens.use.direct" not in previous.get("scopes", []):
                params["prompt"] = "consent"
            url = ISSUER + "/api/accounts/authorize?" + urlencode(params)
            if on_listener_ready is not None:
                on_listener_ready(server.server_port)
            show_url(url)
            if open_browser is not None:
                open_browser(url)
            deadline = time.monotonic() + timeout
            while not result and time.monotonic() < deadline:
                server.timeout = min(0.5, max(0, deadline - time.monotonic()))
                server.handle_request()
        if not result:
            raise ChatGPTError("authorization_timeout")
        if result.get("error"):
            raise ChatGPTError("authorization_declined")
        issued = result.get("client_id") or client_id
        if not issued or issued == "dynamic_agent_client" or (client_id and issued != client_id) or not result.get("code"):
            raise ChatGPTError("invalid_callback")
        # Serialize reauthorization against runtime refresh and sign-out.
        with self._transaction():
            current = self._read(_record_ref(issued))
            if not current:
                # Keep the issued registration if exchange/validation fails;
                # the next attempt must not register a duplicate client.
                current = {"client_id": issued, "needs_reauthorization": True, "scopes": []}
                self._write(_record_ref(issued), current)
                index = self._read(_INDEX)
                self._write(_INDEX, {"ids": list(dict.fromkeys([*index.get("ids", []), issued]))})
            tokens = self._request("POST", ISSUER + "/api/accounts/oauth/token", data={
                "grant_type": "authorization_code", "client_id": issued, "code": result["code"],
                "code_verifier": verifier, "redirect_uri": redirect, "resource": API_URL,
            })
            identity = self._verify_identity(str(tokens.get("id_token") or ""), issued, nonce)
            if current.get("subject") and current["subject"] != identity["sub"]:
                raise ChatGPTError("account_mismatch")
            record = {**current, "auth_provider": PROFILE, "client_id": issued, "issuer": ISSUER,
                      "subject": identity["sub"], "email": identity.get("email", ""),
                      "scopes": [], "usage_paused": bool(current.get("usage_paused"))}
            self._save_tokens(record, tokens)
            index = self._read(_INDEX)
            self._write(_INDEX, {"ids": list(dict.fromkeys([*index.get("ids", []), issued]))})
            return self._public(record)

    def logout(self, client_id: str) -> bool:
        """Clear local tokens even if remote revocation cannot be confirmed."""
        with self._transaction():
            record = self._read(_record_ref(client_id))
            confirmed = not bool(record.get("refresh_token"))
            try:
                if not confirmed:
                    discovery = self._discovery()
                    for attempt in range(3):
                        try:
                            self._request("POST", discovery["revocation_endpoint"], data={
                                "token": record["refresh_token"], "token_type_hint": "refresh_token", "client_id": client_id,
                            })
                            confirmed = True
                            break
                        except ChatGPTError as exc:
                            if not exc.retryable or attempt == 2:
                                raise
                            time.sleep(0.25 * (2 ** attempt))
            except ChatGPTError:
                pass
            self._clear_tokens(record)
            self._write(_record_ref(client_id), record)
            return confirmed
