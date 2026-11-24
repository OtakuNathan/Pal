"""Manager-preseeded paper verification report (verification_report.yaml).

Shared by corpus hashing, preflight validation and report settlement.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
import yaml

from pal.bunshin.input_binding import _read_bytes_beneath, _write_bytes_atomically_beneath
from pal.bunshin.verification import historical_repair_checklist_items
from pal.bunshin.verification_lsp_policy import lsp_evidence_required

VERIFICATION_REPORT_FILENAME = "verification_report.yaml"
VALID_REPORT_STATUSES = frozenset({"pass", "fail", "not_applicable", "unknown"})


class _StrictReportLoader(yaml.SafeLoader):
    """YAML SafeLoader that rejects duplicate mapping keys and aliases."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ValueError("verification_report.yaml cannot contain YAML aliases")
        return super().compose_node(parent, index)


def _construct_strict_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ValueError("verification_report.yaml mapping keys must be strings")
        if key in mapping:
            raise ValueError(f"verification_report.yaml contains duplicate key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictReportLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_strict_mapping,
)
_StrictReportLoader.add_constructor("tag:yaml.org,2002:timestamp", yaml.SafeLoader.construct_scalar)


def _report_location(workspace: Mapping[str, Any]) -> tuple[Path, PurePosixPath, str] | None:
    """Derive the path from Manager-owned workspace roots and scopes."""
    repo = workspace.get("repo_path")
    if repo and not workspace.get("verification_scratch_only"):
        for scope in workspace.get("write_path_scopes") or ():
            if scope.get("kind") != "directory":
                continue
            directory = PurePosixPath(scope["path"])
            if not directory.parts or directory.is_absolute() or ".." in directory.parts:
                raise ValueError("verification report scope escapes the workspace")
            relative = directory / VERIFICATION_REPORT_FILENAME
            return Path(repo), relative, relative.as_posix()
    scratch = workspace.get("review_scratch_dir")
    if scratch:
        return Path(scratch), PurePosixPath(VERIFICATION_REPORT_FILENAME), f"review_scratch/{VERIFICATION_REPORT_FILENAME}"
    return None


def _policy_focus_hints(
    verification_policy: Mapping[str, Any] | None,
    system_delivery_view: Mapping[str, Any] | None,
) -> list[str]:
    policy = dict(verification_policy or {})
    hints: list[str] = []
    if bool(policy.get("require_warning_clean")):
        hints.append("warning-clean build/checks")
    if bool(policy.get("require_consumer_probe")):
        hints.append("positive and negative public-API consumer/compile probes")
    if bool(policy.get("require_public_surface_dogfood")):
        hints.append("real public-surface dogfood / end-to-end execution")
    if bool(policy.get("require_platform_probe")):
        hints.append("platform/environment probe")
    lsp_mode = str(policy.get("lsp_policy") or "").strip().lower()
    if bool(policy.get("require_lsp")) or lsp_mode in {"required", "when_available"}:
        hints.append("LSP diagnostics when language server is available")
    if system_delivery_view:
        scenarios = dict(system_delivery_view.get("scenarios") or {})
        if scenarios:
            hints.append(
                "system scenarios: " + ", ".join(sorted(str(name) for name in scenarios))
            )
    return hints


def _focus_areas(
    work_view: Mapping[str, Any], policy: Mapping[str, Any], delivery: Mapping[str, Any] | None,
) -> list[str]:
    names = ["regressions", "candidate_delta", "adversarial_checks"]
    history = historical_repair_checklist_items(work_view)
    if history:
        names.extend(f"historical_regression/{item['case']}" for item in history)
    elif work_view.get("historical_repair_bill_refs"):
        names.append("historical_regressions")
    scenarios = (delivery or {}).get("scenarios") or {}
    if scenarios:
        names.extend(f"system_delivery/{scenario}" for scenario in sorted(scenarios))
    elif work_view.get("graph_sink") or delivery:
        names.append("system_delivery")
    for name in ("warning_clean", "consumer_probe", "public_surface_dogfood", "platform_probe"):
        if policy.get(f"require_{name}"):
            names.append(name)
    if lsp_evidence_required(policy):
        names.append("lsp")
    return names


