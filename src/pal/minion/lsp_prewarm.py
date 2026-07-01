from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pal.execution.contracts import CapabilityCall
from pal.lsp.ipc import LspManagerClient
from pal.lsp.plugin import LspManagerPluginProvider
from pal.minion.utils import string_list as _string_list
from pal.shared import RuntimeStatus


ProviderFactory = Callable[..., LspManagerPluginProvider]
DEFAULT_LSP_PREWARM_TIMEOUT_SECONDS = 15.0
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class LspPrewarmPlan:
    workspace_root: Path
    server_ids: tuple[str, ...]
    languages: tuple[str, ...]


def prewarm_workspace_lsp(
    *,
    runtime_root: Path,
    workspace: dict[str, Any],
    provider_factory: ProviderFactory = LspManagerPluginProvider,
    request_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    plan = lsp_prewarm_plan(workspace)
    if plan is None:
        return {"status": "skipped", "reason": "no_lsp_servers_for_workspace"}

    provider = provider_factory(runtime_root=Path(runtime_root))
    _apply_provider_timeout(
        provider,
        runtime_root=Path(runtime_root),
        timeout_seconds=request_timeout_seconds if request_timeout_seconds is not None else _prewarm_timeout_seconds(workspace),
    )
    results: list[dict[str, Any]] = []
    for server_id in plan.server_ids:
        args: dict[str, Any] = {
            "server_id": server_id,
            "workspace_root": str(plan.workspace_root),
        }
        if plan.languages:
            args["workspace_languages"] = list(plan.languages)
        try:
            result = provider.doctor(CapabilityCall(name="op_lsp_doctor", args=args))
        except Exception as exc:
            results.append(
                {
                    "server_id": server_id,
                    "status": RuntimeStatus.ERROR,
                    "ok": False,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
            continue
        structured = dict(result.structured or {})
        status = str(structured.get("status") or result.status or "")
        results.append(
            {
                "server_id": server_id,
                "status": status,
                "ok": result.status == RuntimeStatus.OK and status == "ok",
                **(
                    {"reason": str(structured.get("reason") or "")}
                    if str(structured.get("reason") or "").strip()
                    else {}
                ),
            }
        )

    ok_count = len([item for item in results if item.get("ok")])
    status = "ok" if ok_count == len(results) else "partial" if ok_count else "unavailable"
    _log_prewarm_result(plan=plan, status=status, ok_count=ok_count, results=results)
    return {
        "status": status,
        "workspace_root": str(plan.workspace_root),
        "servers": results,
        "ok_count": ok_count,
    }


def lsp_prewarm_plan(workspace: dict[str, Any]) -> LspPrewarmPlan | None:
    root = _workspace_root(workspace)
    if root is None:
        return None
    lsp_setup = workspace.get("lsp_setup")
    if not isinstance(lsp_setup, dict):
        return None
    server_ids = tuple(_dedupe(_string_list(lsp_setup.get("servers"))))
    if not server_ids:
        return None
    languages = tuple(_dedupe(_string_list(lsp_setup.get("languages") or workspace.get("languages"))))
    return LspPrewarmPlan(workspace_root=root, server_ids=server_ids, languages=languages)


def _apply_provider_timeout(provider: Any, *, runtime_root: Path, timeout_seconds: float) -> None:
    if not isinstance(provider, LspManagerPluginProvider):
        return
    provider.client = LspManagerClient(runtime_root=runtime_root, request_timeout_seconds=timeout_seconds)


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
    for key in ("review_scratch_repo_path", "repo_path", "task_repo_path", "target_repo_path"):
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
