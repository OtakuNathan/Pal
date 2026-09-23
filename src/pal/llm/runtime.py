from __future__ import annotations

import asyncio
import hashlib
import json
import random
import sqlite3
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from pal.llm.contracts import (
    LLMGenerationResult,
    LLMPreflightAdvice,
    LLMPreflightRequest,
    LLMRuntimePort,
    ThinkingChoice,
    ThinkingContract,
)
from pal.llm.credentials import LLMCredentialResolver, LLMCredentialUnavailableError
from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.endpoint_spec import LLMEndpointSpec
from pal.llm.ir import (
    GenerationPolicyIR,
    ImagePartIR,
    LLMFinishReason,
    LLMMessageIR,
    LLMRequestIR,
    LLMResponseIR,
    LLMResponseDeltaKind,
    LLMResponseUpdate,
    MessageRole,
    MessageState,
    TextPartIR,
    ThinkingLevel,
    WireShape,
)
from pal.llm.prompt_cache import CacheProfileError
from pal.llm.projection_contracts import (
    EndpointBinding,
    LogicalSessionId,
    ProjectionSendReceipt,
)
from pal.llm.projection_session import EndpointProjectionSession
from pal.llm.model_hooks import ModelHookRegistry
from pal.llm.models import LLMEndpointModel
from pal.llm.output_recovery import (
    continuation_request,
    has_committed_tool_calls,
    endpoint_output_upper_limit,
    merge_responses,
    recovery_settings,
    safe_truncated_response,
    stream_recovery_updates,
    with_recovery_stage,
)
from pal.llm.repository import LLMEndpointRepository, RuntimeSettingRepository
from pal.llm.response_hooks import (
    ProviderResponseHookError,
    ProviderResponseHookRegistry,
)
from pal.llm.usage import LLMUsageLedger
from pal.llm.transport import (
    RequestSubmission,
    DirectSDKTransport,
    LLMEndpointSpecStaleError,
    LLMProviderStartedError,
    LLMStreamControl,
    LLMStreamCancelledError,
)
from pal.shared import LLMPreflightStatus
from pal.shared.json_values import thaw_json
from pal.shared.tool_protocol import ToolCallIR


_DEFAULT_TIMEOUT_SECONDS = 600.0
_STRICT_ENDPOINT_PREFERRED_SOURCES = frozenset({"profile"})
_FALLBACK_DISABLED_POLICIES = frozenset(
    {"disabled", "none", "off", "strict", "strict_preferred", "no_fallback"}
)


def _native_sink_collector(box: dict[str, Any]) -> Callable[[Any], None]:
    """Single-slot box the invoker deposits the attempt's native capture in."""

    def collect(candidate: Any) -> None:
        box.setdefault("native", candidate)

    return collect


def _invoker_accepts_native_sink(invoker: Any, method: str = "invoke_updates", *, keyword: str = "native_sink") -> bool:
    """Whether the invoker's send method accepts an optional observer.

    Subclasses that narrow the base signature (historically: without
    ``**kwargs``) must not receive the kwarg — a TypeError at call time
    would count as an endpoint failure before any attempt runs.
    """

    import inspect

    try:
        signature = inspect.signature(getattr(invoker, method))
    except (TypeError, ValueError, AttributeError):
        return False
    if keyword in signature.parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


class LLMEndpointInvocationError(RuntimeError):
    pass


class LLMEndpointResponseError(LLMEndpointInvocationError):
    pass


class LLMRequestPreparationError(LLMEndpointInvocationError):
    pass


@dataclass(frozen=True)
class PreparedLLMRequest:
    endpoint: LLMEndpointModel
    request: LLMRequestIR
    estimated_input_tokens: int
    target_input_budget: int

    @property
    def compact_required(self) -> bool:
        return (
            self.target_input_budget > 0
            and self.estimated_input_tokens > self.target_input_budget
        )


@dataclass(frozen=True)
class LLMPreparedPlan:
    """One immutable prepared-generation plan (F1, review af51d74).

    Derived BEFORE any projection encodes anything: the resolved endpoint
    (first in the request's own preference/fallback order), the compiled
    EFFECTIVE request (model hooks, effective thinking, output caps, cache
    policy selection), the validated capability profile, and the strong
    projection binding.  A live projection encodes ``effective_request``
    and only THIS plan's endpoint may apply it; anything else must report
    the projection unapplied (F2 receipt).
    """

    endpoint: LLMEndpointModel
    prepared: PreparedLLMRequest
    binding: EndpointBinding
    capabilities: Mapping[str, Any]

    @property
    def effective_request(self) -> LLMRequestIR:
        return self.prepared.request

    @property
    def endpoint_id(self) -> str:
        return str(self.endpoint.endpoint_id)

    @property
    def model_id(self) -> str:
        return str(self.endpoint.model_id)

    @property
    def wire_shape(self) -> str:
        return str(self.endpoint.wire_shape)

    @property
    def compact_required(self) -> bool:
        return self.prepared.compact_required


@dataclass
class EndpointResolver:
    repository: LLMEndpointRepository | None = None
    endpoints: tuple[LLMEndpointModel, ...] = ()

    def __post_init__(self) -> None:
        if self.endpoints:
            self.endpoints = tuple(self.endpoints)
            self._validate()
        else:
            self.refresh()

    def refresh(self) -> tuple[LLMEndpointModel, ...]:
        if self.repository is not None:
            self.endpoints = tuple(self.repository.list_enabled())
        self._validate()
        return self.endpoints

    def _validate(self) -> None:
        for endpoint in self.endpoints:
            LLMEndpointSpec.from_value(endpoint)

    def enabled(
        self,
        *,
        preferred_endpoint_id: str | None = None,
        fallback_endpoint_id: str | None = None,
        include_remaining: bool = True,
    ) -> list[LLMEndpointModel]:
        items = list(self.endpoints)
        ordered: list[LLMEndpointModel] = []
        seen: set[str] = set()
        for endpoint_id in (preferred_endpoint_id, fallback_endpoint_id):
            normalized = str(endpoint_id or "").strip()
            if not normalized or normalized in seen:
                continue
            match = next((item for item in items if item.endpoint_id == normalized), None)
            if match is not None:
                ordered.append(match)
                seen.add(normalized)
        if include_remaining:
            ordered.extend(item for item in items if item.endpoint_id not in seen)
        return ordered if ordered else (items if include_remaining else items[:1])

    def primary(
        self,
        *,
        preferred_endpoint_id: str | None = None,
        fallback_endpoint_id: str | None = None,
    ) -> LLMEndpointModel | None:
        enabled = self.enabled(
            preferred_endpoint_id=preferred_endpoint_id,
            fallback_endpoint_id=fallback_endpoint_id,
        )
        return enabled[0] if enabled else None


