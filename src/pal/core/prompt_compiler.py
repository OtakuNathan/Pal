from __future__ import annotations

from typing import Any

from pal.shared import PromptAssemblyContext, PromptFragment, PromptIR, PromptIRBlock
from pal.shared.payloads import extract_text_from_payload


class PromptCompiler:
    def __init__(self, context) -> None:
        self.context = context

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
        user_context_blocks: list[PromptIRBlock] = []

        for fragment in fragments:
            normalized_section = self._normalize_prompt_section(fragment.section)
            rendered_body = str(fragment.content).strip()
            if not rendered_body:
                continue
            if normalized_section == "identity":
                system_blocks.append(
                    PromptIRBlock(
                        block_id="identity",
                        title="Identity",
                        content=rendered_body,
                        metadata={"source_section": fragment.section, "source_title": fragment.title},
                    )
                )
            elif normalized_section == "rules":
                system_blocks.append(
                    PromptIRBlock(
                        block_id="operating_rules",
                        title="Operating Rules",
                        content=rendered_body,
                        metadata={"source_section": fragment.section, "source_title": fragment.title},
                    )
                )
            elif normalized_section == "behavior_routing":
                system_blocks.append(
                    PromptIRBlock(
                        block_id="behavior_routing",
                        title="Behavior Routing",
                        content=rendered_body,
                        metadata={"source_section": fragment.section, "source_title": fragment.title},
                    )
                )
            elif normalized_section == "memory_routing":
                system_blocks.append(
                    PromptIRBlock(
                        block_id="memory_routing",
                        title="Memory Routing",
                        content=rendered_body,
                        metadata={"source_section": fragment.section, "source_title": fragment.title},
                    )
                )
            elif normalized_section == "resident_affordances":
                system_blocks.append(
                    PromptIRBlock(
                        block_id="resident_affordances",
                        title="Resident Affordances",
                        content=rendered_body,
                        metadata={"source_section": fragment.section, "source_title": fragment.title},
                    )
                )
            elif normalized_section == "memory":
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
            elif normalized_section == "memory_system":
                system_blocks.append(
                    PromptIRBlock(
                        block_id="memory_context",
                        title=str(fragment.title or "Memory Context"),
                        content=rendered_body,
                        metadata={
                            **dict(fragment.metadata),
                            "source_section": fragment.section,
                            "source_title": fragment.title,
                        },
                    )
                )
            elif normalized_section == "runtime":
                system_blocks.append(
                    PromptIRBlock(
                        block_id="runtime_overlay",
                        title="Runtime Overlay",
                        content=f"### {fragment.title}\n{rendered_body}",
                        metadata={"source_section": fragment.section, "source_title": fragment.title},
                    )
                )

        system_blocks.extend(self._build_runtime_overlay_blocks(assembly_context))
        if assembly_context.turn_kind == "service_trigger":
            system_blocks.append(
                PromptIRBlock(
                    block_id="runtime_overlay",
                    title="Runtime Overlay",
                    content=(
                        "### Task Directive\n"
                        "This is a scheduled service trigger. Execute the described task now and output the result directly.\n"
                        "Do not create, configure, or describe services. Perform the action."
                    ),
                )
            )

        ordered_system_blocks = self._order_system_blocks(system_blocks)
        ordered_user_blocks = self._order_user_context_blocks(user_context_blocks, turn_kind=assembly_context.turn_kind)
        primary_input = self._extract_primary_input_text(assembly_context)
        return PromptIR(
            system_blocks=tuple(ordered_system_blocks),
            user_context_blocks=tuple(ordered_user_blocks),
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
        from pal.llm.contracts import CanonicalLLMRequest

        prompt_ir = self.build_prompt_ir(assembly_context)
        messages = self._compile_prompt_ir_messages(prompt_ir)
        return CanonicalLLMRequest(
            messages=messages,
            max_output_tokens=max_output_tokens,
            model_hint=model_hint,
            temperature=None,
            metadata={
                "fragment_sections": [block.block_id for block in prompt_ir.system_blocks],
                "user_context_blocks": [block.block_id for block in prompt_ir.user_context_blocks],
                "prompt_ir": self._prompt_ir_debug_dict(prompt_ir),
                **{
                    key: assembly_context.metadata[key]
                    for key in ("preferred_endpoint_id", "preferred_model_id")
                    if key in assembly_context.metadata
                },
            },
        )

    def _build_failure_prompt_ir(self, assembly_context: PromptAssemblyContext) -> PromptIR:
        identity_blocks: list[PromptIRBlock] = []
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
            if self._normalize_prompt_section(fragment.section) != "identity":
                continue
            rendered_body = str(fragment.content).strip()
            if not rendered_body:
                continue
            identity_blocks.append(
                PromptIRBlock(
                    block_id="identity",
                    title="Identity",
                    content=rendered_body,
                    metadata={"source_section": fragment.section, "source_title": fragment.title},
                )
            )
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
            *self._build_runtime_overlay_blocks(assembly_context),
        ]
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
            user_context_blocks=tuple(user_context_blocks),
            primary_input=primary_input,
            turn_kind="failure",
        )

    def _normalize_prompt_section(self, section: str) -> str:
        lowered = str(section or "").strip().lower()
        if lowered in {
            "identity",
            "memory",
            "memory_system",
            "runtime",
            "rules",
            "behavior_routing",
            "memory_routing",
            "resident_affordances",
        }:
            return lowered
        if lowered in {"control", "observation", "finalization"}:
            return "runtime"
        return lowered

    def _build_runtime_overlay_blocks(self, assembly_context: PromptAssemblyContext) -> list[PromptIRBlock]:
        blocks: list[PromptIRBlock] = []
        for block in assembly_context.metadata.get("observation_blocks", []):
            blocks.append(
                PromptIRBlock(
                    block_id="runtime_overlay",
                    title="Runtime Overlay",
                    content=f"### Tool Observation\n{str(block).strip()}",
                )
            )
        compact_note = assembly_context.metadata.get("compact_note")
        if compact_note:
            blocks.append(
                PromptIRBlock(
                    block_id="runtime_overlay",
                    title="Runtime Overlay",
                    content=f"### Compaction Note\n{str(compact_note).strip()}",
                )
            )
        finalization_directive = assembly_context.metadata.get("finalization_directive")
        if finalization_directive:
            blocks.append(
                PromptIRBlock(
                    block_id="runtime_overlay",
                    title="Runtime Overlay",
                    content=f"### Finalization Directive\n{str(finalization_directive).strip()}",
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
        if turn_kind == "service_trigger":
            return []
        return [*l1_blocks, *summary_blocks, *working_memory_blocks, *trailing_blocks]

    def _order_system_blocks(self, blocks: list[PromptIRBlock]) -> list[PromptIRBlock]:
        identity_blocks = [block for block in blocks if block.block_id == "identity"]
        rule_blocks = [block for block in blocks if block.block_id == "operating_rules"]
        behavior_blocks = [block for block in blocks if block.block_id == "behavior_routing"]
        memory_routing_blocks = [block for block in blocks if block.block_id == "memory_routing"]
        resident_blocks = [block for block in blocks if block.block_id == "resident_affordances"]
        memory_blocks = [block for block in blocks if block.block_id == "memory_context"]
        runtime_blocks = [block for block in blocks if block.block_id == "runtime_overlay"]
        ordered_runtime = [block for block in runtime_blocks if block.metadata.get("priority") != "finalization"]
        ordered_runtime.extend(block for block in runtime_blocks if block.metadata.get("priority") == "finalization")
        return [
            *identity_blocks,
            *rule_blocks,
            *behavior_blocks,
            *memory_routing_blocks,
            *resident_blocks,
            *memory_blocks,
            *ordered_runtime,
        ]

    def _compile_prompt_ir_messages(self, prompt_ir: PromptIR) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        system_content = self._render_system_blocks(prompt_ir.system_blocks)
        if system_content:
            messages.append({"role": "system", "content": system_content})
        for block in prompt_ir.user_context_blocks:
            messages.append(self._render_user_context_message(block))
        if prompt_ir.primary_input.strip():
            messages.append({"role": "user", "content": prompt_ir.primary_input.strip()})
        return messages

    def _render_user_context_message(self, block: PromptIRBlock) -> dict[str, Any]:
        rendered = block.content.strip()
        if block.block_id.startswith("l1_recent_context"):
            role = str(block.metadata.get("role") or "user")
            tool_calls = block.metadata.get("tool_calls")
            tool_call_id = block.metadata.get("tool_call_id")
            if role == "tool":
                msg: dict[str, Any] = {"role": "tool", "content": rendered}
                if tool_call_id:
                    msg["tool_call_id"] = tool_call_id
                return msg
            if role == "assistant" and tool_calls:
                msg = {"role": "assistant", "content": rendered, "tool_calls": tool_calls}
                return msg
            return {"role": role, "content": rendered}
        return {"role": "user", "content": f"<system-reminder>{block.title}:\n{rendered}</system-reminder>"}

    def _render_system_blocks(self, blocks: tuple[PromptIRBlock, ...]) -> str:
        if not blocks:
            return ""
        rendered_sections: list[str] = []
        current_title: str | None = None
        current_parts: list[str] = []
        for block in blocks:
            if current_title != block.title:
                if current_title is not None and current_parts:
                    rendered_sections.append(f"## {current_title}\n" + "\n\n".join(current_parts))
                current_title = block.title
                current_parts = []
            current_parts.append(block.content.strip())
        if current_title is not None and current_parts:
            rendered_sections.append(f"## {current_title}\n" + "\n\n".join(current_parts))
        return "\n\n".join(rendered_sections)

    def _prompt_ir_debug_dict(self, prompt_ir: PromptIR) -> dict[str, Any]:
        return {
            "turn_kind": prompt_ir.turn_kind,
            "system_blocks": [
                {"block_id": block.block_id, "title": block.title, "content": block.content}
                for block in prompt_ir.system_blocks
            ],
            "user_context_blocks": [
                {"block_id": block.block_id, "title": block.title, "content": block.content}
                for block in prompt_ir.user_context_blocks
            ],
            "primary_input": prompt_ir.primary_input,
        }

    def _extract_primary_input_text(self, assembly_context: PromptAssemblyContext) -> str:
        if assembly_context.turn_kind == "service_trigger":
            service_input = assembly_context.metadata.get("service_input")
            if isinstance(service_input, str) and service_input.strip():
                return service_input.strip()
            service_definition = assembly_context.metadata.get("service_definition")
            if service_definition is not None:
                try:
                    from pal.service import ServiceDefinition, build_service_trigger_input

                    if isinstance(service_definition, ServiceDefinition):
                        return build_service_trigger_input(service_definition).strip()
                except Exception:
                    pass
        return self._extract_user_message_text(assembly_context.event)

    def _extract_user_message_text(self, event) -> str:
        return extract_text_from_payload(getattr(event, "payload", None))

    def _ensure_memory_pack(self, assembly_context: PromptAssemblyContext) -> PromptAssemblyContext:
        if assembly_context.turn_kind in {"failure", "service_trigger"}:
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