def render_verification_report_template(
    *,
    work_view: Mapping[str, Any],
    verification_policy: Mapping[str, Any] | None = None,
    system_delivery_view: Mapping[str, Any] | None = None,
    candidate_digest: str = "",
) -> str:
    """Render a compact, comment-guided verification_report.yaml template."""
    module_name = str(
        work_view.get("module_name")
        or work_view.get("verification_name")
        or "module"
    ).strip()
    focus_hints = _policy_focus_hints(verification_policy, system_delivery_view)

    lines = [
        "# Manager-preseeded verification report for this module Candidate.",
        "# Run tests and checks using ordinary file, shell, and LSP tools, then fill out",
        "# this report before calling your semantic verification outcome tool.",
        "# Record every blocking defect or p2 advisory with update_finding so Manager",
        "# can route structured RepairBills to Coder.",
        "#",
        "# Allowed focus_areas.<name>.status values: pass | fail | not_applicable | unknown",
        "# For PASS, required areas need passing evidence. Explain any inapplicable adversarial checks in notes.",
        "# Rerun unresolved failed shell checks. Explain failed LSP checks in focus_areas.lsp.notes.",
    ]
    if focus_hints:
        lines.append("# Suggested focus points for this scope: " + "; ".join(focus_hints))

    focus_areas = {
        name: {"status": "", "commands": [], "notes": ""}
        for name in _focus_areas(work_view, verification_policy or {}, system_delivery_view)
    }
    lines.append("# For pass, list the exact successful shell commands or tool call IDs in commands.")

    payload: dict[str, Any] = {
        "schema_version": "1",
        "module": module_name,
        **({"candidate_digest": str(candidate_digest).strip()} if str(candidate_digest).strip() else {}),
        "verdict_summary": "",
        "focus_areas": focus_areas,
        "extra_checks": [],
    }
    body = yaml.safe_dump(
        payload,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    return "\n".join(lines) + "\n" + body


def bind_verification_report_file(
    workspace: Mapping[str, Any],
    *,
    work_view: Mapping[str, Any],
    verification_policy: Mapping[str, Any] | None = None,
    system_delivery_view: Mapping[str, Any] | None = None,
    candidate_digest: str = "",
) -> dict[str, Any]:
    """Ensure the pre-seeded verification_report.yaml exists and bind its path."""
    result = dict(workspace)
    location = _report_location(result)
    if location is None:
        return result
    root, relative, rel_path = location
    if result.get("verification_scratch_only"):
        # Pin the parent at bind time (e.g. macOS /var -> /private/var).
        # Keep the scratch directory itself subject to the no-follow check.
        root = root.parent.resolve() / root.name
        result["review_scratch_dir"] = str(root)
    path = root / relative
    digest = str(candidate_digest or "").strip()
    refresh = not path.exists()
    if not refresh:
        # Refuse links and non-regular files even when this is a same-Candidate retry.
        data = _read_bytes_beneath(root, relative)
        if digest:
            try:
                existing = _parse_report(data.decode("utf-8"))
            except ValueError:
                existing = {}
            refresh = existing.get("candidate_digest") != digest
    if refresh:
        template = render_verification_report_template(
            work_view=work_view, verification_policy=verification_policy,
            system_delivery_view=system_delivery_view, candidate_digest=digest,
        )
        _write_bytes_atomically_beneath(root, relative, template.encode("utf-8"))
    result.update(
        verification_report_path=str(path), verification_report_relpath=rel_path,
        verification_report_candidate_digest=digest,
        verification_report_focus_areas=_focus_areas(work_view, verification_policy or {}, system_delivery_view),
    )
    return result


def read_verification_report_yaml(path: Path) -> dict[str, Any]:
    return _parse_report(_read_bytes_beneath(path.parent, PurePosixPath(path.name)).decode("utf-8"))


def _parse_report(raw_text: str) -> dict[str, Any]:
    try:
        loaded = yaml.load(raw_text, Loader=_StrictReportLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"verification_report.yaml is not valid YAML: {exc}") from exc
    if not isinstance(loaded, Mapping):
        raise ValueError("verification_report.yaml must be a YAML mapping")
    # The accepted artifact stores this mapping as JSON alongside the original YAML.
    try:
        json.dumps(loaded, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("verification_report.yaml values must be JSON-compatible") from exc
    return dict(loaded)


def validate_verification_report(
    report: Mapping[str, Any],
    *,
    outcome: str,
    expected_candidate_digest: str = "",
) -> list[str]:
    """Validate the filled-out paper verification report against the outcome."""
    errors: list[str] = []
    if not isinstance(report, Mapping):
        return ["verification_report.yaml must be a YAML mapping"]

    summary = str(report.get("verdict_summary") or "").strip()
    if not summary:
        errors.append(
            "verification_report.yaml requires a non-empty verdict_summary"
        )

    expected_digest = str(expected_candidate_digest or "").strip()
    reported_digest = str(report.get("candidate_digest") or "").strip()
    if expected_digest and reported_digest != expected_digest:
        errors.append(
            "verification_report.yaml candidate_digest does not match the current Candidate"
        )

    focus_areas = report.get("focus_areas")
    if not isinstance(focus_areas, Mapping) or not focus_areas:
        errors.append(
            "verification_report.yaml requires a non-empty focus_areas mapping"
        )
        return errors

    for raw_name, raw_entry in focus_areas.items():
        name = str(raw_name or "").strip() or "<unnamed>"
        if not isinstance(raw_entry, Mapping):
            errors.append(f"verification_report.yaml focus_areas.{name} must be a mapping")
            continue
        status = str(raw_entry.get("status") or "").strip().lower()
        notes = str(raw_entry.get("notes") or "").strip()
        commands = raw_entry.get("commands")
        if status not in VALID_REPORT_STATUSES:
            errors.append(
                f"verification_report.yaml focus_areas.{name}.status must be one of "
                f"{', '.join(sorted(VALID_REPORT_STATUSES))} (got {status or '<empty>'!r})"
            )
            continue
        if (status in {"not_applicable", "unknown"} or name == "lsp" and status == "fail") and not notes:
            errors.append(
                f"verification_report.yaml focus_areas.{name} with status={status} requires non-empty notes"
            )
        if outcome == "pass" and name != "lsp" and status in {"fail", "unknown"}:
            errors.append(
                f"PASS cannot be submitted while verification_report.yaml focus_areas.{name}.status is {status}"
            )
        if commands is not None and (
            not isinstance(commands, list)
            or any(not isinstance(item, str) for item in commands)
        ):
            errors.append(
                f"verification_report.yaml focus_areas.{name}.commands must be a list of strings"
            )

    extra_checks = report.get("extra_checks")
    if extra_checks is not None and not isinstance(extra_checks, list):
        errors.append("verification_report.yaml extra_checks must be a list")

    return errors


def load_and_validate_verification_report(
    workspace: Mapping[str, Any] | None,
    *,
    outcome: str,
    submitted_report: Mapping[str, Any] | None = None,
    expected_candidate_digest: str = "",
    submitted_yaml: str | None = None,
) -> tuple[dict[str, Any] | None, str, str, list[str]]:
    """Validate the exact document captured at submission (or the current draft)."""
    ws = workspace or {}
    location = _report_location(ws)
    display = location[2] if location else ""
    bound = bool(ws.get("verification_report_path"))
    raw = ""
    try:
        if submitted_report is not None:
            report = dict(submitted_report)
            if submitted_yaml is not None:
                if not isinstance(submitted_yaml, str) or _parse_report(submitted_yaml) != report:
                    raise ValueError("submitted verification report differs from its YAML attachment")
                raw = submitted_yaml
        elif location and bound:
            root, relative, _ = location
            raw = _read_bytes_beneath(root, relative).decode("utf-8")
            report = _parse_report(raw)
        elif bound:
            raise ValueError("verification report has no bound workspace location")
        else:
            return None, "", "", []
    except (OSError, ValueError, TypeError) as exc:
        return None, display, "", [f"cannot read verification_report.yaml at {display}: {exc}"]

    bound_digest = ws.get("verification_report_candidate_digest")
    errors = validate_verification_report(
        report, outcome=outcome,
        expected_candidate_digest=bound_digest or expected_candidate_digest,
    )
    if (not ws.get("verification_scratch_only") and bound_digest and expected_candidate_digest
            and expected_candidate_digest != bound_digest):
        errors.append("verification report workspace no longer matches the assigned Candidate")
    return report, display, raw, errors
