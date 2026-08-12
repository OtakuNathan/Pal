from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any, Callable

from pydantic import BaseModel

from pal.execution.contracts import CapabilityCall, CapabilityDescriptor
from pal.execution.tool_facade import (
    EffectKind,
    Idempotency,
    InvocationMode,
    PagingMode,
    RetryPolicy,
    ToolExecutionSemantics,
    ToolGuidance,
)
from pal.shared import BoundCapabilityAction, MountedSubtreeHandle, SINGLETON_TARGET


def build_test_capability_handle(
    *,
    alias: str,
    canonical_path: str,
    InputModel: type[BaseModel],
    OutputModel: type[BaseModel],
    handler: Callable[[BaseModel], Any],
    guidance: ToolGuidance | None = None,
    execution: ToolExecutionSemantics | None = None,
    examples: tuple[dict[str, Any], ...] = (),
    module_id: str = "test",
    family: str = "test",
    source: str = "test",
    target_id: str = SINGLETON_TARGET,
    metadata: dict[str, Any] | None = None,
) -> SimpleNamespace:
    resolved_guidance = guidance or ToolGuidance(
        purpose=f"Test capability {alias}",
        use_when="running the focused test that mounted this capability",
        do_not_use_when="outside its focused test",
        failure_next_steps="inspect the focused test failure",
    )
    resolved_execution = execution or ToolExecutionSemantics(
        invocation_mode=InvocationMode.INDIRECT,
        effect_kind=EffectKind.NONE,
        idempotency=Idempotency.IDEMPOTENT,
        retry_policy=RetryPolicy.AUTOMATIC,
        paging=PagingMode.NEVER,
    )
    descriptor = CapabilityDescriptor(
        name=alias,
        aliases=(alias,),
        canonical_path=canonical_path,
        family=family,
        source=source,
        target_id=target_id,
        InputModel=InputModel,
        OutputModel=OutputModel,
        guidance=resolved_guidance,
        execution=resolved_execution,
        examples=tuple(dict(item) for item in examples),
        metadata=dict(metadata or {}),
        module_id=module_id,
    )

    def invoke(call: CapabilityCall) -> Any:
        value = InputModel.model_validate(call.args, strict=True)
        return handler(value)

    async def invoke_async(call: CapabilityCall) -> Any:
        value = InputModel.model_validate(call.args, strict=True)
        result = handler(value)
        return await result if inspect.isawaitable(result) else result

    action = BoundCapabilityAction(
        canonical_path=canonical_path,
        target_id=target_id,
        descriptor=descriptor,
        callable=invoke,
        async_callable=invoke_async if inspect.iscoroutinefunction(handler) else None,
    )
    subtree = MountedSubtreeHandle(module_id=f"test::{canonical_path}::{target_id}")
    subtree.descriptors.append(descriptor)
    subtree.bound_actions.append(action)
    subtree.bound_action_keys.append((canonical_path, target_id))
    subtree.search_record_ids.append(alias)
    handle = SimpleNamespace(mounted_subtree=subtree)
    return handle


def mount_test_capability(runtime: Any, **kwargs: Any) -> SimpleNamespace:
    handle = build_test_capability_handle(**kwargs)
    runtime.mount_subtree(handle)
    return handle


def unmount_test_capability(runtime: Any, handle: SimpleNamespace) -> None:
    runtime.unmount_subtree(handle)


__all__ = [
    "build_test_capability_handle",
    "mount_test_capability",
    "unmount_test_capability",
]
