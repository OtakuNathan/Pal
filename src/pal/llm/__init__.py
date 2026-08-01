"""Lazy public facade for Pal's LLM subsystem.

Importing a low-level module such as :mod:`pal.llm.ir` must not load Core,
Execution, database models, SDK transports, or the resident runtime.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS: dict[str, tuple[str, str]] = {
    "LLMActiveModelSnapshot": ("pal.llm.capabilities", "LLMActiveModelSnapshot"),
    "LLMIntrospectionProvider": ("pal.llm.capabilities", "LLMIntrospectionProvider"),
    "LLMModelListItem": ("pal.llm.capabilities", "LLMModelListItem"),
    "LLMModelSnapshot": ("pal.llm.capabilities", "LLMModelSnapshot"),
    "LLMThinkLevelSnapshot": ("pal.llm.capabilities", "LLMThinkLevelSnapshot"),
    "inspect_llm": ("pal.llm.capabilities", "inspect_llm"),
    "llm_status_payload": ("pal.llm.capabilities", "llm_status_payload"),
    "register_with_core": ("pal.llm.capabilities", "register_with_core"),
    "render_llm_status": ("pal.llm.capabilities", "render_llm_status"),
    "LLMGenerationResult": ("pal.llm.contracts", "LLMGenerationResult"),
    "LLMPreflightAdvice": ("pal.llm.contracts", "LLMPreflightAdvice"),
    "LLMPreflightRequest": ("pal.llm.contracts", "LLMPreflightRequest"),
    "LLMRuntimePort": ("pal.llm.contracts", "LLMRuntimePort"),
    "ThinkingChoice": ("pal.llm.contracts", "ThinkingChoice"),
    "ThinkingContract": ("pal.llm.contracts", "ThinkingContract"),
    "generation_result_from_values": ("pal.llm.contracts", "generation_result_from_values"),
    "request_ir_from_prompt": ("pal.llm.contracts", "request_ir_from_prompt"),
    "LLMCredentialResolver": ("pal.llm.credentials", "LLMCredentialResolver"),
    "LLMCredentialUnavailableError": ("pal.llm.credentials", "LLMCredentialUnavailableError"),
    "ResolvedLLMAuth": ("pal.llm.credentials", "ResolvedLLMAuth"),
    "ShapeEndpointInvoker": ("pal.llm.endpoint", "ShapeEndpointInvoker"),
    "LLMEndpointSpec": ("pal.llm.endpoint_spec", "LLMEndpointSpec"),
    "LLMEndpointSpecError": ("pal.llm.endpoint_spec", "LLMEndpointSpecError"),
    "GenerationPolicyIR": ("pal.llm.ir", "GenerationPolicyIR"),
    "ImagePartIR": ("pal.llm.ir", "ImagePartIR"),
    "LLMFinishReason": ("pal.llm.ir", "LLMFinishReason"),
    "LLMMessageIR": ("pal.llm.ir", "LLMMessageIR"),
    "LLMRequestIR": ("pal.llm.ir", "LLMRequestIR"),
    "LLMResponseIR": ("pal.llm.ir", "LLMResponseIR"),
    "LLMResponseUpdate": ("pal.llm.ir", "LLMResponseUpdate"),
    "LLMUsageIR": ("pal.llm.ir", "LLMUsageIR"),
    "MessageRole": ("pal.llm.ir", "MessageRole"),
    "MessageState": ("pal.llm.ir", "MessageState"),
    "ReasoningPartIR": ("pal.llm.ir", "ReasoningPartIR"),
    "ReplayEnvelope": ("pal.llm.ir", "ReplayEnvelope"),
    "TextPartIR": ("pal.llm.ir", "TextPartIR"),
    "ThinkingLevel": ("pal.llm.ir", "ThinkingLevel"),
    "WireShape": ("pal.llm.ir", "WireShape"),
    "ModelHook": ("pal.llm.model_hooks", "ModelHook"),
    "ModelHookError": ("pal.llm.model_hooks", "ModelHookError"),
    "ModelHookRegistry": ("pal.llm.model_hooks", "ModelHookRegistry"),
    "LLMEndpointModel": ("pal.llm.models", "LLMEndpointModel"),
    "PalRuntimeSettingModel": ("pal.llm.models", "PalRuntimeSettingModel"),
    "LLMEndpointRepository": ("pal.llm.repository", "LLMEndpointRepository"),
    "RuntimeSettingRepository": ("pal.llm.repository", "RuntimeSettingRepository"),
    "EndpointResolver": ("pal.llm.runtime", "EndpointResolver"),
    "LLMEndpointInvocationError": ("pal.llm.runtime", "LLMEndpointInvocationError"),
    "LLMEndpointInvokerPort": ("pal.llm.runtime", "LLMEndpointInvokerPort"),
    "LLMRuntime": ("pal.llm.runtime", "LLMRuntime"),
    "PreparedLLMRequest": ("pal.llm.runtime", "PreparedLLMRequest"),
    "build_default_endpoint_invoker": ("pal.llm.runtime", "build_default_endpoint_invoker"),
    "scoped_llm_event_sink": ("pal.llm.runtime", "scoped_llm_event_sink"),
    "EncryptedFileSecretStore": ("pal.llm.secret_store", "EncryptedFileSecretStore"),
    "InMemorySecretStore": ("pal.llm.secret_store", "InMemorySecretStore"),
    "KeyringSecretStore": ("pal.llm.secret_store", "KeyringSecretStore"),
    "SecretRef": ("pal.llm.secret_store", "SecretRef"),
    "SecretStorePort": ("pal.llm.secret_store", "SecretStorePort"),
    "LLMUsageLedger": ("pal.llm.usage", "LLMUsageLedger"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
