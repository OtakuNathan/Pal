from __future__ import annotations

import ast
import unittest
from dataclasses import fields
from pathlib import Path

from pal.core import PalCore as _PalCoreBootstrap
from pal.execution.contracts import CapabilityDescriptor
from pal.execution.runtime import ExecutionRuntime
from pal.execution.tool_facade import (
    EffectKind,
    EmptyToolInput,
    EmptyToolOutput,
    Idempotency,
    InvocationMode,
    NextToolHint,
    PagingMode,
    RetryPolicy,
    ToolExecutionSemantics,
    ToolGuidance,
)
from pal.shared.capability_forest import CapabilityActionBlueprint
from tests.capability_fixture import mount_test_capability


def _guidance(*, purpose: str, hints: tuple[NextToolHint, ...] = ()) -> ToolGuidance:
    return ToolGuidance(
        purpose=purpose,
        use_when="the focused guidance test needs this capability",
        do_not_use_when="negative-only-token must not enter tool search",
        failure_next_steps="failure-only-token must not enter tool search",
        next_tool_hints=hints,
    )


def _execution(mode: InvocationMode) -> ToolExecutionSemantics:
    return ToolExecutionSemantics(
        invocation_mode=mode,
        effect_kind=EffectKind.NONE,
        idempotency=Idempotency.IDEMPOTENT,
        retry_policy=RetryPolicy.AUTOMATIC,
        paging=PagingMode.NEVER,
    )


def _mount(
    runtime: ExecutionRuntime,
    alias: str,
    *,
    mode: InvocationMode,
    guidance: ToolGuidance | None = None,
    source: str = "test",
    metadata: dict[str, object] | None = None,
) -> None:
    mount_test_capability(
        runtime,
        alias=alias,
        canonical_path=f"op_test_{alias}",
        InputModel=EmptyToolInput,
        OutputModel=EmptyToolOutput,
        handler=lambda _value: {},
        guidance=guidance or _guidance(purpose=f"Use {alias}."),
        execution=_execution(mode),
        source=source,
        metadata=dict(metadata or {}),
    )


class ToolGuidanceCompilationTests(unittest.TestCase):
    def test_descriptions_and_search_documents_are_derived_from_guidance(self) -> None:
        runtime = ExecutionRuntime()
        try:
            _mount(runtime, "direct_next", mode=InvocationMode.DIRECT)
            _mount(runtime, "indirect_next", mode=InvocationMode.INDIRECT)
            _mount(
                runtime,
                "starter",
                mode=InvocationMode.DIRECT,
                guidance=_guidance(
                    purpose="starter-purpose-token",
                    hints=(
                        NextToolHint(name="direct_next", use_when="continue directly"),
                        NextToolHint(name="indirect_next", use_when="continue indirectly"),
                    ),
                ),
            )

            record = runtime.registry_generation.direct_aliases["starter"]
            self.assertIn("Invoke it directly as `direct_next`", record.compiled_description)
            self.assertIn('read_tool(name="indirect_next")', record.compiled_description)
            self.assertIn('call_tool(name="indirect_next", args=...)', record.compiled_description)
            self.assertIn("starter-purpose-token", record.search_document)
            self.assertIn("direct_next", record.search_document)
            self.assertNotIn("negative-only-token", record.search_document)
            self.assertNotIn("failure-only-token", record.search_document)
        finally:
            runtime.shutdown()

    def test_unknown_first_party_hint_is_rejected_but_scoped_projection_can_degrade(self) -> None:
        runtime = ExecutionRuntime()
        try:
            guidance = _guidance(
                purpose="A first-party starter.",
                hints=(NextToolHint(name="missing_next", use_when="continuation is needed"),),
            )
            with self.assertRaisesRegex(ValueError, "unknown next-tool alias 'missing_next'"):
                _mount(
                    runtime,
                    "strict_starter",
                    mode=InvocationMode.DIRECT,
                    guidance=guidance,
                    source="builtin:test",
                )

            _mount(
                runtime,
                "scoped_starter",
                mode=InvocationMode.DIRECT,
                guidance=guidance,
                source="builtin:test",
                metadata={"allow_missing_next_tool_hints": True},
            )
            description = runtime.registry_generation.direct_aliases[
                "scoped_starter"
            ].compiled_description
            self.assertIn("not available in the current tool surface", description)
        finally:
            runtime.shutdown()

    def test_authoring_contract_has_no_parallel_description_or_search_text(self) -> None:
        for model in (CapabilityDescriptor, CapabilityActionBlueprint):
            field_names = {field.name for field in fields(model)}
            self.assertNotIn("description", field_names)
            self.assertNotIn("search_text", field_names)
            self.assertIn("guidance", field_names)

    def test_every_literal_first_party_next_tool_name_is_a_declared_alias(self) -> None:
        source_root = Path(__file__).parents[1] / "src" / "pal"
        aliases: set[str] = set()
        hints: list[tuple[Path, int, str]] = []
        for path in source_root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function_name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else ""
                )
                keywords = {item.arg: item.value for item in node.keywords}
                if function_name == "capability_action":
                    self.assertIn("guidance", keywords, f"{path}:{node.lineno}")
                    self.assertNotIn("description", keywords, f"{path}:{node.lineno}")
                    self.assertNotIn("search_text", keywords, f"{path}:{node.lineno}")
                    declared = keywords.get("aliases")
                    if isinstance(declared, ast.Tuple | ast.List):
                        aliases.update(
                            item.value
                            for item in declared.elts
                            if isinstance(item, ast.Constant) and isinstance(item.value, str)
                        )
                elif function_name == "NextToolHint":
                    name = keywords.get("name")
                    if isinstance(name, ast.Constant) and isinstance(name.value, str):
                        hints.append((path, node.lineno, name.value))

        missing = [f"{path}:{line}: {name}" for path, line, name in hints if name not in aliases]
        self.assertFalse(missing, "unknown first-party next-tool aliases:\n" + "\n".join(missing))


if __name__ == "__main__":
    unittest.main()
