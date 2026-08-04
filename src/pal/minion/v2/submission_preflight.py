from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping


RequirementRef = tuple[str, str]


def raise_submission_errors(errors: Iterable[Any], *, owner: str) -> None:
    unique_by_message: dict[str, Any] = {}
    for item in errors:
        message = str(item).strip()
        if message:
            unique_by_message.setdefault(message, item)
    unique = list(unique_by_message.items())
    if not unique:
        return
    if len(unique) == 1:
        message, original = unique[0]
        if isinstance(original, ValueError):
            raise original
        raise ValueError(message)
    raise ValueError(
        f"{owner} found {len(unique)} consistent errors:\n"
        + "\n".join(f"- {message}" for message, _original in unique)
    )


def bound_reference_payload(
    workspace: Mapping[str, Any],
    name: str,
    *,
    required: bool = True,
) -> dict[str, Any]:
    for raw in list(workspace.get("reference_paths") or []):
        item = dict(raw or {})
        if str(item.get("name") or "") != name:
            continue
        path = Path(str(item.get("path") or "")).expanduser()
        # A projected /pal path is visible only inside the bwrap worker.  A
        # submission preflight can also run in a Manager-side continuation or
        # in a recovery probe, where that path is intentionally unavailable.
        # Resolve the authenticated immutable input by name in that case;
        # never accept a caller-supplied host path as a fallback.
        if bool(item.get("bound_input")) or not path.is_file():
            from pal.minion.v2.role_gateway import role_gateway_client_from_env

            gateway = role_gateway_client_from_env(
                Path(str(workspace.get("runtime_root") or ""))
            )
            if gateway is not None:
                response = gateway.request_sync("bound_input_json", {"name": name})
                value = response.get("value")
                if not isinstance(value, Mapping):
                    raise ValueError(f"bound input {name!r} must contain a JSON object")
                return dict(value)
            raise ValueError(f"bound input {name!r} is unavailable at {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError(f"bound input {name!r} must contain a JSON object")
        return dict(value)
    if required:
        raise ValueError(f"bound input {name!r} is required for submit preflight")
    return {}


def requirement_refs_from_view(view: Mapping[str, Any]) -> set[RequirementRef]:
    raw_requirements: Any = view.get("requirements", view)
    if isinstance(raw_requirements, Mapping):
        requirements = dict(raw_requirements)
        allowed = {
            (str(section), str(requirement))
            for section, values in dict(requirements.get("sections") or {}).items()
            for requirement in list(values or [])
            if str(section).strip() and str(requirement).strip()
        }
    else:
        allowed = {
            (
                str(dict(item or {}).get("section") or "Requirements"),
                str(dict(item or {}).get("statement") or dict(item or {}).get("requirement") or ""),
            )
            for item in list(raw_requirements or [])
            if isinstance(item, Mapping)
        }
        allowed = {(section, requirement) for section, requirement in allowed if section and requirement}
    return allowed


def submission_requirement_refs(value: Mapping[str, Any]) -> set[RequirementRef]:
    references: set[RequirementRef] = set()
    for owner in (value, *list(value.get("cases") or []), *list(value.get("findings") or [])):
        if not isinstance(owner, Mapping):
            continue
        for raw in list(owner.get("requirements") or []):
            if not isinstance(raw, Mapping):
                continue
            section = str(raw.get("section") or "")
            requirement = str(raw.get("requirement") or "")
            if section or requirement:
                references.add((section, requirement))
    return references


def validate_bound_requirement_refs(
    references: Iterable[RequirementRef],
    *,
    allowed: set[RequirementRef],
    owner: str,
) -> tuple[str, ...]:
    unknown = sorted(set(references) - allowed)
    if not unknown:
        return ()
    rendered = "; ".join(f"{section}: {requirement}" for section, requirement in unknown)
    allowed_rendered = "; ".join(
        f"{section}: {requirement}" for section, requirement in sorted(allowed)
    ) or "<none>"
    return (
        f"{owner} used an advisory Requirement citation outside its exact bound text: {rendered}. "
        f"Available exact Requirement text: {allowed_rendered}",
    )


def validate_submission_requirement_refs(
    value: Mapping[str, Any],
    *,
    work_view: Mapping[str, Any],
    owner: str,
) -> tuple[str, ...]:
    return validate_bound_requirement_refs(
        submission_requirement_refs(value),
        allowed=requirement_refs_from_view(work_view),
        owner=owner,
    )
