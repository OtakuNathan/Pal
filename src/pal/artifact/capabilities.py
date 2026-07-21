from __future__ import annotations

from pal.execution.generated_tool_models import (
    ArtifactCapabilitiesArtifactIntrospectionProviderGrepInput,
    ArtifactCapabilitiesArtifactIntrospectionProviderGrepOutput,
    ArtifactCapabilitiesArtifactIntrospectionProviderInfoInput,
    ArtifactCapabilitiesArtifactIntrospectionProviderInfoOutput,
    ArtifactCapabilitiesArtifactIntrospectionProviderListInput,
    ArtifactCapabilitiesArtifactIntrospectionProviderListOutput,
    ArtifactCapabilitiesArtifactIntrospectionProviderReadInput,
    ArtifactCapabilitiesArtifactIntrospectionProviderReadOutput,
    ArtifactCapabilitiesArtifactIntrospectionProviderSearchInput,
    ArtifactCapabilitiesArtifactIntrospectionProviderSearchOutput,
    ArtifactCapabilitiesArtifactIntrospectionProviderSelectInput,
    ArtifactCapabilitiesArtifactIntrospectionProviderSelectOutput,
    ArtifactCapabilitiesArtifactIntrospectionProviderTranscribeInput,
    ArtifactCapabilitiesArtifactIntrospectionProviderTranscribeOutput,
)

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pal.artifact.service import ArtifactManager
from pal.artifact.tools import (
    ArtifactContentSearchTool,
    ArtifactInfoTool,
    ArtifactListTool,
    ArtifactReadTool,
    ArtifactSearchTool,
    ArtifactSelectTool,
    ArtifactTranscribeTool,
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
    execution_runtime: Any | None = None
    module_id: str = "artifact"

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="show",
        description="Show artifact manager state.",
        aliases=("artifact_show",),
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
        description="List recent tagged conversation artifacts visible to the current turn.",
        InputModel=ArtifactCapabilitiesArtifactIntrospectionProviderListInput,
        OutputModel=ArtifactCapabilitiesArtifactIntrospectionProviderListOutput,
        metadata={"async_required": True},
        aliases=("list_artifacts",),
    )
    async def list_artifacts(self, call: CapabilityCall) -> CapabilityResult:
        return await ArtifactListTool(service=self.service).ainvoke(
            dict(call.args), runtime=self.execution_runtime, turn_id=str(call.meta.get("turn_id") or "") or None
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="artifact",
        action_name="info",
        description="Inspect metadata and available representations for one artifact id.",
        InputModel=ArtifactCapabilitiesArtifactIntrospectionProviderInfoInput,
        OutputModel=ArtifactCapabilitiesArtifactIntrospectionProviderInfoOutput,
        metadata={"async_required": True},
        aliases=("artifact_info",),
    )
    async def info(self, call: CapabilityCall) -> CapabilityResult:
        return await ArtifactInfoTool(service=self.service).ainvoke(
            dict(call.args), runtime=self.execution_runtime, turn_id=str(call.meta.get("turn_id") or "") or None
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="artifact",
        action_name="read",
        description="Read a text-like representation of a scoped artifact by artifact_id. Does not inspect visual image pixels.",
        InputModel=ArtifactCapabilitiesArtifactIntrospectionProviderReadInput,
        OutputModel=ArtifactCapabilitiesArtifactIntrospectionProviderReadOutput,
        metadata={"async_required": True},
        aliases=("read_artifact",),
    )
    async def read(self, call: CapabilityCall) -> CapabilityResult:
        return await ArtifactReadTool(service=self.service).ainvoke(
            dict(call.args), runtime=self.execution_runtime, turn_id=str(call.meta.get("turn_id") or "") or None
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="artifact",
        action_name="search",
        description="Search recent tagged conversation artifacts by filename, kind, caption, summary, or time hint.",
        InputModel=ArtifactCapabilitiesArtifactIntrospectionProviderSearchInput,
        OutputModel=ArtifactCapabilitiesArtifactIntrospectionProviderSearchOutput,
        metadata={"async_required": True},
        aliases=("search_artifacts",),
    )
    async def search(self, call: CapabilityCall) -> CapabilityResult:
        return await ArtifactSearchTool(service=self.service).ainvoke(
            dict(call.args), runtime=self.execution_runtime, turn_id=str(call.meta.get("turn_id") or "") or None
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="artifact",
        action_name="select",
        description="Mark an artifact search result as selected and refresh its short-lived hot state.",
        InputModel=ArtifactCapabilitiesArtifactIntrospectionProviderSelectInput,
        OutputModel=ArtifactCapabilitiesArtifactIntrospectionProviderSelectOutput,
        metadata={"async_required": True},
        aliases=("artifact_select",),
    )
    async def select(self, call: CapabilityCall) -> CapabilityResult:
        return await ArtifactSelectTool(service=self.service).ainvoke(
            dict(call.args), runtime=self.execution_runtime, turn_id=str(call.meta.get("turn_id") or "") or None
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="artifact",
        action_name="grep",
        description=(
            "Search inside existing text representations of a known artifact, such as text files, PDF page text/chunks, "
            "or an already-created transcript. Does not inspect image pixels or create transcripts from audio."
        ),
        InputModel=ArtifactCapabilitiesArtifactIntrospectionProviderGrepInput,
        OutputModel=ArtifactCapabilitiesArtifactIntrospectionProviderGrepOutput,
        metadata={"async_required": True},
        aliases=("artifact_grep",),
    )
    async def content_search(self, call: CapabilityCall) -> CapabilityResult:
        return await ArtifactContentSearchTool(service=self.service).ainvoke(
            dict(call.args), runtime=self.execution_runtime, turn_id=str(call.meta.get("turn_id") or "") or None
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="artifact",
        action_name="transcribe",
        description="Request transcription for an audio artifact.",
        InputModel=ArtifactCapabilitiesArtifactIntrospectionProviderTranscribeInput,
        OutputModel=ArtifactCapabilitiesArtifactIntrospectionProviderTranscribeOutput,
        metadata={"async_required": True},
        aliases=("artifact_transcribe",),
    )
    async def transcribe(self, call: CapabilityCall) -> CapabilityResult:
        return await ArtifactTranscribeTool(service=self.service).ainvoke(
            dict(call.args), runtime=self.execution_runtime, turn_id=str(call.meta.get("turn_id") or "") or None
        )


def register_with_core(context: "MainContext", service: ArtifactManager) -> ModuleHandle:
    from pal.artifact.prompt import ArtifactPromptFragmentProvider

    context.execution_runtime.register_provider_ref("artifact:artifact", service)
    provider = ArtifactIntrospectionProvider(service=service, execution_runtime=context.execution_runtime)
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
