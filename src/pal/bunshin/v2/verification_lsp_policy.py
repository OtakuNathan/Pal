"""Manager-compiled LSP applicability, shared by local and receipt gates."""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from pal.bunshin.v2.submission_preflight import bound_reference_payload


def compile_lsp_applicability(
    policy: Mapping[str, Any],
    workspace_preparation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Consume a Manager-owned preparation, never a role's availability claim.

    Only an explicit unavailable observation can discharge ``when_available``.
    Missing, partial, or unrecognized preparation stays fail-closed. The
    result is bound in the immutable verification policy, so later runtime
    observations cannot reinterpret a submitted verdict or its assignment.
    """
    preparation = dict(workspace_preparation or {}).get("lsp_workspace_preparation")
    status = str(preparation.get("status") or "") if isinstance(preparation, Mapping) else ""
    availability = {"ok": "available", "unavailable": "unavailable"}.get(status, "unknown")
    mode = str(policy.get("lsp_policy") or "")
    explicit = bool(policy.get("require_lsp", False)) or mode == "required"
    required = explicit or mode not in {"", "never"} and not (
        mode == "when_available" and availability == "unavailable"
    )
    return {
        "source": "manager_workspace_preparation.v1",
        "availability": availability,
        "required": required,
    }


def lsp_evidence_required(policy: Mapping[str, Any]) -> bool:
    """Legacy/missing decisions stay strict; explicit requirements win."""
    mode = str(policy.get("lsp_policy") or "")
    if bool(policy.get("require_lsp", False)) or mode == "required":
        return True
    if mode in {"", "never"}:
        return False
    decision = policy.get("lsp_applicability")
    if mode == "when_available" and isinstance(decision, Mapping):
        if (decision.get("source") == "manager_workspace_preparation.v1"
                and decision.get("availability") == "unavailable"
                and decision.get("required") is False):
            return False
    return True


def lsp_policy_errors(
    policy: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    exceptions: Mapping[str, Any] | None = None,
) -> list[str]:
    tags = {str(tag) for case in cases for tag in list(case.get("obligation_tags") or [])}
    if (lsp_evidence_required(policy) and "lsp" not in tags
            and not str(dict(exceptions or {}).get("lsp") or "").strip()):
        return ["VerificationPolicy requires LSP evidence or an explicit UNKNOWN reason"]
    return []


def bound_verification_policy(workspace: Mapping[str, Any]) -> dict[str, Any]:
    """The authenticated pack carries the same policy as the bound artifact.

    Manager validation cannot read a worker's /pal mount. Its authenticated
    prompt pack supplies this immutable contract, not the submitted payload.
    Older/test workspaces may instead expose the bound reference directly.
    """
    binding = dict(workspace.get("bunshin_v2") or {})
    contract = dict(binding.get("verification_tool_contract") or {})
    policy = contract.get("verification_policy")
    if isinstance(policy, Mapping):
        return dict(policy)
    return bound_reference_payload(workspace, "verification_policy", required=False)
