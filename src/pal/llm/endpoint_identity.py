"""Persistent endpoint identity: one access provider and exact upstream model."""
from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import urlsplit

# A provider slug cannot contain the separator. Model IDs are opaque (including
# gateway namespaces and case), not names to be parsed or normalized.
IDENTITY_CHECK = (
    "provider GLOB '[a-z]*' AND provider NOT GLOB '*[^a-z0-9_]*' "
    "AND instr(provider, char(0)) = 0 "
    "AND length(model_id) > 0 AND model_id = trim(model_id, char(32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288)) "
    "AND model_id NOT GLOB ('*[' || char(1) || '-' || char(31) || char(127) || ']*') "
    "AND instr(model_id, char(0)) = 0 "
    "AND endpoint_id = provider || '-' || model_id"
)


def canonical_endpoint_id(provider: str, model_id: str) -> str:
    if not isinstance(provider, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", provider):
        raise ValueError("provider must be a lowercase slug (letters, digits, underscores)")
    if (not isinstance(model_id, str) or not model_id or model_id != model_id.strip()
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in model_id)):
        raise ValueError("model_id must be an exact non-empty upstream model ID without control characters")
    return f"{provider}-{model_id}"


def bind_endpoint_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    provider = result.get("provider")
    endpoint_id = canonical_endpoint_id(provider, result.get("model_id"))
    if "endpoint_id" in result and result["endpoint_id"] != endpoint_id:
        raise ValueError(f"endpoint_id must be {endpoint_id!r}")
    result["endpoint_id"] = endpoint_id
    capabilities = dict(result.get("capabilities_blob") or {})
    if provider == "chatgpt":
        from pal.llm.chatgpt import PROFILE, API_URL
        if (result.get("auth_kind") != "oauth" or result.get("wire_shape") != "openai_response"
                or str(result.get("base_url", "")).rstrip("/") != API_URL):
            raise ValueError("chatgpt requires OAuth and official OpenAI Responses")
        if not str(result.get("credential_ref", "")).startswith("pal.chatgpt:") or not str(result["credential_ref"]).removeprefix("pal.chatgpt:"):
            raise ValueError("chatgpt requires a saved pal.chatgpt:<account> credential reference")
        capabilities["access_profile"] = PROFILE
    elif capabilities.get("access_profile") == "openai_chatgpt":
        raise ValueError("ChatGPT OAuth endpoints require provider=chatgpt")
    elif result.get("auth_kind") == "oauth" and str(result.get("credential_ref", "")).startswith("pal.chatgpt:"):
        raise ValueError("ChatGPT OAuth credentials require provider=chatgpt")
    result["capabilities_blob"] = capabilities
    return result


def legacy_provider(payload: Mapping[str, Any]) -> str:
    provider = str(payload["provider"]).strip().lower()
    host = urlsplit(str(payload["base_url"])).hostname
    if provider == "zhipu":
        provider = {"open.bigmodel.cn": "bigmodel", "api.z.ai": "zai"}.get(host, provider)
    if payload.get("auth_kind") == "oauth" and (
        (payload.get("capabilities_blob") or {}).get("access_profile") == "openai_chatgpt"
        or str(payload.get("credential_ref", "")).startswith("pal.chatgpt:")
    ):
        provider = "chatgpt"
    return provider
