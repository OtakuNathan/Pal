from __future__ import annotations

from pal.execution.tool_semantics import (
    INDIRECT_LOCAL_READ,
    INDIRECT_LOCAL_WRITE,
    INDIRECT_UNSAFE_LOCAL_WRITE,
)
from pal.execution.tool_facade import StrictToolModel, ToolGuidance
from pydantic import Field

from pal.execution.generated_tool_models import (
    ArtifactCapabilitiesArtifactIntrospectionProviderInfoInput,
    ArtifactCapabilitiesArtifactIntrospectionProviderInfoOutput,
    ArtifactCapabilitiesArtifactIntrospectionProviderSearchInput,
    ArtifactCapabilitiesArtifactIntrospectionProviderSearchOutput,
)

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pal.artifact.service import ArtifactManager
from pal.artifact.tools import (
    ArtifactInfoTool,
    ArtifactImportTool,
    ArtifactSearchTool,
    _scope_from_runtime,
)
from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
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
from pal.shared.inspection import inspection_snapshot

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


class ArtifactImportInput(StrictToolModel):
    path: str = Field(min_length=1, description="Local file path to copy into the current conversation's managed artifacts.")


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
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="artifact",
        action_name="import",
        guidance=ToolGuidance(
            search_objects=('artifact', 'artifacts', 'image', 'images'),
            search_terms=('file', 'files', 'screenshot', 'screenshots', 'pdf', 'document', 'documents', 'audio', 'attach', 'load',),
            search_intents=('register a local file as a conversation attachment', 'show a local image to the model', 'normalize a pdf into page files'),
            purpose="Import a local file or screenshot into the current conversation and attach its artifact reference for model input.",
            use_when='Registering a local image, screenshot, PDF, audio or document for this conversation. Unsupported formats are retained as files with metadata; only images may attach pixels automatically. Extracted OCR or transcript text is not proof of visual inspection; pixels are inspected only when actually attached to a vision-capable model.',
            do_not_use_when="The same image is already inline. A PDF artifact_id does not mean its page images are inline; import an image_file_path from its page index when pixels are needed. Reading ordinary source text (use read_file).",
            failure_next_steps="Check the local path, image validity and size. Registration works without vision; core attaches pixels only when the selected model supports vision and image budgets permit. If pixels are not attached, discover a suitable OCR/image-analysis capability when needed; OCR is text extraction, not full visual inspection. If a processing failure includes an artifact_id, inspect it with inspect_artifact_info. Reconcile uncertain outcomes with search_artifacts before retrying.",
        ),
        InputModel=ArtifactImportInput,
        metadata={"async_required": True},
        aliases=("import_artifact",),
        execution=INDIRECT_UNSAFE_LOCAL_WRITE,
    )
    async def import_file(self, call: CapabilityCall) -> CapabilityResult:
        return await ArtifactImportTool(service=self.service).ainvoke(
            dict(call.args), runtime=self.execution_runtime, turn_id=str(call.meta.get("turn_id") or "") or None
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="show",
        guidance=ToolGuidance(
            search_objects=('state', 'states', 'attachment', 'attachments'),
            search_terms=('status', 'show', 'get', 'health', 'lifecycle', 'limits', 'count'),
            purpose="Inspect artifact manager policy and conversation-scoped resource and lifecycle counts.",
            use_when="Diagnosing artifact lifecycle issues — checking hot TTL, hard cap, or max size limits.",
            do_not_use_when="Looking for specific attachments (use search_artifacts). Reading their contents (use the provided file paths with read_file/rg or a suitable processor).",
            failure_next_steps="Read-only diagnostic tool. If limits look wrong, check ArtifactPolicy configuration in the runtime.",
        ),
        aliases=("inspect_artifact_state",),
    )
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        structured = {
            "hot_ttl_seconds": self.service.policy.lifecycle.hot_ttl_seconds,
            "hard_cap_seconds": self.service.policy.lifecycle.hard_cap_seconds,
            "max_original_bytes": self.service.policy.limits.max_original_bytes,
        }
        structured = inspection_snapshot(structured, sections={
            "inventory": lambda: self.service.repository.inspect_counts(
                scope_key=_scope_from_runtime(self.execution_runtime, str(call.meta.get("turn_id") or "") or None)),
        })
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
        action_name="info",
        guidance=ToolGuidance(
            search_objects=('artifact', 'artifacts', 'attachment', 'attachments', 'file', 'files', 'metadata', 'path', 'paths', 'index'),
            search_terms=('show', 'get', 'details', 'processing', 'normalization', 'filename', 'extension', 'mime', 'pdf', 'page'),
            search_intents=('inspect attachment file type and original paths', 'find pdf page text and image index'),
            purpose="Inspect one attachment's file type, normalization status, original/processed file paths and PDF page index.",
            use_when="Recovering file paths or diagnosing attachment processing when its automatic file description is missing or insufficient. This returns file information, not document contents or image pixels.",
            do_not_use_when="File paths are already available: use read_file/rg or a tool suited to the file type. Importing a new local file (use import_artifact).",
            failure_next_steps="If artifact_not_found, recover a current artifact_id with search_artifacts. If artifact_handler_retired, its managed bytes were deleted; ask the user to attach it again.",
        ),
        InputModel=ArtifactCapabilitiesArtifactIntrospectionProviderInfoInput,
        OutputModel=ArtifactCapabilitiesArtifactIntrospectionProviderInfoOutput,
        metadata={"async_required": True},
        aliases=("inspect_artifact_info",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    async def info(self, call: CapabilityCall) -> CapabilityResult:
        return await ArtifactInfoTool(service=self.service).ainvoke(
            dict(call.args), runtime=self.execution_runtime, turn_id=str(call.meta.get("turn_id") or "") or None
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="artifact",
        action_name="search",
        guidance=ToolGuidance(
            search_objects=('artifact', 'artifacts'),
            search_terms=('find', 'lookup', 'list', 'recent', 'filename', 'caption'),
            purpose="Find or list available attachments in the current conversation, returning file metadata and paths without body contents.",
            use_when='Find attachments by filename, kind, caption or summary; omit query to list recent attachments. Results are paginated: use next_offset while has_more=true. An empty filtered result does not establish expiry; broaden or omit query before asking for a resend.',
            do_not_use_when="Searching inside document contents or a local codebase: use rg on the supplied paths. You already have the needed file description.",
            failure_next_steps="Omit query for the unfiltered conversation inventory; follow next_offset to see remaining results. Inspect a known artifact with inspect_artifact_info if its processing or availability is uncertain.",
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



def register_with_core(context: "MainContext", service: ArtifactManager) -> ModuleHandle:
    from pal.artifact.prompt import ArtifactPromptFragmentProvider

    context.execution_runtime.register_provider_ref("artifact:artifact", service)
    provider = ArtifactIntrospectionProvider(service=service, execution_runtime=context.execution_runtime)
    prompt_provider = ArtifactPromptFragmentProvider(service=service)
    handle = ModuleHandle(
        module_id="artifact",
        tier=MODULE_TIER_DETACHABLE,
        detachable=True,
        introspection_provider=provider,
        prompt_fragment_providers=[prompt_provider],
        provider_refs=["artifact:artifact"],
        ports={"artifact": service, "provider:artifact:artifact": service},
    )
    context.register_module(handle)
    context.prompt_fragment_registry.register(prompt_provider)
    return handle
