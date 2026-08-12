from __future__ import annotations

from pal.execution.tool_semantics import (
    INDIRECT_LOCAL_READ,
    INDIRECT_LOCAL_WRITE,
)
from pal.execution.tool_facade import ToolGuidance

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
        guidance=ToolGuidance(
            purpose="Show artifact manager state.",
            use_when="Diagnosing artifact lifecycle issues — checking hot TTL, hard cap, or max size limits.",
            do_not_use_when="Looking for specific artifacts (use list_artifacts or search_artifacts). Reading artifact content (use read_artifact).",
            failure_next_steps="Read-only diagnostic tool. If limits look wrong, check ArtifactPolicy configuration in the runtime.",
        ),
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
        guidance=ToolGuidance(
            purpose="List recent tagged conversation artifacts visible to the current turn.",
            use_when="The user sent a file (PDF, image, audio, document) through a channel and you need to discover what's available.",
            do_not_use_when="Looking for local filesystem files (use run_shell rg or read_file). No files were sent in this conversation.",
            failure_next_steps="If empty, artifacts may have expired (hot state TTL exceeded) or none were sent. Ask the user to resend.",
        ),
        InputModel=ArtifactCapabilitiesArtifactIntrospectionProviderListInput,
        OutputModel=ArtifactCapabilitiesArtifactIntrospectionProviderListOutput,
        metadata={"async_required": True},
        aliases=("list_artifacts",),
        execution=INDIRECT_LOCAL_READ,
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
        guidance=ToolGuidance(
            purpose="Inspect metadata and available representations for one artifact id.",
            use_when="You have an artifact_id and need to know what representations exist (text, page_text, transcript, image) before reading.",
            do_not_use_when="You already know the representation and just want content (use read_artifact). Inspecting local files (use read_file).",
            failure_next_steps="If artifact_not_found, verify the ID with list_artifacts or search_artifacts. If artifact_expired, ask the user to resend.",
        ),
        InputModel=ArtifactCapabilitiesArtifactIntrospectionProviderInfoInput,
        OutputModel=ArtifactCapabilitiesArtifactIntrospectionProviderInfoOutput,
        metadata={"async_required": True},
        aliases=("artifact_info",),
        execution=INDIRECT_LOCAL_READ,
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
        guidance=ToolGuidance(
            purpose="Read a text-like representation of a scoped artifact by artifact_id. Does not inspect visual image pixels.",
            use_when="Reading text content from a channel-delivered file (PDF text, text file, transcript). Supports page/chunk selection and max_chars.",
            do_not_use_when="Reading local filesystem files (use read_file). Inspecting image pixels: use the inline image directly when the active model supports vision; read_artifact cannot inspect pixels. Audio without transcript (use artifact_transcribe first).",
            failure_next_steps="If representation_unavailable, use artifact_info to inspect the available representations. If not_text_readable, inspect an already-inline image directly with a vision-capable model; there is no separate vision tool. If expired, ask the user to resend.",
        ),
        InputModel=ArtifactCapabilitiesArtifactIntrospectionProviderReadInput,
        OutputModel=ArtifactCapabilitiesArtifactIntrospectionProviderReadOutput,
        metadata={"async_required": True},
        aliases=("read_artifact",),
        execution=INDIRECT_LOCAL_READ,
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
        guidance=ToolGuidance(
            purpose="Search recent tagged conversation artifacts by filename, kind, caption, summary, or time hint.",
            use_when="You know roughly what file the user means (by name, type, or when sent) but lack the exact artifact_id.",
            do_not_use_when="Searching local filesystem or codebase (use run_shell rg or read_file). You already have the artifact_id.",
            failure_next_steps="If no results, try list_artifacts for a broader view, widen the query, or check if the artifact expired.",
        ),
        InputModel=ArtifactCapabilitiesArtifactIntrospectionProviderSearchInput,
        OutputModel=ArtifactCapabilitiesArtifactIntrospectionProviderSearchOutput,
        metadata={"async_required": True},
        aliases=("search_artifacts",),
        execution=INDIRECT_LOCAL_READ,
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
        guidance=ToolGuidance(
            purpose="Mark an artifact search result as selected and refresh its short-lived hot state.",
            use_when="You want to keep a specific artifact's hot state alive across multiple tool calls. Use after search_artifacts when you've identified the right artifact.",
            do_not_use_when="You just need to read an artifact once (read_artifact already refreshes hot state).",
            failure_next_steps="If artifact_not_found, verify the ID with list_artifacts or search_artifacts. If expired, the hard cap was exceeded — ask the user to resend.",
        ),
        InputModel=ArtifactCapabilitiesArtifactIntrospectionProviderSelectInput,
        OutputModel=ArtifactCapabilitiesArtifactIntrospectionProviderSelectOutput,
        metadata={"async_required": True},
        aliases=("artifact_select",),
        execution=INDIRECT_LOCAL_WRITE,
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
        guidance=ToolGuidance(
            purpose="Search inside existing text representations of a known artifact.",
            use_when="For text files, PDF page text/chunks, or existing transcripts.",
            do_not_use_when="Does not inspect image pixels or create transcripts from audio.",
            failure_next_steps="Correct invalid regex or artifact_id.",
        ),
        InputModel=ArtifactCapabilitiesArtifactIntrospectionProviderGrepInput,
        OutputModel=ArtifactCapabilitiesArtifactIntrospectionProviderGrepOutput,
        metadata={"async_required": True},
        aliases=("artifact_grep",),
        execution=INDIRECT_LOCAL_READ,
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
        guidance=ToolGuidance(
            purpose="Request transcription for an audio artifact.",
            use_when="The user sent an audio/voice message and you need the text content.",
            do_not_use_when="The artifact is not audio (use read_artifact for text/pdf). Transcribing local audio files.",
            failure_next_steps="If needs_transcription is returned, the transcriber backend may not be configured — check runtime or inform the user. If expired, ask the user to resend.",
        ),
        InputModel=ArtifactCapabilitiesArtifactIntrospectionProviderTranscribeInput,
        OutputModel=ArtifactCapabilitiesArtifactIntrospectionProviderTranscribeOutput,
        metadata={"async_required": True},
        aliases=("artifact_transcribe",),
        execution=INDIRECT_LOCAL_READ,
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
