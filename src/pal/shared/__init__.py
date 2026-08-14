"""Lazy facade for cross-module Pal protocols.

Importing a leaf protocol such as :mod:`pal.shared.enums` must not initialize
Execution, capability registries, channel adapters, or any resident runtime.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS: dict[str, tuple[str, str]] = {
    "DURABLE_TRUTH_SURFACES": ("pal.shared.contracts", "DURABLE_TRUTH_SURFACES"),
    "OwnershipBoundary": ("pal.shared.contracts", "OwnershipBoundary"),
    "RUNTIME_ONLY_SURFACES": ("pal.shared.contracts", "RUNTIME_ONLY_SURFACES"),
    "BoundCapabilityAction": ("pal.shared.capability_forest", "BoundCapabilityAction"),
    "CapabilityActionBlueprint": ("pal.shared.capability_forest", "CapabilityActionBlueprint"),
    "CapabilityNodeBlueprint": ("pal.shared.capability_forest", "CapabilityNodeBlueprint"),
    "HydratedCapabilityNode": ("pal.shared.capability_forest", "HydratedCapabilityNode"),
    "INTROSPECTION_NAMESPACE": ("pal.shared.capability_forest", "INTROSPECTION_NAMESPACE"),
    "MountedSubtreeHandle": ("pal.shared.capability_forest", "MountedSubtreeHandle"),
    "OPERATION_NAMESPACE": ("pal.shared.capability_forest", "OPERATION_NAMESPACE"),
    "SINGLETON_TARGET": ("pal.shared.capability_forest", "SINGLETON_TARGET"),
    "capability_action": ("pal.shared.capability_forest", "capability_action"),
    "capability_node": ("pal.shared.capability_forest", "capability_node"),
    "AgentOutputPort": ("pal.shared.agent_io", "AgentOutputPort"),
    "ChannelAdapter": ("pal.shared.agent_io", "ChannelAdapter"),
    "ChannelDeliveryError": ("pal.shared.agent_io", "ChannelDeliveryError"),
    "ChannelEnvelope": ("pal.shared.agent_io", "ChannelEnvelope"),
    "ChannelMessage": ("pal.shared.agent_io", "ChannelMessage"),
    "ChannelMessageReceipt": ("pal.shared.agent_io", "ChannelMessageReceipt"),
    "ChannelNormalizer": ("pal.shared.agent_io", "ChannelNormalizer"),
    "ChannelRuntimePort": ("pal.shared.agent_io", "ChannelRuntimePort"),
    "ChannelStreamUpdate": ("pal.shared.agent_io", "ChannelStreamUpdate"),
    "EndpointConfig": ("pal.shared.agent_io", "EndpointConfig"),
    "QueuedAttachment": ("pal.shared.agent_io", "QueuedAttachment"),
    "QueuedReply": ("pal.shared.agent_io", "QueuedReply"),
    "QueuedStatus": ("pal.shared.agent_io", "QueuedStatus"),
    "QueuedStreamUpdate": ("pal.shared.agent_io", "QueuedStreamUpdate"),
    "ResponseHandle": ("pal.shared.agent_io", "ResponseHandle"),
    "TurnDeliveryBinding": ("pal.shared.agent_io", "TurnDeliveryBinding"),
    "EffectKind": ("pal.shared.enums", "EffectKind"),
    "EventKind": ("pal.shared.enums", "EventKind"),
    "GuardAction": ("pal.shared.enums", "GuardAction"),
    "GuardStatus": ("pal.shared.enums", "GuardStatus"),
    "LLMFinishReason": ("pal.shared.enums", "LLMFinishReason"),
    "LLMPreflightStatus": ("pal.shared.enums", "LLMPreflightStatus"),
    "LLMResponseMode": ("pal.shared.enums", "LLMResponseMode"),
    "ChannelStreamUpdateKind": ("pal.shared.enums", "ChannelStreamUpdateKind"),
    "RuntimeStatus": ("pal.shared.enums", "RuntimeStatus"),
    "SourceKind": ("pal.shared.enums", "SourceKind"),
    "IntrospectionCall": ("pal.shared.introspection", "IntrospectionCall"),
    "LifecycleIntrospectionPort": ("pal.shared.introspection", "LifecycleIntrospectionPort"),
    "IntrospectionPort": ("pal.shared.introspection", "IntrospectionPort"),
    "IntrospectionResult": ("pal.shared.introspection", "IntrospectionResult"),
    "PromptAssemblyContext": ("pal.shared.prompting", "PromptAssemblyContext"),
    "PromptFragment": ("pal.shared.prompting", "PromptFragment"),
    "PromptIR": ("pal.shared.prompting", "PromptIR"),
    "PromptIRBlock": ("pal.shared.prompting", "PromptIRBlock"),
    "PromptFragmentProvider": ("pal.shared.prompting", "PromptFragmentProvider"),
    "render_runtime_reminder": ("pal.shared.prompt_rendering", "render_runtime_reminder"),
    "render_system_reminder": ("pal.shared.prompt_rendering", "render_system_reminder"),
    "render_xml_block": ("pal.shared.prompt_rendering", "render_xml_block"),
    "default_tool_result_text": ("pal.shared.tool_protocol", "default_tool_result_text"),
    "CompleteResult": ("pal.shared.tool_protocol", "CompleteResult"),
    "EffectOutcome": ("pal.shared.tool_protocol", "EffectOutcome"),
    "FailedResult": ("pal.shared.tool_protocol", "FailedResult"),
    "PagedResult": ("pal.shared.tool_protocol", "PagedResult"),
    "RejectedResult": ("pal.shared.tool_protocol", "RejectedResult"),
    "RetryDirective": ("pal.shared.tool_protocol", "RetryDirective"),
    "ToolAffordance": ("pal.shared.tool_protocol", "ToolAffordance"),
    "ToolCallIR": ("pal.shared.tool_protocol", "ToolCallIR"),
    "ToolDefinitionIR": ("pal.shared.tool_protocol", "ToolDefinitionIR"),
    "ToolExecutionResult": ("pal.shared.tool_protocol", "ToolExecutionResult"),
    "ToolInvocationResult": ("pal.shared.tool_protocol", "ToolInvocationResult"),
    "ToolResultIR": ("pal.shared.tool_protocol", "ToolResultIR"),
    "new_tool_call": ("pal.shared.tool_protocol", "new_tool_call"),
    "BunshinApprovalDecision": ("pal.shared.messages", "BunshinApprovalDecision"),
    "BunshinInvocationPack": ("pal.shared.messages", "BunshinInvocationPack"),
    "ProactiveTriggerEvent": ("pal.shared.messages", "ProactiveTriggerEvent"),
    "extract_text_from_payload": ("pal.shared.payloads", "extract_text_from_payload"),
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