class LLMEndpointInvokerPort(Protocol):
    def invoke(
        self,
        endpoint: LLMEndpointModel,
        request: LLMRequestIR,
        *,
        stream: bool = False,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> tuple[LLMResponseIR, tuple[LLMResponseUpdate, ...]]:
        ...

    def invoke_updates(
        self,
        endpoint: LLMEndpointModel,
        request: LLMRequestIR,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        stream_control: LLMStreamControl | None = None,
    ) -> Iterator[LLMResponseUpdate]:
        ...


def build_default_endpoint_invoker(
    *,
    credentials: LLMCredentialResolver | None = None,
    runtime_root: str | Path | None = None,
    response_hooks: ProviderResponseHookRegistry | None = None,
) -> ShapeEndpointInvoker:
    _ = runtime_root
    resolver = credentials or LLMCredentialResolver()
    return ShapeEndpointInvoker(
        transport=DirectSDKTransport(
            credential_resolver=resolver.resolve_api_key,
        ),
        response_hooks=response_hooks or ProviderResponseHookRegistry.builtin(),
    )


@dataclass
class LLMRuntime(LLMRuntimePort):
    endpoint_resolver: EndpointResolver
    settings_repository: RuntimeSettingRepository
    endpoint_invoker: LLMEndpointInvokerPort | None = None
    config: Any = None
    safety_margin_tokens: int = 16_384
    endpoint_retry_attempts: int = 3
    last_request: LLMRequestIR | None = None
    last_endpoint_id: str | None = None
    last_model_id: str | None = None
    # F2: the send receipt of the most recent generation that was offered a
    # projection (None when none was offered); cleared per generation.
    last_projection_receipt: Any = None
    think_level: str = ""
    active_endpoint_id: str | None = None
    _endpoint_fallback_enabled: bool | None = None
    event_sink: Callable[[dict[str, Any]], None] | None = None
    usage_ledger: LLMUsageLedger = field(default_factory=LLMUsageLedger, repr=False)
    cache_profile_generation: int = field(default=1, init=False)
    model_hooks: ModelHookRegistry = field(init=False)
    provider_response_hooks: ProviderResponseHookRegistry = field(init=False)
    _detached_stream_tasks: set[asyncio.Task[Any]] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    # v3: per-scope endpoint projection owners hosted by this runtime.
    # Sessions bind lazily to the ACTIVE endpoint; a binding change rebinds
    # and destroys the old lineage (bind semantics, P06).  Nothing else in
    # the runtime touches their internals.
    _projection_sessions: dict[str, EndpointProjectionSession] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        runtime_root = Path(getattr(self.config, "runtime_root", None) or ".")
        self.model_hooks = ModelHookRegistry.load(runtime_root)
        self.provider_response_hooks = ProviderResponseHookRegistry.builtin()
        if self.endpoint_invoker is None:
            self.endpoint_invoker = build_default_endpoint_invoker(
                runtime_root=runtime_root,
                response_hooks=self.provider_response_hooks,
            )
        elif isinstance(self.endpoint_invoker, ShapeEndpointInvoker):
            # Initial decoding and post-continuation normalization are two
            # passes through one immutable provider-response pipeline.
            self.provider_response_hooks = self.endpoint_invoker.response_hooks
        if isinstance(self.endpoint_invoker, ShapeEndpointInvoker):
            self.endpoint_invoker.attempt_sink = self.usage_ledger.record_attempt
        self.endpoint_retry_attempts = max(
            1,
            int(getattr(self.config, "llm_endpoint_retry_attempts", self.endpoint_retry_attempts) or 1),
        )
        self.refresh_runtime_settings()

    def active_endpoint(self) -> LLMEndpointModel | None:
        return self.endpoint_resolver.primary(preferred_endpoint_id=self.active_endpoint_id)

    def endpoint_projection_session(
        self,
        scope_id: str,
        *,
        plan: "LLMPreparedPlan | None" = None,
        rebind: bool = True,
    ) -> EndpointProjectionSession | None:
        """Host the per-scope projection owner bound to a resolved endpoint.

        F1 (review af51d74): when ``plan`` is supplied the session binds
        against the plan's RESOLVED endpoint with its strong binding (real
        spec identity, the wire shape's continuation contract version, and
        a config fingerprint covering the validated capability profile) —
        never a placeholder identity derived from ``active_endpoint()``.
        Without a plan the binding is derived from the active endpoint with
        the same strong builder.  ``rebind=False`` returns the hosted
        session WITHOUT destroying a drifted lineage — post-commit
        readers (the left-replacement rebase) must not lose the lineage
        they are about to repair.  Returns None while no endpoint is
        resolvable.
        """

        if plan is not None:
            binding = plan.binding
            capabilities = plan.capabilities
        else:
            endpoint = self.active_endpoint()
            if endpoint is None:
                return None
            try:
                capabilities = self._projection_capabilities(
                    endpoint,
                    selection=(self.cache_policy_snapshot()
                               .get(str(endpoint.endpoint_id)) or {}).get("policy"),
                )
                binding = self._projection_binding(
                    endpoint, capabilities=capabilities)
            except Exception:
                return None
        key = str(scope_id or "pal:resident").strip() or "pal:resident"
        session = self._projection_sessions.get(key)
        if session is None:
            session = EndpointProjectionSession(LogicalSessionId(key))
            self._projection_sessions[key] = session
        if session.binding is None or session.binding != binding:
            if not rebind:
                return session
            session.rebind(binding, capabilities=capabilities)
        return session

    def prepare_generation_plan(
        self, request: LLMRequestIR
    ) -> "LLMPreparedPlan | None":
        """Derive the immutable prepared-generation plan for one request.

        F1 (review af51d74): the plan resolves the endpoint exactly the way
        ``_generate`` will (preference, fallback policy, vision filtering)
        and compiles the effective request ONCE — model hooks, effective
        thinking settings, endpoint output caps, and the validated cache
        policy selection.  The live projection encodes THIS request; a
        generation that ends up on a different endpoint re-compiles and
        must report the projection unapplied.  None means the honest cold
        path (no endpoint, or compilation refused).
        """

        try:
            endpoints = self._enabled_endpoints(request)
        except Exception:
            return None
        if not endpoints:
            return None
        endpoint = endpoints[0]
        try:
            prepared = self._compile_request(endpoint, request)
        except Exception:
            return None
        capabilities = self._projection_capabilities(
            endpoint, selection=prepared.request.metadata.get("cache_policy_selection"))
        binding = self._projection_binding(endpoint, capabilities=capabilities)
        return LLMPreparedPlan(
            endpoint=endpoint,
            prepared=prepared,
            binding=binding,
            capabilities=capabilities,
        )

    def retire_projection_sessions(self) -> None:
        """Retire every hosted logical-scope projection owner (F6).

        Soft reset rolls the history authority's session incarnation; the
        hosted projection lineages are retired with it and recreated lazily
        by their next user.  Frozen prefixes must never outlive the history
        they were frozen from.
        """

        for session in self._projection_sessions.values():
            try:
                session.retire()
            except Exception:
                pass
        self._projection_sessions.clear()

    def _projection_capabilities(
        self,
        endpoint: LLMEndpointModel,
        *,
        selection: Any,
    ) -> dict[str, Any]:
        """The validated capability profile for one projection binding."""

        capabilities = thaw_json(
            dict(getattr(endpoint, "capabilities_blob", None) or {})
        )
        if isinstance(selection, Mapping):
            capabilities["prompt_cache"] = thaw_json(dict(selection))
        return capabilities

    def _endpoint_identity_fields(self, endpoint: LLMEndpointModel) -> dict[str, Any]:
        """R1 (review 95373ef): the endpoint object's OWN configuration.

        Everything here is recomputable from a live endpoint at reuse time.
        Runtime-injected selection (cache-policy generation counters, model-
        hook provenance) is deliberately NOT part of this view — it belongs
        to the binding's display fingerprint, not to the endpoint's
        configuration identity."""

        return {
            "endpoint_id": str(endpoint.endpoint_id),
            "model_id": str(endpoint.model_id),
            "wire_shape": str(endpoint.wire_shape),
            "provider": str(endpoint.provider),
            "base_url": str(endpoint.base_url or ""),
            "context_window": int(getattr(endpoint, "context_window", 0) or 0),
            "max_output_tokens": int(getattr(endpoint, "max_output_tokens", 0) or 0),
            "supports_tools": bool(endpoint.supports_tools),
            "supports_streaming": bool(endpoint.supports_streaming),
            "supports_vision": bool(endpoint.supports_vision),
            "thinking_levels": [
                str(level) for level in (getattr(endpoint, "thinking_levels_blob", None) or ())
            ],
            "default_thinking_level": str(
                getattr(endpoint, "default_thinking_level", "") or ""
            ),
            "declared_spec_revision": str(
                getattr(endpoint, "endpoint_spec_revision", "") or ""
            ).strip(),
            "capabilities_blob": thaw_json(
                dict(getattr(endpoint, "capabilities_blob", None) or {})
            ),
        }

    def _endpoint_config_digest(self, endpoint: LLMEndpointModel) -> str:
        """R1: digest over the endpoint's own config, recomputable anywhere.

        This is the reuse admission check for projections and plans: the
        same endpoint id/model/shape does not prove the same validated
        profile, so callers compare THIS digest before trusting a prepared
        binding."""

        return hashlib.sha256(
            json.dumps(
                self._endpoint_identity_fields(endpoint),
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()

    def _projection_binding(
        self,
        endpoint: LLMEndpointModel,
        *,
        capabilities: Mapping[str, Any],
    ) -> EndpointBinding:
        """Strong projection binding (F1): no placeholder identity.

        ``endpoint_spec_revision`` comes from the endpoint when declared and
        otherwise from a digest of the endpoint's own config; the
        continuation policy version is the wire shape's STRUCTURAL contract
        version; the config fingerprint covers endpoint identity, spec
        revision, policy version, and the validated capability profile, so
        any config drift is a new binding (lineage destruction), never an
        in-place mutation.
        """

        identity_fields = {
            **self._endpoint_identity_fields(endpoint),
            "capabilities": thaw_json(dict(capabilities or {})),
        }
        spec_revision = str(
            getattr(endpoint, "endpoint_spec_revision", "") or ""
        ).strip()
        if not spec_revision:
            spec_revision = "spec-" + hashlib.sha256(
                json.dumps(identity_fields, sort_keys=True, default=str)
                .encode("utf-8")
            ).hexdigest()[:16]
        from pal.llm.continuation_policy import contract_for_shape

        contract = contract_for_shape(WireShape(str(endpoint.wire_shape)))
        policy_version = (
            contract.contract_version if contract is not None else "uncontracted"
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "identity": identity_fields,
                    "endpoint_spec_revision": spec_revision,
                    "continuation_policy_version": policy_version,
                },
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        return EndpointBinding(
            endpoint_id=str(endpoint.endpoint_id),
            model_id=str(endpoint.model_id),
            wire_shape=WireShape(str(endpoint.wire_shape)),
            endpoint_spec_revision=spec_revision,
            continuation_policy_version=policy_version,
            config_fingerprint=f"fingerprint-{fingerprint}",
            endpoint_config_digest=self._endpoint_config_digest(endpoint),
        )

    def _binding_is_current(
        self,
        binding: EndpointBinding,
        endpoint: LLMEndpointModel,
    ) -> bool:
        """R1 (review 95373ef): is this binding still the endpoint's config?

        Same endpoint id/model/shape is NOT a validated profile (J1): the
        endpoint's own configuration must still be the one the binding was
        derived from.  The check recomputes the endpoint-config digest from
        the LIVE endpoint; any drift (capabilities, output caps, spec
        revision) makes the caller fall back to a fresh compile (plans) or
        an honest cold send (projections) — never an old effective request
        wearing a refreshed profile.  A refresh that changes nothing but
        the cache-policy generation counter keeps the projection: that
        counter is runtime bookkeeping, not endpoint configuration.
        """

        try:
            current_digest = self._endpoint_config_digest(endpoint)
        except Exception:
            return False
        recorded = str(getattr(binding, "endpoint_config_digest", "") or "")
        if not recorded:
            # Manual/legacy bindings carry no config digest; nothing is
            # recomputable, so the caller's own field-level checks stand
            # (historical behaviour).
            return True
        return current_digest == recorded

    def _plan_is_current(
        self,
        plan: "LLMPreparedPlan",
        endpoint: LLMEndpointModel,
    ) -> bool:
        """R1: a prepared plan may be reused only while its strong binding
        still matches the endpoint's live config; a refreshed or replaced
        profile must re-compile instead of re-sending the old plan's
        effective request."""

        return self._binding_is_current(plan.binding, endpoint)

    def refresh_runtime_settings(self) -> None:
        previous = self.active_endpoint()
        configured = self.settings_repository.get_active_llm_endpoint_id()
        endpoint_ids = {endpoint.endpoint_id for endpoint in self.endpoint_resolver.endpoints}
        self.active_endpoint_id = configured if configured in endpoint_ids else None
        self._endpoint_fallback_enabled = None
        endpoint = self.active_endpoint()
        self.think_level = self._effective_thinking_level(endpoint) or ""
        if endpoint is not None and (
            previous is None or previous.endpoint_id != endpoint.endpoint_id
        ):
            activate = getattr(self.endpoint_invoker, "activate_endpoint", None)
            if callable(activate):
                activate(endpoint.endpoint_id)

    def refresh_llm_endpoints(self) -> dict[str, Any]:
        before = {endpoint.endpoint_id for endpoint in self.endpoint_resolver.endpoints}
        self.endpoint_resolver.refresh()
        refresh_settings = getattr(self.settings_repository, "refresh", None)
        if callable(refresh_settings):
            refresh_settings()
        runtime_root = Path(getattr(self.config, "runtime_root", None) or ".")
        self.model_hooks = ModelHookRegistry.load(runtime_root)
        self.cache_profile_generation += 1
        refresh_credentials = getattr(
            self.endpoint_invoker,
            "refresh_credentials",
            None,
        )
        credentials_refreshed = bool(
            refresh_credentials()
            if callable(refresh_credentials)
            else False
        )
        self.refresh_runtime_settings()
        after = {endpoint.endpoint_id for endpoint in self.endpoint_resolver.endpoints}
        primary = self.active_endpoint()
        return {
            "before_count": len(before),
            "enabled_count": len(after),
            "added_endpoint_ids": sorted(after - before),
            "removed_endpoint_ids": sorted(before - after),
            "configured_active_endpoint_id": self.settings_repository.get_active_llm_endpoint_id(),
            "active_endpoint_id": self.active_endpoint_id,
            "primary_endpoint_id": primary.endpoint_id if primary else None,
            "primary_model_id": primary.model_id if primary else None,
            "model_hook_count": len(self.model_hooks.hooks),
            "credentials_refreshed": credentials_refreshed,
        }

    def set_active_endpoint(self, endpoint_id: str) -> str:
        normalized = str(endpoint_id or "").strip()
        if not any(endpoint.endpoint_id == normalized for endpoint in self.endpoint_resolver.endpoints):
            raise ValueError(f"unknown enabled LLM endpoint: {normalized}")
        self.settings_repository.set_active_llm_endpoint_id(normalized)
        self.active_endpoint_id = normalized
        self.think_level = self._effective_thinking_level(self.active_endpoint()) or ""
        activate = getattr(self.endpoint_invoker, "activate_endpoint", None)
        if callable(activate):
            activate(normalized)
        return normalized

    def close(self) -> None:
        close = getattr(self.endpoint_invoker, "close", None)
        if callable(close):
            close()

    def thinking_contract(self, endpoint_id: str | None = None) -> ThinkingContract | None:
        endpoint = self._endpoint_by_id(endpoint_id) if endpoint_id else self.active_endpoint()
        if endpoint is None:
            return None
        levels = self._thinking_levels(endpoint)
        if not levels:
            return None
        return ThinkingContract(
            choices=tuple(ThinkingChoice(level, level.replace("xhigh", "extra high").title()) for level in levels),
            default_choice_id=str(endpoint.default_thinking_level),
        )

    def thinking_status(self, endpoint_id: str | None = None) -> dict[str, Any]:
        endpoint = self._endpoint_by_id(endpoint_id) if endpoint_id else self.active_endpoint()
        if endpoint is None:
            return {"available": False, "endpoint_id": None, "model_id": None, "current": None, "choices": []}
        levels = self._thinking_levels(endpoint)
        return {
            "available": bool(levels),
            "endpoint_id": endpoint.endpoint_id,
            "model_id": endpoint.model_id,
            "current": self._effective_thinking_level(endpoint),
            "choices": [{"id": level, "label": level.replace("xhigh", "extra high").title()} for level in levels],
        }

    def thinking_levels_snapshot(self) -> dict[str, str]:
        return {
            endpoint.endpoint_id: level
            for endpoint in self.endpoint_resolver.enabled()
            if (level := self._effective_thinking_level(endpoint)) is not None
        }

    def set_think_level(self, value: str, *, endpoint_id: str | None = None) -> str:
        endpoint = self._endpoint_by_id(endpoint_id) if endpoint_id else self.active_endpoint()
        if endpoint is None:
            raise ValueError("no enabled LLM endpoint is available")
        normalized = str(value or "").strip().lower()
        levels = self._thinking_levels(endpoint)
        if normalized not in levels:
            raise ValueError(f"invalid think level for {endpoint.endpoint_id}; available: {', '.join(levels)}")
        self.settings_repository.set_think_level(endpoint.endpoint_id, normalized)
        if endpoint.endpoint_id == self.active_endpoint_id:
            self.think_level = normalized
        return normalized

    def preflight(self, request: LLMPreflightRequest) -> LLMPreflightAdvice:
        self.refresh_runtime_settings()
        endpoints = self._enabled_endpoints(request.request)
        endpoint = endpoints[0] if endpoints else None
        if endpoint is None:
            raise LLMEndpointInvocationError("no enabled endpoints are available")
        prepared = self._compile_request(endpoint, request.request)
        return LLMPreflightAdvice(
            status=(
                LLMPreflightStatus.COMPACT_REQUIRED
                if prepared.compact_required
                else LLMPreflightStatus.READY
            ),
            active_model=endpoint.model_id,
            fallback_chain=[item.model_id for item in endpoints[1:]],
            target_input_budget=prepared.target_input_budget,
            reserved_output_tokens=prepared.request.policy.max_output_tokens,
            breakdown={
                "estimated_input_tokens": prepared.estimated_input_tokens,
                "target_input_budget": prepared.target_input_budget,
            },
        )

    async def apreflight(self, request: LLMPreflightRequest) -> LLMPreflightAdvice:
        return self.preflight(request)

    def resolve_endpoint_facts(
        self,
        *,
        preferred_endpoint_id: str | None = None,
        preferred_endpoint_source: str | None = None,
    ) -> dict[str, Any]:
        endpoints = self._enabled_endpoints_for_preference(
            preferred_endpoint_id=preferred_endpoint_id,
            preferred_endpoint_source=preferred_endpoint_source,
        )
        endpoint = endpoints[0] if endpoints else None
        if endpoint is None:
            return {
                "endpoint_id": str(preferred_endpoint_id or self.active_endpoint_id or "") or None,
                "model_id": None,
                "wire_shape": None,
                "context_window": None,
                "max_output_tokens": None,
                "supports_streaming": False,
                "supports_tools": False,
                "supports_vision": False,
                "input_modalities": [],
                "output_modalities": [],
                "capabilities": {},
                "thinking_levels": [],
                "default_thinking_level": None,
            }
        return {
            "endpoint_id": endpoint.endpoint_id,
            "model_id": endpoint.model_id,
            "wire_shape": endpoint.wire_shape,
            "context_window": endpoint.context_window,
            "max_output_tokens": endpoint.max_output_tokens,
            "max_output_tokens_upper_limit": endpoint_output_upper_limit(endpoint),
            "supports_streaming": bool(endpoint.supports_streaming),
            "supports_tools": bool(endpoint.supports_tools),
            "supports_vision": bool(endpoint.supports_vision),
            "input_modalities": list(endpoint.input_modalities_blob or ()),
            "output_modalities": list(endpoint.output_modalities_blob or ()),
            "capabilities": dict(endpoint.capabilities_blob or {}),
            "thinking_levels": list(endpoint.thinking_levels_blob or ()),
            "default_thinking_level": endpoint.default_thinking_level,
        }

    def resolve_max_output_tokens(
        self,
        *,
        preferred_endpoint_id: str | None = None,
        preferred_endpoint_source: str | None = None,
    ) -> int | None:
        endpoints = self._enabled_endpoints_for_preference(
            preferred_endpoint_id=preferred_endpoint_id,
            preferred_endpoint_source=preferred_endpoint_source,
        )
        endpoint = endpoints[0] if endpoints else None
        if endpoint is None:
            return None
        if endpoint.max_output_tokens is not None:
            return int(endpoint.max_output_tokens)
        return int(endpoint.context_window) if endpoint.context_window is not None else None

    def supports_streaming(self, request: LLMRequestIR | None = None) -> bool:
        endpoints = self._enabled_endpoints(request) if request is not None else [self.active_endpoint()]
        endpoint = endpoints[0] if endpoints else None
        return bool(endpoint and endpoint.supports_streaming)

    def generate(
        self, request: LLMRequestIR, *,
        projection: "EncodedRequest | None" = None,
        projection_binding: "EndpointBinding | None" = None,
        projection_attempt_id: str = "",
        generation_plan: "LLMPreparedPlan | None" = None,
        on_submitted: Callable[[RequestSubmission], None] | None = None,
    ) -> LLMGenerationResult:
        return self._generate(
            request, allow_stale_refresh=True,
            projection=projection, projection_binding=projection_binding,
            projection_attempt_id=projection_attempt_id,
            generation_plan=generation_plan,
            on_submitted=on_submitted,
        )

    def _generate(
        self,
        request: LLMRequestIR,
        *,
        allow_stale_refresh: bool,
        projection: "EncodedRequest | None" = None,
        projection_binding: "EndpointBinding | None" = None,
        projection_attempt_id: str = "",
        generation_plan: "LLMPreparedPlan | None" = None,
        on_submitted: Callable[[RequestSubmission], None] | None = None,
    ) -> LLMGenerationResult:
        self.last_request = request
        # F2: receipts are per generation; clearing here keeps a stale
        # receipt from an earlier round from authorizing a later freeze.
        self.last_projection_receipt = None
        try:
            endpoints = self._enabled_endpoints(request)
        except Exception as exc:
            self.usage_ledger.record_failed_request()
            return _failure_result(str(exc), exc=exc)
        if not endpoints:
            return _failure_result("no enabled endpoints are available")
        last_error: Exception | None = None
        requested_preferred = str(request.metadata.get("preferred_endpoint_id") or "").strip() or None
        for endpoint_index, endpoint in enumerate(endpoints):
            # F1: when the prepared plan resolved THIS endpoint, reuse its
            # compiled request verbatim — the projected payload is the
            # encoding of exactly this effective request, not a parallel
            # compile.  R1: the plan is a config snapshot; reuse requires
            # its strong binding to still match the endpoint's live config.
            if (
                generation_plan is not None
                and str(endpoint.endpoint_id) == generation_plan.endpoint_id
                and self._plan_is_current(generation_plan, endpoint)
            ):
                prepared = generation_plan.prepared
            else:
                try:
                    prepared = self._compile_request(endpoint, request)
                except CacheProfileError as exc:
                    self.usage_ledger.record_failed_request(endpoint_id=endpoint.endpoint_id)
                    return _failure_result(str(exc), exc=exc)
                except Exception as exc:
                    last_error = exc
                    error_kind = self._record_failure(endpoint, exc, 0, provider_attempt=False)
                    self._emit(
                        "llm_endpoint_exhausted",
                        endpoint=endpoint,
                        reason=error_kind,
                    )
                    continue
            effective = prepared.request
            if prepared.compact_required:
                return self._compact_required_result(endpoint, effective)
            if _is_stub_endpoint(endpoint):
                response = _text_response("stub response", LLMFinishReason.STUB)
                return self._success(endpoint, response)
            for attempt in range(self.endpoint_retry_attempts):
                native_box: dict[str, Any] = {}
                try:
                    invoke_kwargs: dict[str, Any] = {
                        "stream": False,
                        "timeout_seconds": self._timeout_seconds(effective),
                    }
                    attempt_projection = None
                    if isinstance(self._invoker(), ShapeEndpointInvoker):
                        attempt_projection = self._projection_for_endpoint(
                            endpoint, projection, projection_binding)
                        if on_submitted is not None and _invoker_accepts_native_sink(
                            self._invoker(), "invoke", keyword="submission_sink"
                        ):
                            invoke_kwargs["submission_sink"] = on_submitted
                        if attempt_projection is not None:
                            invoke_kwargs["projection"] = attempt_projection
                        # F3: the codec-level native capture for THIS attempt
                        # travels back with the send receipt.
                        if _invoker_accepts_native_sink(self._invoker(), "invoke"):
                            invoke_kwargs["native_sink"] = _native_sink_collector(native_box)
                    response, _ = self._invoker().invoke(
                        endpoint,
                        effective,
                        **invoke_kwargs,
                    )
                    if response.finish_reason == LLMFinishReason.LENGTH:
                        first = response
                        response = self._recover_length(endpoint, effective, response)
                        recovery_rewrote = response is not first
                        # Recovery merges multiple already-decoded pieces, so
                        # the merged response crosses the same provider
                        # decorator once more. Non-recovered responses were
                        # normalized by the endpoint iterator already.
                        response = self._normalize_completed_response(
                            endpoint,
                            effective,
                            response,
                        )
                        if recovery_rewrote:
                            # R2 (review 95373ef): recovery rewrote the
                            # contribution (merged continuations or discarded
                            # the first attempt).  The first attempt's codec
                            # capture no longer represents the accepted
                            # output — revoke it so the receipt cannot freeze
                            # a half response as this turn's native lineage.
                            native_box.clear()
                    if response.finish_reason == LLMFinishReason.ERROR:
                        raise _accounted_response_error(endpoint, response)
                    if requested_preferred is None and endpoint.endpoint_id != self.active_endpoint_id:
                        self.set_active_endpoint(endpoint.endpoint_id)
                    if endpoint_index > 0:
                        self._emit("llm_endpoint_fallback_succeeded", endpoint=endpoint)
                    receipt = self._projection_send_receipt(
                        attempt_id=projection_attempt_id,
                        endpoint=endpoint,
                        projection=projection,
                        binding=projection_binding,
                        applied=attempt_projection is not None,
                        native_box=native_box,
                    )
                    if receipt is not None:
                        self.last_projection_receipt = receipt
                    return self._success(endpoint, response, projection_receipt=receipt)
                except LLMEndpointSpecStaleError as exc:
                    if allow_stale_refresh:
                        self._emit(
                            "llm_endpoint_spec_refresh",
                            endpoint=endpoint,
                            reason="endpoint_spec_stale",
                        )
                        self.refresh_llm_endpoints()
                        # F2: the sync refresh recursion keeps the projection
                        # arguments exactly like the stream path — dropping
                        # them here silently downgraded the round to cold.
                        # R1: the stale-spec event revokes the prepared plan
                        # outright (J1): the retry re-derives the effective
                        # request from the REFRESHED endpoint config.  The
                        # projection rides along, but _projection_for_endpoint
                        # re-validates its binding against the live config and
                        # drops it when anything drifted.
                        return self._generate(
                            request,
                            allow_stale_refresh=False,
                            projection=projection,
                            projection_binding=projection_binding,
                            projection_attempt_id=projection_attempt_id,
                            generation_plan=None,
                            on_submitted=on_submitted,
                        )
                    last_error = exc
                    error_kind = self._record_failure(endpoint, exc, attempt)
                    self._emit(
                        "llm_endpoint_exhausted",
                        endpoint=endpoint,
                        reason=error_kind,
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    error_kind = self._record_failure(endpoint, exc, attempt)
                    retryable = _retryable_error_kind(error_kind)
                    if retryable and attempt + 1 < self.endpoint_retry_attempts:
                        time.sleep(_retry_delay(attempt + 1))
                        continue
                    self._emit(
                        "llm_endpoint_exhausted",
                        endpoint=endpoint,
                        reason=error_kind,
                    )
                    break
        self.usage_ledger.record_failed_request(
            endpoint_id=endpoints[-1].endpoint_id if endpoints else ""
        )
        return _failure_result(
            _public_failure_text(last_error),
            exc=last_error,
        )

    def _projection_send_receipt(
        self,
        *,
        attempt_id: str,
        endpoint: LLMEndpointModel,
        projection: "EncodedRequest | None",
        binding: "EndpointBinding | None",
        applied: bool,
        native_box: dict[str, Any] | None = None,
    ) -> ProjectionSendReceipt | None:
        """F2: build the typed send receipt when a projection was offered.

        No projection offered → no receipt (None); offered but dropped →
        applied=False with the resolved endpoint that actually served, so
        the owner rejects the round instead of freezing a payload the
        provider never saw.  Native material only rides an APPLIED
        receipt — a cold send's capture authorizes nothing.
        """

        if projection is None or binding is None or not str(attempt_id or "").strip():
            return None
        candidate = (native_box or {}).get("native") if applied else None
        return ProjectionSendReceipt(
            attempt_id=str(attempt_id),
            resolved_endpoint_id=str(endpoint.endpoint_id),
            resolved_model_id=str(endpoint.model_id),
            resolved_wire_shape=str(endpoint.wire_shape),
            applied=bool(applied),
            detail="sent" if applied else "projection_dropped_for_endpoint",
            binding=binding,
            native=candidate,
        )

    def _projection_for_endpoint(
        self,
        endpoint: LLMEndpointModel,
        projection: "EncodedRequest | None",
        binding: "EndpointBinding | None",
    ) -> "EncodedRequest | None":
        """W3 (review): a projection is usable only on the endpoint it was
        prepared against.  The owner prepared it for ``binding``; if THIS
        resolved endpoint (including fallback) differs, drop the projection
        and let the codec encode cold — correct, just without the cached
        prefix benefit.

        R1 (review 95373ef): id/model/shape equality is not a profile proof.
        The binding is re-derived from the endpoint's LIVE config and the
        projection is dropped on any drift (capabilities, output caps, spec
        revision, cache-policy generation), so a refreshed endpoint can
        never re-send the old encode."""

        if projection is None or binding is None:
            return None
        try:
            from pal.llm.projection_contracts import EndpointBinding as _Binding

            if not isinstance(binding, _Binding):
                return None
            if (
                str(binding.endpoint_id) == str(endpoint.endpoint_id)
                and str(binding.model_id) == str(endpoint.model_id)
                and str(getattr(binding.wire_shape, "value", binding.wire_shape))
                == str(endpoint.wire_shape)
                and self._binding_is_current(binding, endpoint)
            ):
                return projection
        except Exception:
            return None
        return None

    async def agenerate(
        self, request: LLMRequestIR, *,
        projection: "EncodedRequest | None" = None,
        projection_binding: "EndpointBinding | None" = None,
        projection_attempt_id: str = "",
        generation_plan: "LLMPreparedPlan | None" = None,
        on_submitted: Callable[[RequestSubmission], None] | None = None,
    ) -> LLMGenerationResult:
        loop = asyncio.get_running_loop()
        notify = (lambda receipt: loop.call_soon_threadsafe(on_submitted, receipt)) if on_submitted else None
        return await asyncio.to_thread(
            self.generate, request,
            projection=projection, projection_binding=projection_binding,
            projection_attempt_id=projection_attempt_id,
            generation_plan=generation_plan,
            on_submitted=notify,
        )

    def _iter_stream_updates(
        self,
        request: LLMRequestIR,
        *,
        stream_control: LLMStreamControl | None = None,
        allow_stale_refresh: bool = True,
        projection: "EncodedRequest | None" = None,
        projection_binding: "EndpointBinding | None" = None,
        projection_attempt_id: str = "",
        generation_plan: "LLMPreparedPlan | None" = None,
        on_submitted: Callable[[RequestSubmission], None] | None = None,
    ) -> Iterator[LLMResponseUpdate]:
        self.last_request = request
        # F2: per-generation receipt; cleared so late readers cannot see a
        # previous round's receipt.
        self.last_projection_receipt = None
        try:
            endpoints = self._enabled_endpoints(request)
        except Exception as exc:
            self.usage_ledger.record_failed_request()
            response = _failure_result(str(exc), exc=exc).response
            yield LLMResponseUpdate(
                response,
                delta_kind=LLMResponseDeltaKind.STATE,
            )
            return
        if not endpoints:
            response = _failure_result("no enabled endpoints are available").response
            yield LLMResponseUpdate(response, delta_kind=LLMResponseDeltaKind.STATE)
            return
        last_error: Exception | None = None
        for endpoint in endpoints:
            # F1: plan-prepared requests are reused verbatim on their
            # resolved endpoint (the projection encoded exactly this).
            # R1: only while the plan's strong binding still matches the
            # endpoint's live config — a refreshed profile re-compiles.
            if (
                generation_plan is not None
                and str(endpoint.endpoint_id) == generation_plan.endpoint_id
                and self._plan_is_current(generation_plan, endpoint)
            ):
                prepared = generation_plan.prepared
            else:
                try:
                    prepared = self._compile_request(endpoint, request)
                except CacheProfileError as exc:
                    self.usage_ledger.record_failed_request(endpoint_id=endpoint.endpoint_id)
                    yield LLMResponseUpdate(_failure_result(str(exc), exc=exc).response, delta_kind=LLMResponseDeltaKind.STATE)
                    return
                except Exception as exc:
                    last_error = exc
                    error_kind = self._record_failure(endpoint, exc, 0, provider_attempt=False)
                    self._emit(
                        "llm_endpoint_exhausted",
                        endpoint=endpoint,
                        reason=error_kind,
                    )
                    continue
            effective = prepared.request
            if prepared.compact_required:
                response = self._compact_required_result(endpoint, effective).response
                yield LLMResponseUpdate(response, delta_kind=LLMResponseDeltaKind.STATE)
                return
            semantic_seen = False
            for attempt in range(self.endpoint_retry_attempts):
                last_update: LLMResponseUpdate | None = None
                native_box: dict[str, Any] = {}
                attempt_projection = None
                try:
                    invoke_kwargs: dict[str, Any] = {
                        "timeout_seconds": self._timeout_seconds(effective),
                    }
                    if isinstance(self._invoker(), ShapeEndpointInvoker):
                        invoke_kwargs["stream_control"] = stream_control
                        attempt_projection = self._projection_for_endpoint(
                            endpoint, projection, projection_binding)
                        if on_submitted is not None and _invoker_accepts_native_sink(
                            self._invoker(), keyword="submission_sink"
                        ):
                            invoke_kwargs["submission_sink"] = on_submitted
                        if attempt_projection is not None:
                            invoke_kwargs["projection"] = attempt_projection
                        # F3: native capture travels back with the receipt.
                        if _invoker_accepts_native_sink(self._invoker()):
                            invoke_kwargs["native_sink"] = _native_sink_collector(native_box)
                    for update in self._invoker().invoke_updates(
                        endpoint,
                        effective,
                        **invoke_kwargs,
                    ):
                        last_update = update
                        semantic_seen = semantic_seen or (
                            update.delta_kind != LLMResponseDeltaKind.STATE
                            and bool(update.response.message.parts)
                        )
                        if (
                            update.delta_kind == LLMResponseDeltaKind.STATE
                            and update.response.finish_reason
                            in {LLMFinishReason.LENGTH, LLMFinishReason.ERROR}
                        ):
                            continue
                        yield update
                    completed = last_update.response if last_update is not None else _text_response("", LLMFinishReason.ERROR)
                    if completed.finish_reason == LLMFinishReason.LENGTH:
                        first = completed
                        recovered = self._recover_length(
                            endpoint,
                            effective,
                            completed,
                            allow_discarded_retry=False,
                        )
                        recovery_rewrote = recovered is not first
                        recovered = self._normalize_completed_response(
                            endpoint,
                            effective,
                            recovered,
                        )
                        if recovery_rewrote:
                            # R2: the merged continuation is not represented
                            # by the first attempt's capture; revoke it
                            # rather than freezing a half response.
                            native_box.clear()
                        if recovered.finish_reason == LLMFinishReason.ERROR:
                            raise _accounted_response_error(endpoint, recovered)
                        recovery_updates = tuple(stream_recovery_updates(completed, recovered))
                        yield from recovery_updates
                        completed = recovered
                    if completed.finish_reason == LLMFinishReason.ERROR:
                        raise _accounted_response_error(endpoint, completed)
                    receipt = self._projection_send_receipt(
                        attempt_id=projection_attempt_id,
                        endpoint=endpoint,
                        projection=projection,
                        binding=projection_binding,
                        applied=attempt_projection is not None,
                        native_box=native_box,
                    )
                    if receipt is not None:
                        self.last_projection_receipt = receipt
                    self._record_success(endpoint, completed)
                    self.last_endpoint_id = endpoint.endpoint_id
                    self.last_model_id = endpoint.model_id
                    return
                except LLMEndpointSpecStaleError as exc:
                    if allow_stale_refresh and not semantic_seen and not bool(
                        stream_control is not None
                        and stream_control.provider_started
                    ):
                        self._emit(
                            "llm_endpoint_spec_refresh",
                            endpoint=endpoint,
                            reason="endpoint_spec_stale",
                        )
                        self.refresh_llm_endpoints()
                        # R1: stale-spec refresh revokes the plan; the retry
                        # re-derives the effective request from the live
                        # config and re-validates the projection's binding.
                        yield from self._iter_stream_updates(
                            request,
                            stream_control=stream_control,
                            allow_stale_refresh=False,
                            projection=projection,
                            projection_binding=projection_binding,
                            projection_attempt_id=projection_attempt_id,
                            generation_plan=None,
                            on_submitted=on_submitted,
                        )
                        return
                    last_error = exc
                    error_kind = self._record_failure(endpoint, exc, attempt)
                    self._emit(
                        "llm_endpoint_exhausted",
                        endpoint=endpoint,
                        reason=error_kind,
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    error_kind = self._record_failure(endpoint, exc, attempt)
                    provider_started = semantic_seen or bool(
                        stream_control is not None
                        and stream_control.provider_started
                    )
                    if provider_started:
                        partial = (
                            last_update.response
                            if last_update is not None
                            else _text_response(
                                _public_failure_text(exc),
                                LLMFinishReason.ERROR,
                            )
                        )
                        error_response = _response_with_failure(
                            partial,
                            exc,
                            partial_output_chars=(
                                len(str(partial.message.text or ""))
                                if semantic_seen
                                else 0
                            ),
                        )
                        self.usage_ledger.record_failed_request(
                            endpoint_id=endpoint.endpoint_id
                        )
                        yield LLMResponseUpdate(error_response, delta_kind=LLMResponseDeltaKind.STATE)
                        return
                    if (
                        _retryable_error_kind(error_kind)
                        and attempt + 1 < self.endpoint_retry_attempts
                    ):
                        time.sleep(_retry_delay(attempt + 1))
                        continue
                    self._emit(
                        "llm_endpoint_exhausted",
                        endpoint=endpoint,
                        reason=error_kind,
                    )
                    break
        self.usage_ledger.record_failed_request(
            endpoint_id=endpoints[-1].endpoint_id if endpoints else ""
        )
        response = _failure_result(
            str(last_error or "LLM stream failed"),
            exc=last_error,
        ).response
        yield LLMResponseUpdate(response, delta_kind=LLMResponseDeltaKind.STATE)

    async def astream(
        self, request: LLMRequestIR, *,
        projection: "EncodedRequest | None" = None,
        projection_binding: "EndpointBinding | None" = None,
        projection_attempt_id: str = "",
        generation_plan: "LLMPreparedPlan | None" = None,
        on_submitted: Callable[[RequestSubmission], None] | None = None,
    ) -> AsyncIterator[LLMResponseUpdate]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[object] = asyncio.Queue()
        done = object()
        stream_control = LLMStreamControl()
        wall_timeout = self._stream_wall_timeout_seconds(request)
        cleanup_timeout = self._stream_cleanup_timeout_seconds()

        def enqueue(item: object) -> None:
            # A cooperatively cancelled SDK worker can outlive its consumer
            # until the provider read timeout expires.  The resident loop is
            # normally still present, but shutdown may close it first.
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                pass

        def worker() -> None:
            try:
                for update in self._iter_stream_updates(
                    request,
                    stream_control=stream_control,
                    projection=projection,
                    projection_binding=projection_binding,
                    projection_attempt_id=projection_attempt_id,
                    generation_plan=generation_plan,
                    on_submitted=enqueue if on_submitted is not None else None,
                ):
                    enqueue(update)
            except BaseException as exc:  # noqa: BLE001
                enqueue(exc)
            finally:
                enqueue(done)

        task = asyncio.create_task(asyncio.to_thread(worker))
        deadline = loop.time() + wall_timeout
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    stream_control.cancel("wall_timeout")
                    response = _failure_result(
                        f"LLM stream exceeded the {wall_timeout:g}s wall-clock limit"
                    ).response
                    yield LLMResponseUpdate(
                        response,
                        delta_kind=LLMResponseDeltaKind.STATE,
                    )
                    break
                try:
                    item = await asyncio.wait_for(
                        queue.get(),
                        timeout=min(remaining, 1.0),
                    )
                except asyncio.TimeoutError:
                    continue
                if item is done:
                    break
                if isinstance(item, BaseException):
                    raise item
                if isinstance(item, RequestSubmission):
                    if on_submitted is not None:
                        on_submitted(item)
                    continue
                yield item  # type: ignore[misc]
        finally:
            stream_control.cancel("consumer_closed")
            if not task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=cleanup_timeout,
                    )
                except asyncio.TimeoutError:
                    # The SDK owns its hidden socket and must close the active
                    # response on the worker thread after its network timeout.
                    # Cancellation fences every later capability admission;
                    # tracking keeps the worker alive until its finally block
                    # releases the last hazard and closes the client graph.
                    self._track_detached_stream_task(task)
                except asyncio.CancelledError:
                    self._track_detached_stream_task(task)
                    raise
                except LLMStreamCancelledError:
                    pass
            elif not task.cancelled():
                try:
                    task.result()
                except LLMStreamCancelledError:
                    pass

    def _track_detached_stream_task(self, task: asyncio.Task[Any]) -> None:
        if task in self._detached_stream_tasks:
            return
        self._detached_stream_tasks.add(task)
        task.add_done_callback(self._retire_detached_stream_task)

    def _retire_detached_stream_task(self, task: asyncio.Task[Any]) -> None:
        self._detached_stream_tasks.discard(task)
        with suppress(asyncio.CancelledError, Exception):
            task.result()

    def usage_snapshot(self) -> dict[str, Any]:
        snapshot = self.usage_ledger.snapshot()
        prompt_cache = getattr(self.endpoint_invoker, "prompt_cache", None)
        cache_snapshot = getattr(prompt_cache, "snapshot", None)
        if callable(cache_snapshot):
            snapshot["prompt_cache_policy"] = cache_snapshot()
        return snapshot

    def end_prompt_cache_turn(self, turn_id: str) -> None:
        cache = getattr(self.endpoint_invoker, "prompt_cache", None)
        close = getattr(cache, "end_turn", None)
        if callable(close):
            close(str(turn_id))

    def prompt_cache_warm_deadline_snapshot(self) -> dict[str, Any]:
        prompt_cache = getattr(self.endpoint_invoker, "prompt_cache", None)
        snapshot = getattr(prompt_cache, "warm_deadline_snapshot", None)
        active_endpoint = self.active_endpoint()
        return (
            dict(
                snapshot(
                    logical_scope_id="pal:resident",
                    endpoint_id=(
                        str(active_endpoint.endpoint_id)
                        if active_endpoint is not None
                        else ""
                    ),
                )
                or {}
            )
            if callable(snapshot)
            else {}
        )

    def prompt_cache_confirmed_anchor_request(
        self,
        *,
        logical_scope_id: str = "pal:resident",
        endpoint_id: str = "",
    ) -> dict[str, Any]:
        prompt_cache = getattr(self.endpoint_invoker, "prompt_cache", None)
        snapshot = getattr(prompt_cache, "confirmed_anchor_request", None)
        active_endpoint = self.active_endpoint()
        resolved_endpoint_id = str(endpoint_id or "").strip() or (
            str(active_endpoint.endpoint_id)
            if active_endpoint is not None
            else ""
        )
        return (
            dict(
                snapshot(
                    logical_scope_id=str(logical_scope_id or "").strip(),
                    endpoint_id=resolved_endpoint_id,
                )
                or {}
            )
            if callable(snapshot)
            else {}
        )

    def prompt_cache_eligible_anchor_request(
        self,
        *,
        logical_scope_id: str = "pal:resident",
        endpoint_id: str = "",
    ) -> dict[str, Any]:
        """R3/S1 (review 95373ef): locally eligible anchor material.

        Local eligibility says the replayed bytes may be used to BUILD a
        same-source request; read evidence only says the provider once
        served them.  Ordinary autocompact uses this reader so an
        unconfirmed-but-locally-valid prefix is tried instead of silently
        switching to another cold source; the confirmed reader remains for
        diagnostics and explicit hot-only surfaces.  No prewarm, no extra
        provider call is issued here.
        """

        prompt_cache = getattr(self.endpoint_invoker, "prompt_cache", None)
        snapshot = getattr(prompt_cache, "eligible_anchor_request", None)
        active_endpoint = self.active_endpoint()
        resolved_endpoint_id = str(endpoint_id or "").strip() or (
            str(active_endpoint.endpoint_id)
            if active_endpoint is not None
            else ""
        )
        return (
            dict(
                snapshot(
                    logical_scope_id=str(logical_scope_id or "").strip(),
                    endpoint_id=resolved_endpoint_id,
                )
                or {}
            )
            if callable(snapshot)
            else {}
        )

    def cache_policy_snapshot(self) -> dict[str, Any]:
        from pal.shared.json_values import thaw_json
        snapshots = {}
        for endpoint in self.endpoint_resolver.endpoints:
            policy = thaw_json((getattr(endpoint, "capabilities_blob", None) or {}).get("prompt_cache") or {})
            hook = self.model_hooks.hooks.get(str(endpoint.model_id))
            explicit = "mode" in policy or "cache_profile" in policy or "dialect" in policy or policy.get("enabled") is False
            if not explicit and hook is not None and hook.cache_profile_ref:
                policy["cache_profile"] = hook.cache_profile_ref
                policy["origin"] = "model_hook"
            else:
                policy["origin"] = "endpoint" if policy else "default"
            policy["generation"] = str(self.cache_profile_generation)
            snapshots[str(endpoint.endpoint_id)] = {
                "model_id": str(endpoint.model_id), "provider": str(endpoint.provider),
                "wire_shape": str(endpoint.wire_shape), "base_url": str(endpoint.base_url or ""), "policy": policy,
            }
        return snapshots

    def _compile_request(
        self,
        endpoint: LLMEndpointModel,
        request: LLMRequestIR,
    ) -> PreparedLLMRequest:
        LLMEndpointSpec.from_value(endpoint)
        endpoint_request = replace(request, model_hint=endpoint.model_id)
        hooked = (
            endpoint_request
            if bool(request.metadata.get("model_hooks_already_applied"))
            else self.model_hooks.apply(endpoint.model_id, endpoint_request)
        )
        from pal.llm.prompt_cache import CacheProfileError, validate_cache_policy
        from pal.llm.shapes.base import ShapeContext
        snapshot = request.metadata.get("cache_policy_snapshot")
        if snapshot is None:
            snapshot = self.cache_policy_snapshot()
        selection = snapshot.get(str(endpoint.endpoint_id))
        if selection is None or any(selection.get(key) != str(getattr(endpoint, key) or "") for key in ("model_id", "provider", "wire_shape", "base_url")):
            raise CacheProfileError("endpoint identity changed after turn cache-policy snapshot")
        policy = dict(selection["policy"])
        validate_cache_policy(ShapeContext(
            wire_shape=WireShape(endpoint.wire_shape), endpoint_id=endpoint.endpoint_id,
            model_id=endpoint.model_id, provider_id=endpoint.provider, base_url=endpoint.base_url,
            capabilities={"prompt_cache": policy},
        ))
        hooked = replace(hooked, metadata={**dict(hooked.metadata), "cache_policy_selection": policy})
        level = hooked.policy.thinking_level
        levels = self._thinking_levels(endpoint)
        budget = hooked.policy.thinking_budget_tokens
        reasoning_context = hooked.policy.reasoning_context
        if reasoning_context is None:
            reasoning_context = (endpoint.capabilities_blob or {}).get("reasoning_context")
        if reasoning_context is not None and endpoint.wire_shape != WireShape.OPENAI_RESPONSE.value:
            raise LLMRequestPreparationError("reasoning_context is only supported by openai_response")
        if hooked.policy.thinking_selection == "lowest_supported":
            level = next(item for item in ThinkingLevel if item.value in levels)
            budget = None
        elif level is None:
            effective = self._effective_thinking_level(endpoint)
            level = ThinkingLevel(effective) if effective else None
        elif level.value not in levels:
            raise LLMRequestPreparationError(
                f"thinking_level={level.value!r} is not declared by endpoint "
                f"{endpoint.endpoint_id}; available={list(levels)}"
            )
        max_output = hooked.policy.max_output_tokens
        if endpoint.max_output_tokens is not None:
            max_output = min(max_output, int(endpoint.max_output_tokens))
        if budget is not None:
            if endpoint.wire_shape != WireShape.ANTHROPIC_MESSAGES.value:
                raise LLMRequestPreparationError(
                    "thinking_budget_tokens is only supported by anthropic_messages"
                )
            if level == ThinkingLevel.OFF or not 1024 <= budget < max_output:
                raise LLMRequestPreparationError(
                    "manual thinking requires a non-off level and "
                    "1024 <= thinking_budget_tokens < effective max_output_tokens"
                )
        policy = replace(
            hooked.policy,
            max_output_tokens=max_output,
            thinking_level=level,
            thinking_budget_tokens=budget,
            reasoning_context=reasoning_context,
        )
        prepared = replace(hooked, policy=policy, model_hint=endpoint.model_id)
        from pal.llm.replay_acceptance import has_opaque_continuation, validate_native_for_send

        for message in prepared.messages:
            replay = message.replay
            if replay is not None and has_opaque_continuation(message) and not replay.matches(
                wire_shape=WireShape(endpoint.wire_shape),
                endpoint_id=endpoint.endpoint_id, model_id=endpoint.model_id,
            ):
                raise LLMRequestPreparationError(
                    "History contains reasoning/replay bound to another model. "
                    "Complete /compact on the previous model before switching, or use /reset."
                )
            if replay is not None and has_opaque_continuation(message):
                validate_native_for_send(message)
        target = self._target_input_budget(endpoint, prepared.policy.max_output_tokens)
        return PreparedLLMRequest(
            endpoint=endpoint,
            request=prepared,
            estimated_input_tokens=_estimate_request_tokens(prepared),
            target_input_budget=target,
        )

    def _prepare_request(
        self,
        endpoint: LLMEndpointModel,
        request: LLMRequestIR,
    ) -> LLMRequestIR:
        return self._compile_request(endpoint, request).request

    def _enabled_endpoints(self, request: LLMRequestIR | None) -> list[LLMEndpointModel]:
        metadata = dict(request.metadata) if request is not None else {}
        endpoints = self._enabled_endpoints_for_preference(
            preferred_endpoint_id=str(metadata.get("preferred_endpoint_id") or "").strip() or None,
            preferred_endpoint_source=str(metadata.get("preferred_endpoint_source") or "").strip() or None,
            endpoint_fallback_policy=str(metadata.get("endpoint_fallback_policy") or "").strip() or None,
        )
        if request is not None and _request_has_image_input(request):
            return [endpoint for endpoint in endpoints if bool(endpoint.supports_vision)]
        return endpoints

    def _enabled_endpoints_for_preference(
        self,
        *,
        preferred_endpoint_id: str | None = None,
        preferred_endpoint_source: str | None = None,
        endpoint_fallback_policy: str | None = None,
    ) -> list[LLMEndpointModel]:
        preferred = str(preferred_endpoint_id or "").strip() or None
        source = str(preferred_endpoint_source or "").strip().lower()
        policy = str(endpoint_fallback_policy or "").strip().lower()
        explicit_policy = bool(policy)
        strict = (
            policy in _FALLBACK_DISABLED_POLICIES
            or bool(preferred and source in _STRICT_ENDPOINT_PREFERRED_SOURCES)
            or (not explicit_policy and not self.llm_endpoint_fallback_enabled())
        )
        if strict:
            selected = preferred or self.active_endpoint_id
            if selected:
                return [
                    endpoint
                    for endpoint in self.endpoint_resolver.endpoints
                    if endpoint.endpoint_id == selected
                ]
            return list(self.endpoint_resolver.endpoints[:1])
        return self.endpoint_resolver.enabled(
            preferred_endpoint_id=preferred,
            fallback_endpoint_id=self.active_endpoint_id,
            include_remaining=True,
        )

    def llm_endpoint_fallback_enabled(self) -> bool:
        """Whether generic requests may fall back to other enabled endpoints.

        Disabled by default: failing the preferred endpoint honestly surfaces
        the failure instead of silently continuing on a different model.
        Explicit per-request ``endpoint_fallback_policy`` metadata overrides
        this global setting in both directions.
        """

        if self._endpoint_fallback_enabled is None:
            getter = getattr(self.settings_repository, "get_llm_endpoint_fallback", None)
            self._endpoint_fallback_enabled = bool(getter()) if callable(getter) else False
        return bool(self._endpoint_fallback_enabled)

    def set_llm_endpoint_fallback(self, enabled: bool) -> dict[str, Any]:
        setter = getattr(self.settings_repository, "set_llm_endpoint_fallback", None)
        if not callable(setter):
            raise LLMEndpointInvocationError(
                "settings repository does not support the endpoint fallback switch"
            )
        setter(bool(enabled))
        self._endpoint_fallback_enabled = bool(enabled)
        return {
            "endpoint_fallback_enabled": bool(enabled),
            "note": (
                "Future requests may fall back to other enabled endpoints."
                if enabled
                else "Future requests fail on the preferred endpoint instead of falling back."
            ),
        }

    def _effective_thinking_level(self, endpoint: LLMEndpointModel | None) -> str | None:
        if endpoint is None:
            return None
        levels = self._thinking_levels(endpoint)
        if not levels:
            return None
        persisted = str(self.settings_repository.get_think_level(endpoint.endpoint_id) or "").strip().lower()
        effective = (
            persisted
            if persisted in levels
            else str(getattr(endpoint, "default_thinking_level", None) or levels[0])
        )
        if effective not in levels:
            effective = levels[0]
        if persisted != effective:
            self.settings_repository.set_think_level(endpoint.endpoint_id, effective)
        return effective

    @staticmethod
    def _thinking_levels(endpoint: LLMEndpointModel) -> tuple[str, ...]:
        return LLMEndpointSpec.from_value(endpoint).thinking_levels_blob

    def _endpoint_by_id(self, endpoint_id: str | None) -> LLMEndpointModel | None:
        normalized = str(endpoint_id or "").strip()
        return next((endpoint for endpoint in self.endpoint_resolver.endpoints if endpoint.endpoint_id == normalized), None)

    def _needs_compaction(self, endpoint: LLMEndpointModel, request: LLMRequestIR) -> bool:
        target = self._target_input_budget(endpoint, request.policy.max_output_tokens)
        return target > 0 and _estimate_request_tokens(request) > target

    def _target_input_budget(self, endpoint: LLMEndpointModel, output_tokens: int) -> int:
        context_window = int(endpoint.context_window or 0)
        if context_window <= 0:
            return 0
        margin = min(self.safety_margin_tokens, max(1024, int(context_window * 0.05)))
        return max(1, context_window - int(output_tokens) - margin)

    def _compact_required_result(self, endpoint: LLMEndpointModel, request: LLMRequestIR) -> LLMGenerationResult:
        response = _text_response("Context compaction is required before LLM invocation.", LLMFinishReason.COMPACT_REQUIRED)
        return LLMGenerationResult(
            response=response,
            target_input_budget=self._target_input_budget(endpoint, request.policy.max_output_tokens),
            reserved_output_tokens=request.policy.max_output_tokens,
            preferred_endpoint_id=endpoint.endpoint_id,
            preferred_model_id=endpoint.model_id,
        )

    def _recover_length(
        self,
        endpoint: LLMEndpointModel,
        request: LLMRequestIR,
        first: LLMResponseIR,
        *,
        allow_discarded_retry: bool = True,
    ) -> LLMResponseIR:
        if first.finish_reason != LLMFinishReason.LENGTH:
            return first
        # A provider item boundary is a durable semantic commit.  Do not ask
        # the model to regenerate a closed tool call: return it to the normal
        # agent loop, which remains the sole execution path.
        if has_committed_tool_calls(first):
            return first
        default_attempts = max(
            0,
            int(
                getattr(
                    self.config,
                    "llm_max_output_recovery_attempts",
                    3,
                )
                or 0
            ),
        )
        settings = recovery_settings(
            endpoint,
            request,
            default_attempts=default_attempts,
        )
        if not settings.enabled:
            return safe_truncated_response(first)

        discarded: tuple[LLMResponseIR, ...] = ()
        responses: list[LLMResponseIR] = []
        current_request = request
        current = first
        if (
            allow_discarded_retry
            and request.policy.max_output_tokens < settings.upper_limit
        ):
            escalated = with_recovery_stage(
                request,
                stage="escalate",
                attempt=0,
                max_output_tokens=settings.upper_limit,
            )
            if not self._needs_compaction(endpoint, escalated):
                self._emit(
                    "llm_output_limit_recovery_started",
                    endpoint=endpoint,
                    stage="escalate",
                    attempt=0,
                    max_output_tokens=settings.upper_limit,
                )
                discarded = (first,)
                current_request = escalated
                current, _ = self._invoker().invoke(
                    endpoint,
                    current_request,
                    stream=False,
                    timeout_seconds=self._timeout_seconds(current_request),
                )
                if has_committed_tool_calls(current):
                    return merge_responses([current], discarded=discarded)
                if current.finish_reason != LLMFinishReason.LENGTH:
                    recovered = merge_responses([current], discarded=discarded)
                    self._emit(
                        "llm_output_limit_recovery_succeeded",
                        endpoint=endpoint,
                        stage="escalate",
                        attempt=0,
                        max_output_tokens=settings.upper_limit,
                    )
                    return recovered

        responses.append(safe_truncated_response(current))
        for attempt in range(1, settings.max_continuations + 1):
            candidate = continuation_request(
                current_request,
                current,
                max_output_tokens=settings.upper_limit,
                attempt=attempt,
            )
            if self._needs_compaction(endpoint, candidate):
                break
            self._emit(
                "llm_output_limit_recovery_started",
                endpoint=endpoint,
                stage="continue",
                attempt=attempt,
                max_output_tokens=settings.upper_limit,
            )
            current_request = candidate
            current, _ = self._invoker().invoke(
                endpoint,
                current_request,
                stream=False,
                timeout_seconds=self._timeout_seconds(current_request),
            )
            if has_committed_tool_calls(current):
                return merge_responses(
                    [*responses, current],
                    discarded=discarded,
                )
            if current.finish_reason != LLMFinishReason.LENGTH:
                recovered = merge_responses(
                    [*responses, current],
                    discarded=discarded,
                )
                self._emit(
                    "llm_output_limit_recovery_succeeded",
                    endpoint=endpoint,
                    stage="continue",
                    attempt=attempt,
                    max_output_tokens=settings.upper_limit,
                )
                return recovered
            responses.append(safe_truncated_response(current))

        exhausted = merge_responses(responses, discarded=discarded)
        self._emit(
            "llm_output_limit_recovery_exhausted",
            endpoint=endpoint,
            stage="continue",
            attempt=max(0, len(responses) - 1),
            max_output_tokens=settings.upper_limit,
        )
        return exhausted

    def _normalize_completed_response(
        self,
        endpoint: LLMEndpointModel,
        request: LLMRequestIR,
        response: LLMResponseIR,
    ) -> LLMResponseIR:
        updates = tuple(
            self.provider_response_hooks.normalize(
                endpoint_id=str(endpoint.endpoint_id),
                provider_id=str(endpoint.provider),
                model_id=str(endpoint.model_id),
                wire_shape=WireShape(str(endpoint.wire_shape)),
                request=request,
                updates=(
                    LLMResponseUpdate(
                        response=response,
                        delta_kind=LLMResponseDeltaKind.STATE,
                    ),
                ),
            )
        )
        if not updates:
            raise ProviderResponseHookError(
                f"provider response hook produced no output for {endpoint.endpoint_id}"
            )
        normalized = updates[-1].response
        if response.message.replay is not None and normalized.message.replay is None:
            if normalized.message.tool_calls != response.message.tool_calls:
                raise ProviderResponseHookError("recovery changed accepted tool inventory")
            normalized = replace(normalized, message=replace(
                normalized.message, replay=response.message.replay,
            ))
        return normalized

    def _success(
        self,
        endpoint: LLMEndpointModel,
        response: LLMResponseIR,
        *,
        projection_receipt: ProjectionSendReceipt | None = None,
    ) -> LLMGenerationResult:
        self.last_endpoint_id = endpoint.endpoint_id
        self.last_model_id = endpoint.model_id
        self._record_success(endpoint, response)
        return LLMGenerationResult(
            response=response,
            preferred_endpoint_id=endpoint.endpoint_id,
            preferred_model_id=endpoint.model_id,
            projection_receipt=projection_receipt,
        )

    def _record_success(self, endpoint: LLMEndpointModel, response: LLMResponseIR) -> None:
        if response.finish_reason == LLMFinishReason.ERROR:
            raise LLMEndpointResponseError(
                f"endpoint {endpoint.endpoint_id} returned finish_reason=error"
            )
        self.usage_ledger.record_success(
            endpoint_id=endpoint.endpoint_id,
            model_id=endpoint.model_id,
            provider=endpoint.provider,
            usage=response.usage,
            provider_response_count=response.provider_response_count,
            usage_accounted=bool(response.attempt_ids) and isinstance(self._invoker(), ShapeEndpointInvoker),
        )

    def _record_failure(self, endpoint: LLMEndpointModel, exc: Exception, attempt: int, *, provider_attempt: bool = True) -> str:
        error_kind = _classify_retry_error(exc)
        if provider_attempt and not getattr(exc, "llm_attempt_recorded", False):
            self.usage_ledger.record_failed_attempt(
                endpoint_id=endpoint.endpoint_id,
                model_id=endpoint.model_id,
                provider=endpoint.provider,
            )
        self._emit(
            "llm_endpoint_attempt_failed",
            endpoint=endpoint,
            attempt=attempt + 1,
            error_kind=error_kind,
            error_type=type(exc).__name__,
        )
        return error_kind

    def _timeout_seconds(self, request: LLMRequestIR) -> float:
        value = request.metadata.get("timeout_seconds")
        if value is None:
            purpose = str(request.metadata.get("purpose") or "").lower()
            setting = "llm_compaction_timeout_seconds" if "compact" in purpose else "llm_request_timeout_seconds"
            value = getattr(self.config, setting, _DEFAULT_TIMEOUT_SECONDS)
        try:
            return max(1.0, float(value))
        except (TypeError, ValueError):
            return _DEFAULT_TIMEOUT_SECONDS

    def _stream_wall_timeout_seconds(self, request: LLMRequestIR) -> float:
        value = request.metadata.get("stream_wall_timeout_seconds")
        if value is None:
            value = getattr(
                self.config,
                "llm_stream_wall_timeout_seconds",
                1_800.0,
            )
        try:
            return max(1.0, float(value))
        except (TypeError, ValueError):
            return 1_800.0

    def _stream_cleanup_timeout_seconds(self) -> float:
        value = getattr(
            self.config,
            "llm_stream_cleanup_timeout_seconds",
            2.0,
        )
        try:
            return max(0.01, float(value))
        except (TypeError, ValueError):
            return 2.0

    def _emit(self, phase: str, *, endpoint: LLMEndpointModel, **payload: Any) -> None:
        event = {"phase": phase, "endpoint_id": endpoint.endpoint_id, "model_id": endpoint.model_id, **payload}
        if callable(self.event_sink):
            self.event_sink(dict(event))

    def _invoker(self) -> LLMEndpointInvokerPort:
        if self.endpoint_invoker is None:
            raise LLMEndpointInvocationError("LLM endpoint invoker is not configured")
        return self.endpoint_invoker


def _text_response(
    text: str,
    reason: LLMFinishReason,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> LLMResponseIR:
    return LLMResponseIR(
        message=LLMMessageIR(
            role=MessageRole.ASSISTANT,
            parts=(TextPartIR(str(text)),) if str(text) else (),
            state=MessageState.COMPLETE,
            metadata=dict(metadata or {}),
        ),
        finish_reason=reason,
        provider_response_count=0 if reason in {LLMFinishReason.ERROR, LLMFinishReason.COMPACT_REQUIRED} else 1,
    )


def _failure_result(
    text: str,
    *,
    exc: Exception | None = None,
) -> LLMGenerationResult:
    return LLMGenerationResult(
        response=_text_response(
            text,
            LLMFinishReason.ERROR,
            metadata=_failure_metadata(exc),
        )
    )


def _response_with_failure(
    response: LLMResponseIR,
    exc: Exception,
    *,
    partial_output_chars: int = 0,
) -> LLMResponseIR:
    metadata = dict(response.message.metadata)
    metadata.update(_failure_metadata(exc))
    if partial_output_chars > 0:
        metadata["partial_output_chars"] = int(partial_output_chars)
    return replace(
        response,
        message=replace(response.message, metadata=metadata),
        finish_reason=LLMFinishReason.ERROR,
    )


def _failure_metadata(exc: Exception | None) -> dict[str, str]:
    error_kind = _classify_retry_error(exc) if exc is not None else "unknown"
    return {
        "failure_subsystem": (
            "persistence" if error_kind == "local_state" else "llm"
        ),
        "failure_kind": error_kind,
        "error_type": type(exc).__name__ if exc is not None else "UnknownError",
    }


def _classify_retry_error(exc: Exception) -> str:
    if any(isinstance(item, sqlite3.DatabaseError) for item in _exception_chain(exc)):
        return "local_state"
    if isinstance(exc, LLMEndpointSpecStaleError):
        return "endpoint_spec_stale"
    if isinstance(exc, LLMProviderStartedError):
        return "provider_started"
    if isinstance(exc, LLMCredentialUnavailableError):
        return "credential"
    if isinstance(exc, LLMRequestPreparationError):
        return "request"
    if isinstance(exc, LLMEndpointResponseError):
        return "response_error"
    if isinstance(exc, ProviderResponseHookError):
        return "response_error"
    message = str(exc).lower()
    error_type = type(exc).__name__.lower()
    if any(
        marker in message
        for marker in (
            "error code: 401",
            "status code: 401",
            "http 401",
            "unauthorized",
            "invalid api key",
            "invalid_api_key",
            "error code: 403",
            "status code: 403",
            "http 403",
            "forbidden",
        )
    ):
        return "credential"
    if "timeout" in message or "timed out" in message or "timeout" in error_type:
        return "timeout"
    if any(marker in message for marker in ("error code: 400", "status code: 400", "bad request")):
        return "bad_request"
    if any(marker in message for marker in ("connection refused", "connection reset", "broken pipe")) or "connection" in error_type:
        return "connection"
    if "429" in message or "rate limit" in message:
        return "rate_limit"
    if any(marker in message for marker in ("500", "502", "503", "504", "529", "overload")):
        return "server"
    return "unknown"


def _exception_chain(exc: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _retryable_error_kind(error_kind: str) -> bool:
    return error_kind in {
        "timeout",
        "connection",
        "rate_limit",
        "server",
        "response_error",
        "unknown",
    }


def _public_failure_text(exc: Exception | None) -> str:
    if exc is None:
        return "LLM invocation failed: kind=unknown type=UnknownError"
    return (
        "LLM invocation failed: "
        f"kind={_classify_retry_error(exc)} type={type(exc).__name__}"
    )


def _estimate_request_tokens(request: LLMRequestIR) -> int:
    chars = 0
    for message in request.messages:
        if message.replay is not None:
            # The continuation replaces the semantic representation on the
            # wire. Count it once; source_payload is local provenance only.
            chars += len(json.dumps(thaw_json(message.replay.payload), ensure_ascii=False))
            continue
        chars += len(message.text) + len(message.reasoning_text)
        for call in message.tool_calls:
            chars += len(call.name) + len(json.dumps(thaw_json(call.arguments), ensure_ascii=False))
    for tool in request.tools:
        chars += len(tool.name) + len(tool.description) + len(json.dumps(thaw_json(tool.input_schema), ensure_ascii=False))
    return max(1, (chars + 3) // 4)


def _request_has_image_input(request: LLMRequestIR) -> bool:
    return any(
        isinstance(part, ImagePartIR)
        for message in request.messages
        for part in message.parts
    )


def _retry_delay(attempt: int) -> float:
    base = min(0.5 * (2 ** max(0, attempt - 1)), 32.0)
    return base + random.random() * base * 0.25


def _is_stub_endpoint(endpoint: LLMEndpointModel) -> bool:
    capabilities = dict(getattr(endpoint, "capabilities_blob", None) or {})
    return bool(
        capabilities.get("stub")
        or str(endpoint.base_url).startswith("stub://")
    )


def _accounted_response_error(endpoint: Any, response: LLMResponseIR) -> LLMEndpointResponseError:
    error = LLMEndpointResponseError(f"endpoint {endpoint.endpoint_id} returned finish_reason=error")
    error.llm_attempt_recorded = bool(response.attempt_ids)
    return error
