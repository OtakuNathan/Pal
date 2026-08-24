from __future__ import annotations

from typing import Any

from pal.shared import (
    PromptAssemblyContext,
    PromptFragment,
    PromptIR,
    PromptIRBlock,
)
from pal.shared.payloads import extract_text_from_payload
from pal.shared.prompt_rendering import render_runtime_context_update, render_runtime_reminder, render_system_reminder, render_xml_block


_BEHAVIOR_GUIDANCE_HEADER = (
    "Behavior guidance is behavior-owned routing metadata. It may include resident learned rules and temporary route hints produced by advise_behavior.\n"
    "Temporary behavior guidance retires automatically; learned or resident behavior guidance may persist.\n"
    "Consider matching guidance before choosing a route.\n"
    "Follow relevant hints unless higher-priority policy, current user instruction, live truth, or capability policy makes them inappropriate."
)
_BEHAVIOR_GUIDANCE_HEADER_LINES = frozenset(_BEHAVIOR_GUIDANCE_HEADER.splitlines())


def normalize_prompt_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw_message in list(messages or []):
        if not isinstance(raw_message, dict):
            continue
        message = dict(raw_message)
        role = str(message.get("role") or "").strip()
        if role == "user" and normalized and str(normalized[-1].get("role") or "").strip() == "user":
            normalized[-1]["content"] = _merge_user_message_content(normalized[-1].get("content"), message.get("content"))
            continue
        normalized.append(message)
    return normalized


def _merge_user_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
    if isinstance(left, str) and isinstance(right, str):
        if left.strip() and right.strip():
            return f"{left.rstrip()}\n\n{right.lstrip()}"
        return left or right
    return [*_message_content_parts(left), *_message_content_parts(right)]


def _message_content_parts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    text = str(value or "")
    return [{"type": "text", "text": text}] if text else []


