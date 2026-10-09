"""Redact failure presentation without rewriting executable or success data."""
from __future__ import annotations

from pathlib import Path
import json

import pytest

from pal.execution.contracts import CapabilityResult, ToolCallBudget
from pal.execution.tool_facade import (
    EffectOutcome, EffectReceipt, FailedResult, RejectedResult, RetryDirective,
    ToolExecutionError, ToolHandlerResult,
)
from pal.shared import RuntimeStatus
from pal.shared.diagnostics import diagnostic_text
from pal.shared.tool_protocol import ToolAffordance, new_tool_call
from tests.test_tool_failure_affordances import runtime, invoke, metadata
from tests.test_tool_result_fidelity import mount


CANARY = "SYNTHETIC_FAILURE_DELIVERY_CANARY_917"


def test_nested_diagnostic_redaction_preserves_evidence_after_newline(runtime):
    details = {"nested": [{"message": f'token={CANARY}\nRetain this cause',
                           "quoted": f'api_key="{CANARY} SECRET_TAIL"; retain this action'}]}
    raw = FailedResult(error_code="provider_failed", error="provider failed", llm_text="provider failed",
        details=details, effect=EffectOutcome.NONE, retry=RetryDirective.SAFE)
    mount(runtime, "nested_diagnostic", lambda _: raw)
    result = invoke(runtime, "nested_diagnostic", {})
    assert "Retain this cause" in result.llm_text
    assert "retain this action" in result.llm_text
    assert "SECRET_TAIL" not in result.llm_text
    assert CANARY not in result.llm_text
    assert raw.details == details


@pytest.mark.parametrize("payload", [
    {"message": f'token={CANARY}\nRetain this cause'},
    {"message": f'api_key="{CANARY} SECRET_TAIL"; retain this action'},
    {"api_key": f'{CANARY} "SECRET_TAIL"', "cause": "Retain this cause"},
])
def test_encoded_diagnostics_redact_whole_credentials_without_eating_evidence(payload):
    encoded = json.dumps(payload)
    for text in (encoded, "Failure details: " + encoded):
        redacted = diagnostic_text(text, limit=None)
        assert CANARY not in redacted
        assert "SECRET_TAIL" not in redacted
        assert ("Retain this cause" if "retain this action" not in text else "retain this action") in redacted
        assert diagnostic_text(redacted, limit=None) == redacted


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_failure_guidance_is_redacted_in_final_metadata(runtime, direct, asynchronous):
    arguments = {"file_path": "/tmp/diagnostic-source.py"}

    def fail(_):
        raise ToolExecutionError(
            "backend unavailable", error_code="backend_unavailable",
            effect_receipt=EffectReceipt(outcome=EffectOutcome.NOT_APPLIED),
            retry=RetryDirective.DO_NOT_RETRY,
            recovery_hint=f"Inspect token={CANARY}; keep the source file.",
            affordances=[ToolAffordance(tool="read_file", arguments=arguments,
                reason=f"Inspect the configuration; password={CANARY}")],
        )

    mount(runtime, "guidance_redaction", fail, direct=direct)
    result = invoke(runtime, "guidance_redaction", {}, asynchronous=asynchronous)
    info = metadata(result)
    assert CANARY not in result.llm_text
    assert "token=[redacted]" in info["recovery"]
    assert "keep the source file" in info["recovery"]
    assert info["affordances"][0]["reason"].endswith("password=[redacted]")
    assert info["affordances"][0]["arguments"] == arguments
    assert (info["error_code"], info["effect"], info["retry"]) == (
        "backend_unavailable", "not_applied", "do_not_retry")


@pytest.mark.parametrize("direct", [False, True])
def test_unvalidated_output_diagnostics_redact_nested_credentials(runtime, direct):
    candidate = {"nested": [{"token": CANARY, "password": CANARY,
        "api_key": CANARY, "source": "src/pal/execution/runtime.py",
        "endpoint": "https://example.invalid/v1", "stage": "decode response"}]}
    mount(runtime, "output_redaction", lambda _: CapabilityResult(
        status=RuntimeStatus.OK, text="provider summary", llm_text="provider summary",
        structured=candidate, effect_receipt=EffectReceipt(outcome=EffectOutcome.NONE)),
        direct=direct)
    result = invoke(runtime, "output_redaction", {})
    assert not result.ok
    assert metadata(result)["error_code"] == "output_validation_failed"
    assert CANARY not in result.llm_text
    assert "provider summary" in result.llm_text
    assert "src/pal/execution/runtime.py" in result.llm_text
    assert "https://example.invalid/v1" in result.llm_text
    assert "decode response" in result.llm_text
    # Host evidence is distinct from its redacted model-facing projection.
    assert CANARY in result.invocation_result.details["raw_output_text"]
    assert candidate["nested"][0]["token"] == CANARY


