from __future__ import annotations

from pal.execution.tool_facade import (
    EffectKind,
    Idempotency,
    InvocationMode,
    PagingMode,
    RetryPolicy,
    ToolExecutionSemantics,
)


def execution_semantics(
    *,
    invocation_mode: InvocationMode,
    effect_kind: EffectKind,
    idempotency: Idempotency | None = None,
    retry_policy: RetryPolicy | None = None,
    paging: PagingMode = PagingMode.SUPPORTED,
) -> ToolExecutionSemantics:
    non_idempotent = effect_kind in {EffectKind.EXTERNAL_WRITE, EffectKind.CONTROL}
    resolved_idempotency = idempotency or (
        Idempotency.NON_IDEMPOTENT if non_idempotent else Idempotency.IDEMPOTENT
    )
    resolved_retry = retry_policy or (
        RetryPolicy.RECONCILE_FIRST
        if resolved_idempotency is Idempotency.NON_IDEMPOTENT
        else RetryPolicy.AUTOMATIC
    )
    return ToolExecutionSemantics(
        invocation_mode=invocation_mode,
        effect_kind=effect_kind,
        idempotency=resolved_idempotency,
        retry_policy=resolved_retry,
        paging=paging,
    )


DIRECT_NONE = execution_semantics(invocation_mode=InvocationMode.DIRECT, effect_kind=EffectKind.NONE)
DIRECT_LOCAL_READ = execution_semantics(
    invocation_mode=InvocationMode.DIRECT,
    effect_kind=EffectKind.LOCAL_READ,
)
DIRECT_LOCAL_WRITE = execution_semantics(
    invocation_mode=InvocationMode.DIRECT,
    effect_kind=EffectKind.LOCAL_WRITE,
)
DIRECT_UNSAFE_LOCAL_WRITE = execution_semantics(
    invocation_mode=InvocationMode.DIRECT,
    effect_kind=EffectKind.LOCAL_WRITE,
    idempotency=Idempotency.NON_IDEMPOTENT,
    retry_policy=RetryPolicy.RECONCILE_FIRST,
)
DIRECT_EXTERNAL_READ = execution_semantics(
    invocation_mode=InvocationMode.DIRECT,
    effect_kind=EffectKind.EXTERNAL_READ,
)
DIRECT_EXTERNAL_WRITE = execution_semantics(
    invocation_mode=InvocationMode.DIRECT,
    effect_kind=EffectKind.EXTERNAL_WRITE,
)
DIRECT_CONTROL = execution_semantics(
    invocation_mode=InvocationMode.DIRECT,
    effect_kind=EffectKind.CONTROL,
)

INDIRECT_NONE = execution_semantics(invocation_mode=InvocationMode.INDIRECT, effect_kind=EffectKind.NONE)
INDIRECT_LOCAL_READ = execution_semantics(
    invocation_mode=InvocationMode.INDIRECT,
    effect_kind=EffectKind.LOCAL_READ,
)
INDIRECT_LOCAL_READ_UNPAGED = execution_semantics(
    invocation_mode=InvocationMode.INDIRECT,
    effect_kind=EffectKind.LOCAL_READ,
    paging=PagingMode.NEVER,
)
INDIRECT_LOCAL_WRITE = execution_semantics(
    invocation_mode=InvocationMode.INDIRECT,
    effect_kind=EffectKind.LOCAL_WRITE,
)
INDIRECT_UNSAFE_LOCAL_WRITE = execution_semantics(
    invocation_mode=InvocationMode.INDIRECT,
    effect_kind=EffectKind.LOCAL_WRITE,
    idempotency=Idempotency.NON_IDEMPOTENT,
    retry_policy=RetryPolicy.RECONCILE_FIRST,
)
INDIRECT_EXTERNAL_READ = execution_semantics(
    invocation_mode=InvocationMode.INDIRECT,
    effect_kind=EffectKind.EXTERNAL_READ,
)
INDIRECT_EXTERNAL_WRITE = execution_semantics(
    invocation_mode=InvocationMode.INDIRECT,
    effect_kind=EffectKind.EXTERNAL_WRITE,
)
INDIRECT_CONTROL = execution_semantics(
    invocation_mode=InvocationMode.INDIRECT,
    effect_kind=EffectKind.CONTROL,
)