class PromptCompiler:
    def __init__(self, context) -> None:
        self.context = context

    def _project_llm_text(self, value: object) -> str:
        runtime = getattr(self.context, "execution_runtime", None)
        projector = getattr(runtime, "project_llm_text", None)
        if callable(projector):
            return str(projector(value))
        return str(value or "")

    def collect_prompt_fragments(self, assembly_context: PromptAssemblyContext) -> list[PromptFragment]:
        indexed_fragments: list[tuple[int, int, PromptFragment]] = []
        for registration_order, provider in enumerate(self.context.prompt_fragment_registry.list_for_prompt()):
            for fragment in provider.build_prompt_fragments(assembly_context):
                indexed_fragments.append((fragment.priority, registration_order, fragment))
        indexed_fragments.sort(key=lambda item: (item[0], item[1]))
        return [fragment for _, _, fragment in indexed_fragments]

    def build_prompt_ir(self, assembly_context: PromptAssemblyContext) -> PromptIR:
        if assembly_context.turn_kind == "failure":
            return self._build_failure_prompt_ir(assembly_context)
        assembly_context = self._ensure_memory_pack(assembly_context)
        fragments = self.collect_prompt_fragments(assembly_context)
        system_blocks: list[PromptIRBlock] = []
        developer_blocks: list[PromptIRBlock] = []
        user_context_blocks: list[PromptIRBlock] = []
        runtime_reminder_blocks: list[PromptIRBlock] = []

        for fragment in fragments:
            normalized_section = self._normalize_prompt_section(fragment.section)
            rendered_body = str(fragment.content).strip()
            if not rendered_body and not self._preserve_empty_protocol_fragment(fragment):
                continue
            prompt_target = self._prompt_target(fragment)
            self._validate_prompt_target(
                fragment,
                normalized_section=normalized_section,
                prompt_target=prompt_target,
            )
            if prompt_target == "runtime_reminder":
                runtime_reminder_blocks.append(
                    self._runtime_reminder_block(
                        fragment,
                        normalized_section=normalized_section,
                        rendered_body=rendered_body,
                    )
                )
                continue
            if normalized_section == "memory":
                user_context_blocks.append(
                    PromptIRBlock(
                        block_id=str(fragment.metadata.get("block_id") or "memory_projection"),
                        title=str(fragment.title or "Memory Projection"),
                        content=rendered_body,
                        metadata={
                            **dict(fragment.metadata),
                            "source_section": fragment.section,
                            "source_title": fragment.title,
                        },
                    )
                )
                continue
            if normalized_section == "artifact":
                user_context_blocks.append(
                    PromptIRBlock(
                        block_id=str(fragment.metadata.get("block_id") or "available_artifacts"),
                        title=str(fragment.title or "Available Artifacts"),
                        content=rendered_body,
                        metadata={
                            **dict(fragment.metadata),
                            "source_section": fragment.section,
                            "source_title": fragment.title,
                        },
                    )
                )
                continue
            block = self._static_instruction_block(
                fragment,
                normalized_section=normalized_section,
                rendered_body=rendered_body,
            )
            (system_blocks if prompt_target == "system" else developer_blocks).append(block)

        runtime_reminder_blocks.extend(
            self._build_runtime_overlay_blocks(assembly_context)
        )

        ordered_system_blocks = self._order_system_blocks(system_blocks)
        ordered_developer_blocks = self._order_developer_blocks(developer_blocks)
        ordered_user_blocks = self._order_user_context_blocks(user_context_blocks, turn_kind=assembly_context.turn_kind)
        ordered_reminder_blocks = self._order_runtime_reminder_blocks(runtime_reminder_blocks)
        primary_input = self._extract_primary_input_text(assembly_context)
        return PromptIR(
            system_blocks=tuple(ordered_system_blocks),
            developer_blocks=tuple(ordered_developer_blocks),
            user_context_blocks=tuple(ordered_user_blocks),
            runtime_reminder_blocks=tuple(ordered_reminder_blocks),
            primary_input=primary_input,
            turn_kind=assembly_context.turn_kind,
        )

    def build_canonical_prompt(
        self,
        assembly_context: PromptAssemblyContext,
        *,
        max_output_tokens: int = 1024,
        model_hint: str | None = None,
    ):
        from pal.llm.conversions import request_ir_from_prompt

        prompt_ir = self.build_prompt_ir(assembly_context)
        messages = self._resolve_artifact_images(
            normalize_prompt_messages(self._compile_prompt_ir_messages(prompt_ir))
        )
        return request_ir_from_prompt(
            messages=messages,
            max_output_tokens=max_output_tokens,
            model_hint=model_hint,
            temperature=None,
            metadata={
                "fragment_sections": [
                    block.block_id
                    for block in (*prompt_ir.system_blocks, *prompt_ir.developer_blocks)
                ],
                "system_sections": [block.block_id for block in prompt_ir.system_blocks],
                "developer_sections": [block.block_id for block in prompt_ir.developer_blocks],
                "user_context_blocks": [block.block_id for block in prompt_ir.user_context_blocks],
                "reminder_sections": [block.block_id for block in prompt_ir.runtime_reminder_blocks],
                "prompt_ir": self._prompt_ir_debug_dict(prompt_ir),
                "runtime_reminder_text": self._render_final_runtime_reminder(
                    prompt_ir.runtime_reminder_blocks
                ),
                **{
                    key: assembly_context.metadata[key]
                    for key in ("preferred_endpoint_id", "preferred_model_id")
                    if key in assembly_context.metadata
                },
            },
        )

    def _build_failure_prompt_ir(self, assembly_context: PromptAssemblyContext) -> PromptIR:
        identity_blocks: list[PromptIRBlock] = []
        persona_blocks: list[PromptIRBlock] = []
        for fragment in self.collect_prompt_fragments(
            PromptAssemblyContext(
                event=assembly_context.event,
                core_mode=assembly_context.core_mode,
                turn_kind="chat",
                task_id=assembly_context.task_id,
                work_order_id=assembly_context.work_order_id,
                metadata={},
            )
        ):
            normalized_section = self._normalize_prompt_section(fragment.section)
            if normalized_section not in {"identity", "persona"}:
                continue
            rendered_body = str(fragment.content).strip()
            if not rendered_body:
                continue
            block = self._static_instruction_block(
                fragment,
                normalized_section=normalized_section,
                rendered_body=rendered_body,
            )
            (identity_blocks if normalized_section == "identity" else persona_blocks).append(block)
        rules = str(
            assembly_context.metadata.get("failure_rules")
            or (
                "You are performing bounded self-healing inside PalCore.\n"
                "Work on only the primary blocker.\n"
                "Use only the allowed capabilities.\n"
                "Prefer the smallest safe repair.\n"
                "Do not expand scope to secondary issues.\n"
                "If safe repair is insufficient, conclude with verification_status = failed."
            )
        ).strip()
        system_blocks = [
            *identity_blocks,
            PromptIRBlock(block_id="operating_rules", title="Operating Rules", content=rules),
        ]
        runtime_blocks = self._build_runtime_overlay_blocks(assembly_context)
        user_context_blocks: list[PromptIRBlock] = []
        recent_summaries = str(assembly_context.metadata.get("failure_recent_summaries") or "").strip()
        if recent_summaries:
            user_context_blocks.append(
                PromptIRBlock(
                    block_id="l2_recent_summaries",
                    title="Recent Summaries",
                    content=recent_summaries,
                )
            )
        primary_input = str(assembly_context.metadata.get("failure_primary_input") or "").strip()
        return PromptIR(
            system_blocks=tuple(self._order_system_blocks(system_blocks)),
            developer_blocks=tuple(self._order_developer_blocks(persona_blocks)),
            user_context_blocks=tuple(user_context_blocks),
            runtime_reminder_blocks=tuple(runtime_blocks),
            primary_input=primary_input,
            turn_kind="failure",
        )

    def _resolve_artifact_images(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Resolve Pal artifact handles before messages cross the LLM IR boundary.

        Artifact identifiers are Pal runtime references, not provider wire data.  L1
        therefore never stores an unresolved ``artifact_image`` part and shape
        codecs remain pure IR-to-wire encoders.
        """

        manager = None
        port_registry = getattr(self.context, "port_registry", None)
        get_port = getattr(port_registry, "get", None)
        if callable(get_port):
            manager = get_port("artifact:artifact")

        resolved_messages: list[dict[str, Any]] = []
        for raw_message in messages:
            message = dict(raw_message)
            content = message.get("content")
            if not isinstance(content, list):
                resolved_messages.append(message)
                continue
            resolved_parts: list[dict[str, Any]] = []
            for raw_part in content:
                if not isinstance(raw_part, dict) or raw_part.get("type") != "artifact_image":
                    if isinstance(raw_part, dict):
                        resolved_parts.append(dict(raw_part))
                    continue
                representation_id = str(raw_part.get("representation_id") or "").strip()
                candidate = str(raw_part.get("source_url") or "").strip()
                source = candidate if candidate.startswith("data:image/") else ""
                if not source and manager is not None:
                    to_data_url = getattr(manager, "to_data_url", None)
                    if callable(to_data_url):
                        source = str(to_data_url(representation_id) or "").strip()
                if not source:
                    raise ValueError(
                        "artifact image could not be resolved before LLM IR construction: "
                        f"representation_id={representation_id or '<missing>'}"
                    )
                resolved_parts.append(
                    {
                        "type": "image",
                        "source": source,
                        "media_type": str(raw_part.get("mime_type") or "").strip() or None,
                    }
                )
            message["content"] = resolved_parts
            resolved_messages.append(message)
        return resolved_messages

    @staticmethod
    def _prompt_target(fragment: PromptFragment) -> str:
        target = str((fragment.metadata or {}).get("prompt_target") or "").strip().lower()
        if not target:
            raise ValueError("prompt fragment must declare metadata.prompt_target")
        if target not in {"system", "developer", "user_context", "runtime_reminder"}:
            raise ValueError(f"unknown prompt fragment target: {target!r}")
        return target

    @staticmethod
    def _validate_prompt_target(
        fragment: PromptFragment,
        *,
        normalized_section: str,
        prompt_target: str,
    ) -> None:
        system_sections = {
            "identity",
            "memory_system",
            "source_of_truth",
            "prompt_context_policy",
            "operating_rules",
            "priority",
            "tool_policy",
            "mutation_policy",
        }
        developer_sections = {
            "persona",
            "system_map",
            "operating_guidance",
            "tool_routing",
            "tool_efficiency",
            "memory_guide",
            "behavior_guidance",
            "behavior_guidance_guide",
            "skill_guide",
            "knowledge_storage_boundary",
            "requirements_brief",
            "requirements_policy",
            "task_acceptance",
            "task_acceptance_policy",
            "output_contract",
        }
        if normalized_section in {"memory", "artifact"} and prompt_target != "user_context":
            raise ValueError(
                f"prompt fragment section {fragment.section!r} must target user_context"
            )
        if normalized_section in {"runtime", "resident_affordances"} and prompt_target != "runtime_reminder":
            raise ValueError(
                f"prompt fragment section {fragment.section!r} must target runtime_reminder"
            )
        if normalized_section == "task_flow" and prompt_target not in {
            "developer",
            "runtime_reminder",
        }:
            raise ValueError("task_flow fragments must target developer or runtime_reminder")
        if (
            prompt_target == "user_context"
            and normalized_section not in {"memory", "artifact"}
        ):
            raise ValueError(
                f"prompt fragment section {fragment.section!r} cannot target user_context"
            )
        if normalized_section in system_sections and prompt_target != "system":
            raise ValueError(
                f"prompt fragment section {fragment.section!r} must target system"
            )
        if normalized_section in developer_sections and prompt_target != "developer":
            raise ValueError(
                f"prompt fragment section {fragment.section!r} must target developer"
            )

    @staticmethod
    def _static_instruction_block(
        fragment: PromptFragment,
        *,
        normalized_section: str,
        rendered_body: str,
    ) -> PromptIRBlock:
        block_ids = {
            "memory_system": "memory_context",
        }
        default_titles = {
            "identity": "Identity",
            "persona": "Persona Defaults",
            "system_map": "System Map",
            "source_of_truth": "Source of Truth",
            "prompt_context_policy": "Prompt Context Policy",
            "operating_rules": "Operating Rules",
            "operating_guidance": "Operating Guidance",
            "priority": "Priority",
            "task_flow": "Task Flow",
            "tool_policy": "Tool Policy",
            "tool_routing": "Tool Routing",
            "tool_efficiency": "Tool Efficiency",
            "mutation_policy": "Mutation Policy",
            "memory_guide": "Memory Guide",
            "behavior_guidance": "Behavior Guidance",
            "behavior_guidance_guide": "Behavior Guidance Guide",
            "skill_guide": "Skill Guide",
            "knowledge_storage_boundary": "Knowledge Storage Boundary",
            "requirements_brief": "Requirements Brief",
            "requirements_policy": "Requirements Policy",
            "task_acceptance": "Task Acceptance",
            "task_acceptance_policy": "Task Acceptance Policy",
            "output_contract": "Output Contract",
            "memory_system": "Memory Context",
        }
        return PromptIRBlock(
            block_id=block_ids.get(normalized_section, normalized_section),
            title=str(fragment.title or default_titles.get(normalized_section) or normalized_section),
            content=rendered_body,
            metadata={
                **dict(fragment.metadata or {}),
                "source_section": fragment.section,
                "source_title": fragment.title,
                "source_priority": fragment.priority,
            },
        )

    @staticmethod
    def _runtime_reminder_block(
        fragment: PromptFragment,
        *,
        normalized_section: str,
        rendered_body: str,
    ) -> PromptIRBlock:
        metadata = {
            **dict(fragment.metadata or {}),
            "source_section": fragment.section,
            "source_title": fragment.title,
        }
        block_id = str(metadata.get("block_id") or normalized_section or fragment.section or "runtime_guidance").strip()
        return PromptIRBlock(
            block_id=block_id,
            title=str(fragment.title or block_id),
            content=rendered_body,
            metadata=metadata,
        )

    def _normalize_prompt_section(self, section: str) -> str:
        lowered = str(section or "").strip().lower()
        aliases = {
            "system_surfaces": "system_map",
            "rules": "operating_rules",
            "advisor_gate": "task_flow",
            "advisor_recovery_memory": "task_flow",
            "behavior_routing": "task_flow",
            "memory_routing": "memory_guide",
            "behavior_memory_write_boundary": "knowledge_storage_boundary",
            "skill_learning": "skill_guide",
        }
        lowered = aliases.get(lowered, lowered)
        if lowered in {
            "identity",
            "persona",
            "memory",
            "memory_system",
            "artifact",
            "runtime",
            "system_map",
            "source_of_truth",
            "prompt_context_policy",
            "operating_rules",
            "operating_guidance",
            "priority",
            "task_flow",
            "tool_policy",
            "tool_routing",
            "tool_efficiency",
            "mutation_policy",
            "memory_guide",
            "behavior_guidance",
            "behavior_guidance_guide",
            "skill_guide",
            "knowledge_storage_boundary",
            "resident_affordances",
            "requirements_brief",
            "requirements_policy",
            "task_acceptance",
            "task_acceptance_policy",
            "output_contract",
        }:
            return lowered
        if lowered in {"control", "observation", "finalization"}:
            return "runtime"
        raise ValueError(f"unknown prompt fragment section: {section!r}")

    @staticmethod
    def _preserve_empty_protocol_fragment(fragment: PromptFragment) -> bool:
        metadata = dict(fragment.metadata or {})
        role = str(metadata.get("role") or "").strip()
        return (
            role == "assistant" and bool(metadata.get("tool_calls"))
        ) or (
            role == "tool" and bool(str(metadata.get("tool_call_id") or "").strip())
        )

    def _build_runtime_overlay_blocks(self, assembly_context: PromptAssemblyContext) -> list[PromptIRBlock]:
        blocks: list[PromptIRBlock] = []
        for block in assembly_context.metadata.get("observation_blocks", []):
            blocks.append(
                PromptIRBlock(
                    block_id="runtime_overlay",
                    title="Runtime Overlay",
                    content=f"Tool Observation:\n{str(block).strip()}",
                )
            )
        finalization_directive = assembly_context.metadata.get("finalization_directive")
        if finalization_directive:
            blocks.append(
                PromptIRBlock(
                    block_id="runtime_overlay",
                    title="Runtime Overlay",
                    content=f"Finalization Directive:\n{str(finalization_directive).strip()}",
                    metadata={"priority": "finalization"},
                )
            )
        return blocks

    def _order_user_context_blocks(self, blocks: list[PromptIRBlock], *, turn_kind: str) -> list[PromptIRBlock]:
        l1_blocks = [block for block in blocks if block.block_id.startswith("l1_recent_context")]
        summary_blocks = [block for block in blocks if block.block_id == "memory_current_summary"]
        working_memory_blocks = [block for block in blocks if block.block_id == "memory_working_memory"]
        trailing_blocks = [
            block
            for block in blocks
            if block.block_id not in {
                *[item.block_id for item in l1_blocks],
                "memory_current_summary",
                "memory_working_memory",
            }
        ]
        if turn_kind == "proactive_trigger":
            return []
        return [*summary_blocks, *l1_blocks, *working_memory_blocks, *trailing_blocks]

    def _order_runtime_reminder_blocks(self, blocks: list[PromptIRBlock]) -> list[PromptIRBlock]:
        order = {
            "task_flow": 10,
            "operating_guidance": 20,
            "memory_guide": 30,
            "memory_guidance": 30,
            "skill_guide": 40,
            "skill_guidance": 40,
            "resident_affordances": 50,
            "behavior_guidance": 60,
            "tool_efficiency": 70,
        }
        return sorted(
            blocks,
            key=lambda block: (
                order.get(block.block_id, 100),
                int(block.metadata.get("source_priority", 1000) or 1000),
                str(block.title),
            ),
        )

    def _order_system_blocks(self, blocks: list[PromptIRBlock]) -> list[PromptIRBlock]:
        identity_blocks = [block for block in blocks if block.block_id == "identity"]
        source_of_truth_blocks = [block for block in blocks if block.block_id == "source_of_truth"]
        prompt_context_policy_blocks = [block for block in blocks if block.block_id == "prompt_context_policy"]
        rule_blocks = [block for block in blocks if block.block_id == "operating_rules"]
        priority_blocks = [block for block in blocks if block.block_id == "priority"]
        tool_policy_blocks = [block for block in blocks if block.block_id == "tool_policy"]
        mutation_policy_blocks = [block for block in blocks if block.block_id == "mutation_policy"]
        memory_blocks = [block for block in blocks if block.block_id == "memory_context"]
        return [
            *identity_blocks,
            *source_of_truth_blocks,
            *prompt_context_policy_blocks,
            *rule_blocks,
            *priority_blocks,
            *tool_policy_blocks,
            *mutation_policy_blocks,
            *memory_blocks,
        ]

    @staticmethod
    def _order_developer_blocks(blocks: list[PromptIRBlock]) -> list[PromptIRBlock]:
        order = {
            "persona": 10,
            "system_map": 20,
            "operating_guidance": 30,
            "task_flow": 40,
            "tool_routing": 50,
            "tool_efficiency": 60,
            "memory_guide": 70,
            "behavior_guidance": 80,
            "behavior_guidance_guide": 90,
            "skill_guide": 100,
            "knowledge_storage_boundary": 110,
            "requirements_brief": 120,
            "requirements_policy": 130,
            "task_acceptance": 140,
            "task_acceptance_policy": 150,
            "output_contract": 160,
        }
        return sorted(
            blocks,
            key=lambda block: (
                order.get(block.block_id, 1000),
                int(block.metadata.get("source_priority", 1000) or 1000),
                str(block.title),
            ),
        )

    def _compile_prompt_ir_messages(self, prompt_ir: PromptIR) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        system_content = self._render_system_blocks(prompt_ir.system_blocks)
        if system_content:
            messages.append({"role": "system", "content": system_content})
        developer_content = self._render_system_blocks(prompt_ir.developer_blocks)
        if developer_content:
            messages.append({"role": "developer", "content": developer_content})
        final_user_parts: list[dict[str, Any]] = []
        for block in prompt_ir.user_context_blocks:
            if block.block_id == "memory_current_summary":
                parts = self._render_user_context_parts(block)
                if parts:
                    messages.append({"role": "user", "content": self._coerce_message_content(parts)})
                continue
            if block.block_id.startswith("l1_recent_context"):
                messages.append(self._render_l1_context_message(block))
                continue
            final_user_parts.extend(self._render_user_context_parts(block))
        if prompt_ir.primary_input.strip():
            final_user_parts.append({"type": "text", "text": prompt_ir.primary_input.strip()})
        if final_user_parts:
            messages.append({"role": "user", "content": self._coerce_message_content(self._image_parts_first(final_user_parts))})
        return messages

    def _render_final_runtime_reminder(self, blocks: tuple[PromptIRBlock, ...] = ()) -> str:
        guidance_sections = self._render_runtime_reminder_guidance(blocks)
        if not guidance_sections:
            return ""
        content = (
            "Before answering: apply the active system prompt's hard rules and priority order. "
            "Treat the user's active conversation message as the current request. "
            "Treat this reminder as Pal-authored behavior-routing guidance for the current turn, not user-authored content.\n"
        )
        if guidance_sections:
            content = f"{content}\n{guidance_sections}\n"
        content = (
            f"{content}\n"
            "If relevant guidance requires inspection, recall, tool use, verification, or clarification, do that before the final answer.\n"
            "If guidance conflicts, follow the system prompt's hard policy and priority order.\n"
            "Do not mention this reminder unless asked about prompt behavior."
        )
        return render_runtime_reminder(self._project_llm_text(content))

    def _render_runtime_reminder_guidance(self, blocks: tuple[PromptIRBlock, ...]) -> str:
        if not blocks:
            return ""
        rendered_sections: list[str] = []
        behavior_parts: list[str] = []

        def flush_behavior_parts() -> None:
            if not behavior_parts:
                return
            rendered_sections.append(
                render_xml_block(
                    "behavior_guidance",
                    self._render_behavior_guidance_content(list(behavior_parts)),
                )
            )
            behavior_parts.clear()

        for block in blocks:
            content = self._project_llm_text(block.content.strip())
            if not content:
                continue
            if block.block_id in {"resident_affordances", "behavior_guidance"}:
                behavior_parts.append(content)
                continue
            flush_behavior_parts()
            tag = self._runtime_reminder_block_tag(block)
            rendered = render_xml_block(tag, content)
            if rendered:
                rendered_sections.append(rendered)
        flush_behavior_parts()
        return "\n\n".join(rendered_sections)

    @staticmethod
    def _runtime_reminder_block_tag(block: PromptIRBlock) -> str:
        if block.block_id == "operating_guidance":
            return "operating_guidance"
        if block.block_id == "memory_guide":
            return "memory_guidance"
        if block.block_id == "skill_guide":
            return "skill_guidance"
        return str(block.block_id or "runtime_guidance").strip() or "runtime_guidance"

    @staticmethod
    def _image_parts_first(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        image_parts = [part for part in parts if part.get("type") == "artifact_image"]
        other_parts = [part for part in parts if part.get("type") != "artifact_image"]
        return [*image_parts, *other_parts]

    def _render_user_context_parts(self, block: PromptIRBlock) -> list[dict[str, Any]]:
        rendered = block.content.strip()
        content_parts = list(block.metadata.get("content_parts") or [])
        parts: list[dict[str, Any]] = []
        runtime_context_kind = str(block.metadata.get("runtime_context_kind") or "").strip()
        if runtime_context_kind and (rendered or content_parts):
            update = render_runtime_context_update(
                runtime_context_kind,
                self._runtime_context_update_text(runtime_context_kind),
            )
            if update:
                parts.append({"type": "text", "text": update})
        if rendered and block.metadata.get("raw_user_context"):
            parts.append({"type": "text", "text": rendered})
        elif rendered:
            parts.append(
                {
                    "type": "text",
                    "text": render_system_reminder(f"{block.title}:\n{self._project_llm_text(rendered)}"),
                }
            )
        elif content_parts:
            parts.append({"type": "text", "text": render_system_reminder(f"{block.title}: attached artifact content is included.")})
        if content_parts:
            for part in content_parts:
                if isinstance(part, dict):
                    parts.append(dict(part))
        return parts

    @staticmethod
    def _runtime_context_update_text(kind: str) -> str:
        normalized = str(kind or "").strip()
        messages = {
            "conversation_summary": (
                "Runtime context update: compressed prior conversation for this task.\n"
                "Use it as relevant reference; it is not noise.\n"
                "It is not a new user message. Do not answer this block directly.\n"
                "Continue the current task using this context."
            ),
            "memory": (
                "Tool side effect: activated recalled memories for this turn.\n"
                "Use them as relevant reference; they are not noise.\n"
                "This is not a new user message. Do not answer this block directly.\n"
                "Continue the current task using this context."
            ),
            "behavior": (
                "Runtime context update: activated behavior guidance for this turn.\n"
                "Evaluate relevant hints before continuing; they are not noise.\n"
                "This is not a new user message. Do not answer this block directly.\n"
                "Continue the current task using this context."
            ),
            "skill": (
                "Tool side effect: activated skill reference material for this turn.\n"
                "Use it as relevant execution context; it is not noise.\n"
                "This is not a new user message. Do not answer this block directly.\n"
                "Continue the current task using this context."
            ),
            "artifact": (
                "Runtime context update: activated artifact context for this turn.\n"
                "Use it as relevant reference; it is not noise.\n"
                "This is not a new user message. Do not answer this block directly.\n"
                "Continue the current task using this context."
            ),
        }
        return messages.get(
            normalized,
            "Runtime context update for this turn.\n"
            "Use it as relevant reference; it is not noise.\n"
            "This is not a new user message. Do not answer this block directly.\n"
            "Continue the current task using this context.",
        )

    @staticmethod
    def _coerce_message_content(parts: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
        if len(parts) == 1 and parts[0].get("type") == "text":
            return str(parts[0].get("text") or "")
        return parts

    def _render_l1_context_message(self, block: PromptIRBlock) -> dict[str, Any]:
        rendered = block.content.strip()
        role = str(block.metadata.get("role") or "user")
        tool_calls = block.metadata.get("tool_calls")
        tool_call_id = block.metadata.get("tool_call_id")
        if role == "tool":
            msg: dict[str, Any] = {"role": "tool", "content": rendered}
            if tool_call_id:
                msg["tool_call_id"] = tool_call_id
            return msg
        if role == "assistant" and tool_calls:
            msg = {"role": "assistant", "content": rendered, "tool_calls": self._alias_tool_calls_for_llm(tool_calls)}
            return msg
        return {"role": role, "content": rendered}

    @staticmethod
    def _alias_tool_calls_for_llm(tool_calls: object) -> object:
        if not isinstance(tool_calls, list):
            return tool_calls
        rendered: list[dict[str, Any]] = []
        for item in tool_calls:
            if not isinstance(item, dict):
                continue
            payload = dict(item)
            function = payload.get("function")
            if isinstance(function, dict):
                rendered_function = dict(function)
                payload["function"] = rendered_function
            rendered.append(payload)
        return rendered

    def _render_system_blocks(self, blocks: tuple[PromptIRBlock, ...]) -> str:
        if not blocks:
            return ""
        rendered_sections: list[str] = []
        current_tag: str | None = None
        current_parts: list[str] = []
        for block in blocks:
            tag = self._system_block_tag(block)
            if current_tag != tag:
                if current_tag is not None and current_parts:
                    rendered_sections.append(self._render_system_section(current_tag, current_parts))
                current_tag = tag
                current_parts = []
            current_parts.append(block.content.strip())
        if current_tag is not None and current_parts:
            rendered_sections.append(self._render_system_section(current_tag, current_parts))
        return "\n\n".join(rendered_sections)

    def _render_system_section(self, tag: str, parts: list[str]) -> str:
        if tag == "behavior_guidance":
            return render_xml_block(
                tag,
                self._render_behavior_guidance_content(
                    [self._project_llm_text(part) for part in parts]
                ),
            )
        return render_xml_block(tag, self._project_llm_text("\n\n".join(parts)))

    @staticmethod
    def _render_behavior_guidance_content(parts: list[str]) -> str:
        lines: list[str] = []
        seen_lines: set[str] = set()
        for part in parts:
            for raw_line in part.splitlines():
                line = raw_line.strip()
                if not line or line in _BEHAVIOR_GUIDANCE_HEADER_LINES:
                    continue
                dedupe_key = line.casefold()
                if dedupe_key in seen_lines:
                    continue
                seen_lines.add(dedupe_key)
                lines.append(line)
        if not lines:
            return _BEHAVIOR_GUIDANCE_HEADER
        return _BEHAVIOR_GUIDANCE_HEADER + "\n\n" + "\n".join(lines)

    @staticmethod
    def _system_block_tag(block: PromptIRBlock) -> str:
        if block.block_id == "resident_affordances":
            return "behavior_guidance"
        tag = str(block.block_id or "").strip()
        if tag == "memory_context":
            return "memory_context"
        return tag or "system_context"

    def _prompt_ir_debug_dict(self, prompt_ir: PromptIR) -> dict[str, Any]:
        return {
            "turn_kind": prompt_ir.turn_kind,
            "system_blocks": [
                {"block_id": block.block_id, "title": block.title, "content": block.content}
                for block in prompt_ir.system_blocks
            ],
            "developer_blocks": [
                {"block_id": block.block_id, "title": block.title, "content": block.content}
                for block in prompt_ir.developer_blocks
            ],
            "user_context_blocks": [
                {"block_id": block.block_id, "title": block.title, "content": block.content}
                for block in prompt_ir.user_context_blocks
            ],
            "runtime_reminder_blocks": [
                {"block_id": block.block_id, "title": block.title, "content": block.content}
                for block in prompt_ir.runtime_reminder_blocks
            ],
            "primary_input": prompt_ir.primary_input,
        }

    def _extract_primary_input_text(self, assembly_context: PromptAssemblyContext) -> str:
        if assembly_context.metadata.get("active_l1_owns_primary_input"):
            return ""
        if assembly_context.turn_kind == "proactive_trigger":
            proactive_input = assembly_context.metadata.get("proactive_input")
            if isinstance(proactive_input, str) and proactive_input.strip():
                return proactive_input.strip()
        return self._extract_user_message_text(assembly_context.event)

    def _extract_user_message_text(self, event) -> str:
        return extract_text_from_payload(getattr(event, "payload", None))

    def _ensure_memory_pack(self, assembly_context: PromptAssemblyContext) -> PromptAssemblyContext:
        if assembly_context.turn_kind in {"failure", "proactive_trigger"}:
            return assembly_context
        if "memory_pack" in assembly_context.metadata:
            return assembly_context
        try:
            from pal.memory import MemoryPackRequest

            memory_service = self.context.require_port("memory:memory")
            pack = memory_service.build_pack(
                MemoryPackRequest(
                    turn_kind=assembly_context.turn_kind,
                    task_id=assembly_context.task_id,
                    work_order_id=assembly_context.work_order_id,
                    active_input_id=str(
                        getattr(
                            getattr(assembly_context, "event", None),
                            "event_id",
                            "",
                        )
                        or ""
                    )
                    or None,
                )
            )
        except Exception:
            return assembly_context
        metadata = dict(assembly_context.metadata)
        metadata["memory_pack"] = pack
        return PromptAssemblyContext(
            event=assembly_context.event,
            core_mode=assembly_context.core_mode,
            turn_kind=assembly_context.turn_kind,
            task_id=assembly_context.task_id,
            work_order_id=assembly_context.work_order_id,
            metadata=metadata,
        )