@pytest.mark.parametrize("rejected", [False, True])
def test_typed_failure_body_is_redacted_at_the_same_exit(runtime, rejected):
    cls = RejectedResult if rejected else FailedResult
    raw = cls(error_code="typed_failure", error="typed failure",
        llm_text=f"Provider diagnostic token={CANARY}; inspect the adapter.",
        retry=RetryDirective.CORRECT_INPUT,
        **({} if rejected else {"effect": EffectOutcome.UNKNOWN}))
    mount(runtime, "typed_redaction", lambda _: raw)
    result = invoke(runtime, "typed_redaction", {})
    assert CANARY not in result.llm_text
    assert "inspect the adapter" in result.llm_text
    assert metadata(result)["kind"] == ("rejected" if rejected else "failed")
    assert CANARY in raw.llm_text  # Do not mutate the handler-owned result.


def test_full_failure_snapshot_and_long_recovery_are_redacted(runtime):
    def fail(_):
        raise ToolExecutionError(
            f"Original diagnostic token={CANARY}; " + "evidence " * 1000,
            recovery_hint="Inspect the configuration. " * 80 + f"password={CANARY}; keep the backup.",
        )
    mount(runtime, "snapshot_redaction", fail)
    result = runtime.execute_tool(new_tool_call(name="snapshot_redaction", args={}),
        turn_id="review", budget=ToolCallBudget(max_output_chars=1200, preview_chars=300))
    assert result.snapshot_refs
    assert CANARY not in result.llm_text
    snapshots = "\n".join(Path(ref.path).read_text() for ref in result.snapshot_refs)
    assert CANARY not in snapshots
    assert "Original diagnostic" in snapshots
    assert "keep the backup" in snapshots
    assert "password=[redacted]" in snapshots


def test_typed_error_stays_redacted_in_the_failure_prompt(runtime):
    from pal.core.turns import _render_failure_primary_input
    from pal.failure import FailureSignal
    from pal.failure.runtime import FailureRuntime

    raw = FailedResult(error_code="handler_exception", error=f"token={CANARY}; backend unavailable",
        llm_text="safe summary", effect=EffectOutcome.UNKNOWN,
        retry=RetryDirective.RECONCILE_FIRST)
    mount(runtime, "failure_prompt_redaction", lambda _: raw)
    result = invoke(runtime, "failure_prompt_redaction", {})
    draft = FailureRuntime().begin_draft(FailureSignal(
        subsystem="execution", component="failure_prompt_redaction", failure_kind="capability_failure",
        severity="medium", primary_blocker=result.text, evidence={"tool_result": result.structured}))
    prompt = _render_failure_primary_input(draft, stage="diagnose", allowed_tools=[], observations=[])
    assert CANARY not in prompt
    assert "backend unavailable" in prompt
    assert CANARY in raw.error  # The provider-owned original remains intact.


def test_success_business_output_is_not_rewritten(runtime):
    source = f"token={CANARY}\npassword={CANARY}\n"
    mount(runtime, "success_redaction_control", lambda _: ToolHandlerResult(
        output={}, llm_text=source, effect_receipt=EffectReceipt(outcome=EffectOutcome.NONE)))
    result = invoke(runtime, "success_redaction_control", {})
    assert result.ok
    assert result.llm_text == source


@pytest.mark.parametrize("text", [
    "token=value", "password='a value'", '{"nested":{"api_key":"value"}}',
    "Bearer credential", "https://user:password@example.invalid/path",
])
def test_repeated_diagnostic_redaction_is_idempotent(text):
    once = diagnostic_text(text, limit=None)
    assert diagnostic_text(once, limit=None) == once


