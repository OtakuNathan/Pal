from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping


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
            from pal.bunshin.role_gateway_client import role_gateway_client_from_env

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
