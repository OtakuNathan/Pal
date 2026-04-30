from __future__ import annotations

from dataclasses import dataclass

from pal.artifact.service import ArtifactManager
from pal.shared import PromptAssemblyContext, PromptFragment, PromptFragmentProvider
from pal.shared.payloads import extract_text_from_payload


@dataclass
class ArtifactPromptFragmentProvider(PromptFragmentProvider):
    service: ArtifactManager
    provider_id: str = "artifact.prompt.default"
    module_id: str = "artifact"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        scope_key = str(context.metadata.get("artifact_scope_key") or "").strip()
        turn_id = str(context.metadata.get("artifact_turn_id") or "").strip()
        if not scope_key or not turn_id:
            return []
        payload = getattr(context.event, "payload", None)
        exposure = self.service.select_prompt_exposure(
            scope_key,
            turn_id,
            extract_text_from_payload(payload),
            dict(context.metadata.get("llm_capabilities") or {}),
            artifact_ids=_artifact_ids_from_payload(payload),
        )
        if not exposure.text and not exposure.inline_parts:
            return []
        return [
            PromptFragment(
                section="artifact",
                title="Available Artifacts",
                content=exposure.text,
                priority=58,
                metadata={
                    "block_id": "available_artifacts",
                    "content_parts": [part.to_message_part() for part in exposure.inline_parts],
                },
            )
        ]


def _artifact_ids_from_payload(payload: object) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        return ()
    refs = payload.get("artifact_refs")
    if not isinstance(refs, list):
        return ()
    ids: list[str] = []
    for item in refs:
        if isinstance(item, dict):
            artifact_id = str(item.get("artifact_id") or "").strip()
        else:
            artifact_id = str(item or "").strip()
        if artifact_id and artifact_id not in ids:
            ids.append(artifact_id)
    return tuple(ids)