@pytest.mark.parametrize("header", [
    "Authorization", "aUtHoRiZaTiOn", "Proxy-Authorization",
    "proxy_authorization", "ProxyAuthorization",
])
@pytest.mark.parametrize("scheme", ["Basic", "Bearer", "CustomScheme"])
def test_nested_authorization_is_redacted_in_every_failure_prompt_source(header, scheme):
    from copy import deepcopy
    from pal.core.turns import ToolObservation, _render_failure_primary_input
    from pal.failure import FailureDraft

    evidence = {"request": {"headers": [{header: f"{scheme} {CANARY}",
        "Content-Type": "application/json", "X-Request-ID": "request-17"}]},
        "status": 401, "retry": False, "attempts": 0,
        "authorization_required": True, "authorization_url": "https://example.invalid/auth"}
    original = deepcopy(evidence)
    draft = FailureDraft(subsystem="execution", component="request", failure_kind="capability_failure",
        severity="medium", primary_blocker="request failed", evidence=evidence,
        maintenance_outcomes=[{"action_name": "probe", "status": "error", "ok": False,
                              "text": "probe failed", "structured": evidence}])
    observation = ToolObservation(tool_name="probe", ok=False, summary="request failed", structured=evidence)
    rendered = _render_failure_primary_input(draft, stage="diagnose", allowed_tools=[],
                                            observations=[observation])
    assert CANARY not in rendered
    payload = json.loads(rendered)
    expected = deepcopy(evidence)
    expected["request"]["headers"][0][header] = "[redacted]"
    assert payload["failure"]["evidence"] == expected
    assert payload["failure"]["maintenance_outcomes"][0]["structured_summary"] == expected
    assert payload["recent_observations"][0]["structured_summary"] == expected
    assert evidence == original


@pytest.mark.parametrize("scheme", ["Basic", "Bearer", "CustomScheme"])
def test_nested_authorization_is_redacted_in_tool_delivery_and_snapshot(runtime, scheme):
    details = {"responses": [{"request": {"headers": {
        "Authorization": f"{scheme} {CANARY}",
        "Proxy-Authorization": f"{scheme} {CANARY}", "Accept": "application/json"}},
        "status": 401, "retry": False, "attempts": 0}], "evidence": "retain evidence " * 1000}
    raw = FailedResult(error_code="provider_failed", error="request failed", llm_text="request failed",
        details=details, effect=EffectOutcome.NONE, retry=RetryDirective.SAFE)
    mount(runtime, "header_redaction", lambda _: raw)
    result = invoke(runtime, "header_redaction", {})
    assert CANARY not in result.llm_text
    assert result.snapshot_refs
    snapshots = "\n".join(Path(ref.path).read_text() for ref in result.snapshot_refs)
    assert CANARY not in snapshots
    assert "application/json" in snapshots and "retain evidence" in snapshots
    assert raw.details == details
    assert raw.details["responses"][0]["request"]["headers"]["Authorization"] == f"{scheme} {CANARY}"
    assert (metadata(result)["error_code"], metadata(result)["effect"], metadata(result)["retry"]) == (
        "provider_failed", "none", "safe")


@pytest.mark.parametrize("summarize", [False, True])
def test_shared_header_redaction_supports_mappings_and_tuple_diagnostics(summarize):
    from types import MappingProxyType
    from pal.shared.diagnostics import diagnostic_value

    headers = MappingProxyType({"AUTHORIZATION": f"Basic {CANARY}",
                                "PROXY_AUTHORIZATION": {"credentials": CANARY},
                                "WWW-Authenticate": "Basic realm=example", "Content-Length": 0})
    value = MappingProxyType({"attempts": ({"headers": headers, "ok": False},),
                              "authorization_status": "denied", "body": None})
    expected = {"attempts": [{"headers": {"AUTHORIZATION": "[redacted]",
        "PROXY_AUTHORIZATION": "[redacted]", "WWW-Authenticate": "Basic realm=example",
        "Content-Length": 0}, "ok": False}], "authorization_status": "denied", "body": None}
    assert diagnostic_value(value, summarize=summarize) == expected
    assert diagnostic_value(expected, summarize=summarize) == expected
    assert headers["AUTHORIZATION"] == f"Basic {CANARY}"
