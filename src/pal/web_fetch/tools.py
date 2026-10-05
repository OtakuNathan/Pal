from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pal.execution.contracts import CapabilityResult
from pal.foundation import ArtifactIngestor
from pal.shared import RuntimeStatus
from pal.shared.result_rendering import render_titled_structured_for_llm
from pal.web_fetch.browser_service import BrowserServiceError
from pal.web_fetch.service import WebFetchService


@dataclass
class BrowserScreenshotTool:
    service: WebFetchService

    async def ainvoke(
        self,
        args: dict[str, Any],
        *,
        session_key: str,
        persistent: bool,
        runtime: Any,
        turn_id: str,
    ) -> CapabilityResult:
        try:
            payload = self.service.execute(
                session_key=session_key,
                action="screenshot",
                args=args,
                persistent=persistent,
                timeout_ms=int(args.get("timeout_ms") or 30000),
            )
            encoded = str(payload.pop("png_base64", "") or "")
            content = base64.b64decode(encoded.encode("ascii"), validate=True)
            page = dict(payload.get("page") or {})
            file_name = _screenshot_file_name(str(page.get("url") or "web_page"))
            stored = ArtifactIngestor(_runtime_root(runtime, self.service)).store_bytes(
                channel_kind="web_fetch",
                bucket_id=str(turn_id or "manual"),
                file_name=file_name,
                content=content,
                mime_type="image/png",
            )
            payload["artifact"] = {
                "stored_artifact_id": stored.artifact_id,
                "local_cached_path": stored.local_cached_path,
                "mime_type": stored.mime_type or "image/png",
                "size_bytes": stored.size_bytes,
                "sha256": stored.sha256,
            }
            return await self._attach_saved_screenshot(payload, stored, runtime, turn_id)
        except BrowserServiceError as exc:
            return _result(RuntimeStatus.ERROR, "Browser screenshot failed", {"error": exc.to_dict()})
        except Exception as exc:
            return _result(
                RuntimeStatus.ERROR,
                "Browser screenshot failed",
                {"error": {"code": "screenshot_failed", "message": str(exc), "retryable": False}},
            )

    async def _attach_saved_screenshot(self, payload, stored, runtime, turn_id) -> CapabilityResult:
        owner = getattr(runtime, "provider_registry", {}).get("artifact:artifact")
        register = getattr(owner, "import_local_for_turn", None)
        if not callable(register):
            payload["registration"] = "unavailable"
            payload["next_step"] = "Screenshot saved but not registered or attached inline: artifact owner is unavailable. Once available, use import_artifact with artifact.local_cached_path."
            return _result(RuntimeStatus.OK, "Browser screenshot saved", payload)
        try:
            imported = await register(stored, runtime=runtime, turn_id=turn_id, source_channel="web_fetch")
        except Exception as exc:
            payload["registration"] = "unknown"
            payload["registration_error"] = str(exc)
            payload["next_step"] = "Screenshot saved; registration outcome is uncertain and no inline image was delivered. Inspect list_artifacts before importing the saved path again."
            return _result(RuntimeStatus.OK, "Browser screenshot saved", payload)
        if imported.status != RuntimeStatus.OK:
            payload["registration"] = "failed"
            payload["registration_error"] = dict(imported.structured or {})
            payload["next_step"] = "Screenshot saved but not attached inline. Resolve the registration error; an existing artifact_id can be inspected with inspect_artifact_info."
            return _result(RuntimeStatus.OK, "Browser screenshot saved", payload)
        payload["artifact_id"] = imported.structured["artifact_id"]
        payload["registration"] = "registered"
        payload["next_step"] = "Use the attached image if core supplies pixels for this model; otherwise use the artifact metadata and discover suitable image processing if needed. Do not import this screenshot again."
        return replace(
            _result(RuntimeStatus.OK, "Browser screenshot registered", payload),
            context_messages=imported.context_messages,
        )


def _runtime_root(runtime: Any, service: WebFetchService) -> Path:
    root = getattr(runtime, "runtime_root", None)
    return Path(root) if root is not None else Path(service.browser_manager.runtime_root)


def _screenshot_file_name(url: str) -> str:
    import re

    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(url or "web_page")).strip("._")
    return f"{(cleaned or 'web_page')[:80]}.png"


def _result(status: str, title: str, structured: dict[str, Any]) -> CapabilityResult:
    return CapabilityResult(
        status=status,
        text=title.lower(),
        structured=structured,
        llm_text=render_titled_structured_for_llm(title, structured),
    )
