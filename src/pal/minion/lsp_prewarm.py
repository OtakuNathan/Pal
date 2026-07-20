from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pal.lsp.ipc import LspManagerClient
from pal.minion.utils import string_list as _string_list


ClientFactory = Callable[..., LspManagerClient]
DEFAULT_LSP_PREWARM_TIMEOUT_SECONDS = 180.0
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class LspPrewarmPlan:
    workspace_root: Path
    primary_language: str
    languages: tuple[str, ...]


def prewarm_workspace_lsp(
    *,
    runtime_root: Path,
    workspace: dict[str, Any],
    client_factory: ClientFactory = LspManagerClient,
    request_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    plan = lsp_prewarm_plan(workspace)
    if plan is None:
        return {"status": "skipped", "reason": "workspace_language_unknown"}

    client = client_factory(
        runtime_root=Path(runtime_root),
        request_timeout_seconds=(
            request_timeout_seconds
            if request_timeout_seconds is not None
            else _prewarm_timeout_seconds(workspace)
        ),
    )
    params: dict[str, Any] = {
        "workspace_root": str(plan.workspace_root),
        "primary_language": plan.primary_language,
        "languages": list(plan.languages),
        "prewarm": True,
    }
    for key in (
        "compile_commands_path",
        "include_paths",
        "stub_include_paths",
        "cpp_standard",
        "lsp_compile_flags",
    ):
        value = workspace.get(key)
        if value not in (None, "", [], {}):
            params[key] = value
    try:
        result = client.prepare_workspace_sync(params)
    except Exception as exc:
        result = {
            "status": "unavailable",
            "workspace_root": str(plan.workspace_root),
            "error": f"{exc.__class__.__name__}: {exc}",
            "servers": [],
        }
    servers = list(result.get("servers") or [])
    ok_count = len([item for item in servers if item.get("status") == "ok"])
    _log_prewarm_result(
        plan=plan,
        status=str(result.get("status") or "unavailable"),
        ok_count=ok_count,
        results=servers,
    )
    return result


def lsp_prewarm_plan(workspace: dict[str, Any]) -> LspPrewarmPlan | None:
    root = _workspace_root(workspace)
    if root is None:
        return None
    languages = tuple(_dedupe(_string_list(workspace.get("languages"))))
    primary_language = str(workspace.get("primary_language") or "").strip()
    if not primary_language and languages:
        primary_language = languages[0]
    if not primary_language:
        return None
    return LspPrewarmPlan(
        workspace_root=root,
        primary_language=primary_language,
        languages=languages or (primary_language,),
    )


def _prewarm_timeout_seconds(workspace: dict[str, Any]) -> float:
    for key in ("lsp_prewarm_timeout_seconds", "prewarm_timeout_seconds"):
        raw = (workspace or {}).get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return DEFAULT_LSP_PREWARM_TIMEOUT_SECONDS


def _log_prewarm_result(*, plan: LspPrewarmPlan, status: str, ok_count: int, results: list[dict[str, Any]]) -> None:
    details = [
        {
            "server_id": str(item.get("server_id") or ""),
            "status": str(item.get("status") or ""),
            "reason": str(item.get("reason") or item.get("error") or ""),
        }
        for item in results
    ]
    message = "LSP prewarm %s for %s (%s/%s ok): %s"
    args = (status, plan.workspace_root, ok_count, len(results), details)
    if status == "ok":
        LOGGER.debug(message, *args)
    else:
        LOGGER.warning(message, *args)


def _workspace_root(workspace: dict[str, Any]) -> Path | None:
    for key in ("review_scratch_path", "workspace_path", "repo_path", "task_repo_path", "target_repo_path"):
        value = str((workspace or {}).get(key) or "").strip()
        if not value:
            continue
        path = Path(value).expanduser().resolve()
        if path.exists():
            return path
    return None


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result
