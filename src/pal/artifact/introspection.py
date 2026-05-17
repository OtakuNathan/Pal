from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pal.artifact.service import ArtifactManager
from pal.artifact.tools import (
    ArtifactContentSearchTool,
    ArtifactInfoTool,
    ArtifactListTool,
    ArtifactReadTool,
    ArtifactSearchTool,
    ArtifactSelectTool,
    ArtifactTranscribeTool,
    artifact_args_schema,
    artifact_result_schema,
)
from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
from pal.execution.contracts import CapabilityCall, CapabilityResult
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    OPERATION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
    capability_node,
)
from pal.shared.result_rendering import render_titled_structured_for_llm

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:artifact",
    target_kind="module",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:artifact",
    target_kind="module",
)
@dataclass
class ArtifactIntrospectionProvider:
    service: ArtifactManager
    module_id: str = "artifact"

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="show",
        description="Show artifact manager state.",
    )
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        structured = {
            "hot_ttl_seconds": self.service.policy.lifecycle.hot_ttl_seconds,
            "hard_cap_seconds": self.service.policy.lifecycle.hard_cap_seconds,
            "max_original_bytes": self.service.policy.limits.max_original_bytes,
        }
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="artifact snapshot",
            structured=structured,
            llm_text=render_titled_structured_for_llm("Artifact snapshot", structured),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="artifact",
        action_name="list",
        description="List short-lived conversation artifacts visible to the current turn.",
        args_schema=artifact_args_schema("op_artifact_list"),
        result_schema=artifact_result_schema("op_artifact_list"),
        metadata={"async_required": True},
    )
    def list_artifacts(self, call: CapabilityCall) -> CapabilityResult:
        _ = call
        return _async_required("op_artifact_list")

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="artifact",
        action_name="info",
        description="Inspect metadata and available representations for one artifact id.",
        args_schema=artifact_args_schema("op_artifact_info"),
        result_schema=artifact_result_schema("op_artifact_info"),
        metadata={"async_required": True},
    )
    def info(self, call: CapabilityCall) -> CapabilityResult:
        _ = call
        return _async_required("op_artifact_info")

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="artifact",
        action_name="read",
        description="Read a text-like representation of a scoped artifact by artifact_id. Does not inspect visual image pixels.",
        args_schema=artifact_args_schema("op_artifact_read"),
        result_schema=artifact_result_schema("op_artifact_read"),
        metadata={"async_required": True},
    )
    def read(self, call: CapabilityCall) -> CapabilityResult:
        _ = call
        return _async_required("op_artifact_read")

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="artifact",
        action_name="search",
        description="Find a recent conversation artifact by filename, type, summary, or time hint.",
        args_schema=artifact_args_schema("op_artifact_search"),
        result_schema=artifact_result_schema("op_artifact_search"),
        metadata={"async_required": True},
    )
    def search(self, call: CapabilityCall) -> CapabilityResult:
        _ = call
        return _async_required("op_artifact_search")

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="artifact",
        action_name="select",
        description="Mark an artifact search result as selected and refresh its short-lived hot state.",
        args_schema=artifact_args_schema("op_artifact_select"),
        result_schema=artifact_result_schema("op_artifact_select"),
        metadata={"async_required": True},
    )
    def select(self, call: CapabilityCall) -> CapabilityResult:
        _ = call
        return _async_required("op_artifact_select")

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="artifact",
        action_name="grep",
        description=(
            "Search inside existing text representations of a known artifact, such as text files, PDF page text/chunks, "
            "or an already-created transcript. Does not inspect image pixels or create transcripts from audio."
        ),
        args_schema=artifact_args_schema("op_artifact_grep"),
        result_schema=artifact_result_schema("op_artifact_grep"),
        metadata={"async_required": True},
    )
    def content_search(self, call: CapabilityCall) -> CapabilityResult:
        _ = call
        return _async_required("op_artifact_grep")

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="artifact",
        action_name="transcribe",
        description="Request transcription for an audio artifact.",
        args_schema=artifact_args_schema("op_artifact_transcribe"),
        result_schema=artifact_result_schema("op_artifact_transcribe"),
        metadata={"async_required": True},
    )
    def transcribe(self, call: CapabilityCall) -> CapabilityResult:
        _ = call
        return _async_required("op_artifact_transcribe")


def register_with_core(context: "MainContext", service: ArtifactManager) -> ModuleHandle:
    from pal.artifact.prompt import ArtifactPromptFragmentProvider

    context.execution_runtime.register_provider_ref("artifact:artifact", service)
    context.execution_runtime.register_tool(ArtifactListTool(service=service))
    context.execution_runtime.register_tool(ArtifactInfoTool(service=service))
    context.execution_runtime.register_tool(ArtifactReadTool(service=service))
    context.execution_runtime.register_tool(ArtifactSearchTool(service=service))
    context.execution_runtime.register_tool(ArtifactSelectTool(service=service))
    context.execution_runtime.register_tool(ArtifactContentSearchTool(service=service))
    context.execution_runtime.register_tool(ArtifactTranscribeTool(service=service))
    provider = ArtifactIntrospectionProvider(service=service)
    prompt_provider = ArtifactPromptFragmentProvider(service=service)
    handle = ModuleHandle(
        module_id="artifact",
        tier=MODULE_TIER_CORE_FOUNDATION,
        detachable=False,
        introspection_provider=provider,
        prompt_fragment_providers=[prompt_provider],
        ports={"artifact": service},
    )
    context.register_module(handle)
    context.prompt_fragment_registry.register(prompt_provider)
    return handle


def _async_required(tool_name: str) -> CapabilityResult:
    structured = {"reason": "async_required", "tool": tool_name}
    return CapabilityResult(
        status=RuntimeStatus.INVALID,
        text=f"{tool_name} requires async turn context.",
        structured=structured,
        llm_text=render_titled_structured_for_llm("Artifact tool unavailable", structured),
    )
