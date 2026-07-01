from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pal.channel.channel_endpoint_queue_base import ChannelEndpointQueueBase
from pal.channel.contracts import ChannelDeliveryError, EndpointConfig, ResponseHandle
from pal.control.contracts import InteractionButtonSpec, InteractionMessageSpec, InteractionResult
from pal.foundation.artifact import ArtifactIngestor
from pal.foundation import AttachmentSpec, EventEnvelope
from pal.shared import EventKind, SourceKind
from pal.stream_events import NormalizedLLMStreamEvent


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _FallbackBotCommand:
    command: str
    description: str


@dataclass(frozen=True)
class _FallbackMenuButtonCommands:
    type: str = "commands"


def _proxy_from_env() -> str | None:
    for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return None


@dataclass(frozen=True)
class TelegramBinding:
    chat_id: str | None = None
    user_id: str | None = None

    @classmethod
    def parse(cls, binding_key: str) -> TelegramBinding:
        raw = str(binding_key or "").strip()
        if not raw or ":" not in raw:
            return cls()
        scope, remainder = raw.split(":", 1)
        parts = [p for p in remainder.split(":") if p]
        if scope == "user" and parts:
            return cls(user_id=parts[0])
        if scope == "chat" and parts:
            return cls(chat_id=parts[0])
        if scope == "chat_user" and len(parts) >= 2:
            return cls(chat_id=parts[0], user_id=parts[1])
        return cls()

    @property
    def binding_key(self) -> str:
        if self.chat_id and self.user_id:
            return f"chat_user:{self.chat_id}:{self.user_id}"
        if self.chat_id:
            return f"chat:{self.chat_id}"
        if self.user_id:
            return f"user:{self.user_id}"
        return ""

    def matches(self, *, chat_id: int | None, user_id: int | None) -> bool:
        if self.chat_id and self.user_id:
            return (user_id is not None and str(user_id) == self.user_id
                    and chat_id is not None and str(chat_id) == self.chat_id)
        if self.chat_id:
            return chat_id is not None and str(chat_id) == self.chat_id
        if self.user_id:
            return user_id is not None and str(user_id) == self.user_id
        return False


def _telegram_markdown(text: str) -> tuple[str, str | None]:
    normalized = _flatten_gfm_tables_for_telegram(str(text or ""))
    try:
        import telegramify_markdown

        return telegramify_markdown.markdownify(normalized), "MarkdownV2"
    except Exception:
        return normalized, None


def _flatten_gfm_tables_for_telegram(text: str) -> str:
    lines = str(text or "").splitlines()
    out: list[str] = []
    index = 0
    in_code_fence = False
    while index < len(lines):
        line = lines[index]
        if line.strip().startswith("```"):
            in_code_fence = not in_code_fence
            out.append(line)
            index += 1
            continue
        if not in_code_fence and index + 1 < len(lines) and _looks_like_table_row(line) and _looks_like_table_separator(lines[index + 1]):
            headers = _split_table_row(line)
            rows: list[list[str]] = []
            index += 2
            while index < len(lines) and _looks_like_table_row(lines[index]) and not _looks_like_table_separator(lines[index]):
                rows.append(_split_table_row(lines[index]))
                index += 1
            if rows:
                out.extend(_render_table_rows_for_telegram(headers, rows))
                continue
            out.append(line)
            continue
        out.append(line)
        index += 1
    return "\n".join(out)


def _looks_like_table_row(line: str) -> bool:
    stripped = str(line or "").strip()
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2


def _looks_like_table_separator(line: str) -> bool:
    cells = _split_table_row(line)
    if not cells:
        return False
    for cell in cells:
        normalized = cell.replace(":", "").replace("-", "").strip()
        if normalized:
            return False
        if "-" not in cell:
            return False
    return True


def _split_table_row(line: str) -> list[str]:
    stripped = str(line or "").strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _render_table_rows_for_telegram(headers: list[str], rows: list[list[str]]) -> list[str]:
    rendered: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        first = row[0].strip() if row else ""
        rendered.append(f"- {first or f'Row {row_index}'}")
        for idx, cell in enumerate(row[1:], start=1):
            value = cell.strip()
            if not value:
                continue
            header = headers[idx].strip() if idx < len(headers) else ""
            if header:
                rendered.append(f"  {header}: {value}")
            else:
                rendered.append(f"  {value}")
        rendered.append("")
    if rendered and rendered[-1] == "":
        rendered.pop()
    return rendered


def _segment_text(text: str, *, limit: int) -> list[str]:
    stripped = str(text or "").strip()
    if not stripped:
        return [""]
    if len(stripped) <= limit:
        return [stripped]

    parts: list[str] = []
    remaining = stripped
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at <= 0:
            split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunk = remaining[:split_at].strip()
        if not chunk:
            chunk = remaining[:limit].strip()
            split_at = limit
        parts.append(chunk)
        remaining = remaining[split_at:].strip()
    return [part for part in parts if part]


def _safe_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except Exception:
        return None


@dataclass
class TelegramChannelEndpoint(ChannelEndpointQueueBase):
    runtime_root: Any = None
    bot_token: str = ""
    base_url: str = "https://api.telegram.org"
    poll_timeout_seconds: int = 30
    allowed_updates: tuple[str, ...] = ("message", "edited_message", "callback_query")
    proxy_url: str | None = None
    binding: TelegramBinding = field(default_factory=TelegramBinding)
    application: Any = None
    polling_task: asyncio.Task[None] | None = None
    _start_event: asyncio.Event | None = None
    _stop_event: asyncio.Event | None = None
    _ingestor: ArtifactIngestor | None = None
    _typing_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    _send_chains: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    _polling_running: bool = False
    _authorized: bool = False
    _last_poll_error: str = ""
    _last_status_error: str = ""
    _last_poll_error_at: float = 0.0
    _poll_error_stale_threshold_seconds: float = 10.0
    _poll_monitor_interval_seconds: float = 0.5
    _application_shutdown_timeout_seconds: float = 5.0
    _reconnect_delays: tuple[float, ...] = (1.0, 3.0, 10.0, 30.0)
    _reconnect_attempts: int = 0
    _reconnecting: bool = False
    _last_get_updates_activity_at: float = 0.0
    _get_updates_in_flight_started_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.binding.chat_id and not self.binding.user_id and self.endpoint.binding_key:
            self.binding = TelegramBinding.parse(self.endpoint.binding_key)

    def normalize_raw(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"text": str(payload or "")}
        attachments = payload.get("attachments")
        normalized_attachments = list(attachments) if isinstance(attachments, list) else []
        return {
            "text": str(payload.get("text") or payload.get("caption") or ""),
            "chat_id": str(payload.get("chat_id") or ""),
            "message_id": str(payload.get("message_id") or ""),
            "thread_id": str(payload.get("thread_id") or ""),
            "from_user_id": str(payload.get("from_user_id") or ""),
            "attachments": normalized_attachments,
            "source_metadata": dict(payload.get("source_metadata") or {}),
        }

    async def start_async(self) -> None:
        self.proxy_url = _proxy_from_env()
        self._ingestor = ArtifactIngestor(self.runtime_root)
        self._last_poll_error = ""
        self._last_poll_error_at = 0.0
        self._last_get_updates_activity_at = 0.0
        self._get_updates_in_flight_started_at = 0.0
        self._reconnect_attempts = 0
        self._reconnecting = False
        if not self._can_start():
            self._polling_running = False
            return
        if self.polling_task is not None and not self.polling_task.done():
            return
        self._start_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self.polling_task = asyncio.create_task(self._polling_main())
        await self._start_event.wait()

    async def stop_async(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self.polling_task is not None:
            self.polling_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.polling_task
        self.polling_task = None
        self._polling_running = False
        self._reconnecting = False
        for task in list(self._typing_tasks.values()):
            task.cancel()
        self._typing_tasks.clear()
        for task in list(self._send_chains.values()):
            task.cancel()
        self._send_chains.clear()

    def send_reply(self, response_handle: ResponseHandle, text: str) -> None:
        if self.application is None:
            raise ChannelDeliveryError("telegram application not running", permanent=False)
        self._schedule_ordered_send(response_handle, lambda: self._send_reply_async(response_handle, text))

    def send_attachment(self, response_handle: ResponseHandle, attachment: AttachmentSpec) -> None:
        self._schedule_ordered_send(response_handle, lambda: self._send_attachment_async(response_handle, attachment))

    def send_status(self, response_handle: ResponseHandle, kind: str, payload: dict[str, Any]) -> None:
        loop = asyncio.get_running_loop()
        if kind == "control_catalog":
            loop.create_task(self._apply_control_catalog_async(payload))
            return
        if kind in {"interactive_open", "interactive_update", "interactive_resolve", "interactive_expire"}:
            loop.create_task(self._apply_interactive_status_async(response_handle, kind=kind, payload=payload))
            return
        if kind == "typing_start":
            key = self._typing_key(response_handle)
            if key in self._typing_tasks and not self._typing_tasks[key].done():
                return
            self._typing_tasks[key] = loop.create_task(self._typing_loop(response_handle))
            return
        if kind == "typing_stop" or kind == "working_stop":
            self._stop_typing(response_handle)
            return
        if kind == "receipt_marker":
            loop.create_task(self._send_receipt_marker_async(response_handle, payload))

    def send_stream_event(self, response_handle: ResponseHandle, event: NormalizedLLMStreamEvent) -> None:
        super().send_stream_event(response_handle, event)

    def apply_auth_material(self, material: dict[str, Any]) -> dict[str, Any]:
        bot_token = str(material.get("bot_token") or "").strip()
        if bot_token:
            self.bot_token = bot_token
        self._authorized = bool(self.bot_token)
        self.pair(pairing_metadata={"accepted_keys": sorted(str(key) for key in material.keys())})
        return self.inspect_auth_state()

    def pair(self, *, binding_key: str | None = None, send_policy: dict[str, Any] | None = None, pairing_metadata: dict[str, Any] | None = None) -> None:
        super().pair(binding_key=binding_key, send_policy=send_policy, pairing_metadata=pairing_metadata)
        self.binding = TelegramBinding.parse(self.endpoint.binding_key)

    def derive_default_reply_target(self) -> dict[str, Any]:
        chat_id = self.binding.chat_id or self.binding.user_id
        if chat_id:
            return self._build_reply_target(chat_id=chat_id)
        return {}

    def _build_reply_target(
        self,
        *,
        chat_id: int | str | None,
        message_id: int | str | None = None,
        thread_id: int | str | None = None,
    ) -> dict[str, Any]:
        chat = str(chat_id or "").strip()
        thread = str(thread_id or "").strip()
        target = {
            "chat_id": chat,
            "message_id": str(message_id or "").strip(),
            "thread_id": thread,
        }
        if chat:
            target["control_scope_key"] = f"telegram:{self.endpoint.endpoint_id}:{chat}:{thread or 'root'}"
        return target

    def inspect_health(self) -> dict[str, Any]:
        now = time.monotonic()
        activity_age = (
            max(0.0, now - self._last_get_updates_activity_at)
            if self._last_get_updates_activity_at
            else None
        )
        in_flight_age = (
            max(0.0, now - self._get_updates_in_flight_started_at)
            if self._get_updates_in_flight_started_at
            else 0.0
        )
        reason = ""
        if not self.enabled:
            reason = "endpoint_disabled"
        elif not self.attached:
            reason = "endpoint_detached"
        elif not self.bot_token:
            reason = "token_missing"
        elif not self.endpoint.binding_key:
            reason = "binding_missing"
        elif self._last_poll_error:
            reason = "polling_error"
        elif not self._polling_running:
            reason = "polling_not_running"
        return {
            "healthy": bool(self._polling_running and not self._last_poll_error),
            "polling_running": self._polling_running,
            "reconnecting": self._reconnecting,
            "reconnect_attempts": self._reconnect_attempts,
            "authorized": bool(self.bot_token and self._authorized),
            "proxy_enabled": bool(self.proxy_url),
            "last_poll_error": self._last_poll_error,
            "last_get_updates_activity_age_seconds": round(activity_age, 3) if activity_age is not None else None,
            "get_updates_in_flight_seconds": round(in_flight_age, 3),
            "last_status_error": self._last_status_error,
            "last_delivery_error": self.last_delivery_error,
            "reason": reason,
        }

    def inspect_auth_state(self) -> dict[str, Any]:
        return {
            "paired": bool(self.endpoint.binding_key),
            "authorized": bool(self.bot_token and self._authorized),
            "token_present": bool(self.bot_token),
        }

    def inspect_backlog(self) -> dict[str, Any]:
        payload = super().inspect_backlog()
        payload["typing_sessions"] = sum(1 for task in self._typing_tasks.values() if not task.done())
        return payload

    async def _polling_main(self) -> None:
        started = self._start_event
        first_attempt = True
        try:
            while self._stop_event is not None and not self._stop_event.is_set():
                app = None
                try:
                    app = self._build_application()
                    self.application = app
                    await app.initialize()
                    await app.start()
                    await app.updater.start_polling(
                        timeout=self.poll_timeout_seconds,
                        drop_pending_updates=first_attempt,
                        allowed_updates=list(self.allowed_updates),
                        error_callback=self._on_polling_error,
                    )
                    self._authorized = True
                    self._polling_running = True
                    self._last_poll_error = ""
                    self._last_poll_error_at = 0.0
                    self._last_get_updates_activity_at = time.monotonic()
                    self._get_updates_in_flight_started_at = 0.0
                    self._reconnect_attempts = 0
                    self._reconnecting = False
                    logger.info("telegram polling started successfully")
                    if self._control_commands_manifest:
                        await self._apply_control_catalog_async({"commands": list(self._control_commands_manifest)})
                except Exception as exc:
                    self._last_poll_error = str(exc)
                    self._authorized = False
                    self._polling_running = False
                    logger.exception("telegram endpoint polling failed to start")
                    if first_attempt and started is not None and not started.is_set():
                        started.set()
                    first_attempt = False
                    if self._stop_event is None or self._stop_event.is_set():
                        break
                    await self._sleep_before_reconnect()
                    continue
                if first_attempt and started is not None and not started.is_set():
                    started.set()
                first_attempt = False
                try:
                    while self._stop_event is not None and not self._stop_event.is_set():
                        updater = getattr(app, "updater", None)
                        if updater is not None and not bool(getattr(updater, "running", False)):
                            self._last_poll_error = "polling_stopped"
                            self._last_poll_error_at = time.monotonic()
                            self._polling_running = False
                            break
                        if self._last_poll_error and self._last_poll_error_at:
                            elapsed = time.monotonic() - self._last_poll_error_at
                            if elapsed >= self._poll_error_stale_threshold_seconds:
                                logger.warning(
                                    "telegram polling error persisted for %.0fs (%s), forcing reconnect",
                                    elapsed, self._last_poll_error,
                                )
                                self._polling_running = False
                                break
                        if self._get_updates_in_flight_started_at:
                            elapsed = time.monotonic() - self._get_updates_in_flight_started_at
                            hard_timeout = max(float(self.poll_timeout_seconds) + 45.0, 60.0)
                            if elapsed >= hard_timeout:
                                self._last_poll_error = f"getUpdates stuck for {elapsed:.0f}s"
                                self._last_poll_error_at = time.monotonic()
                                logger.warning("telegram getUpdates stuck for %.0fs, forcing reconnect", elapsed)
                                self._polling_running = False
                                break
                        await asyncio.sleep(self._poll_monitor_interval_seconds)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("telegram monitoring loop crashed unexpectedly")
                    self._last_poll_error = f"monitor_error: {exc}"
                    self._last_poll_error_at = time.monotonic()
                finally:
                    self._polling_running = False
                    await self._shutdown_application(app)
                    self.application = None
                if self._stop_event is None or self._stop_event.is_set():
                    break
                await self._sleep_before_reconnect()
        except asyncio.CancelledError:
            pass
        finally:
            self._polling_running = False
            self._reconnecting = False
            if started is not None and not started.is_set():
                started.set()
            if self.application is not None:
                await self._shutdown_application(self.application)
            self.application = None

    async def _sleep_before_reconnect(self) -> None:
        if self._stop_event is None or self._stop_event.is_set():
            return
        self._reconnecting = True
        index = min(self._reconnect_attempts, max(len(self._reconnect_delays) - 1, 0))
        delay = self._reconnect_delays[index] if self._reconnect_delays else 0.0
        self._reconnect_attempts += 1
        logger.info("telegram reconnecting in %.1fs (attempt %d)", delay, self._reconnect_attempts)
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            return

    async def _shutdown_application(self, app: Any) -> None:
        updater = getattr(app, "updater", None)
        await self._shutdown_step(
            "updater.stop",
            getattr(updater, "stop", None),
            enabled=updater is not None,
        )
        await self._shutdown_step("app.stop", getattr(app, "stop", None), enabled=bool(getattr(app, "running", False)))
        await self._shutdown_step("app.shutdown", getattr(app, "shutdown", None), enabled=True)

    async def _shutdown_step(self, name: str, call: Any, *, enabled: bool) -> None:
        if not enabled or call is None:
            return
        try:
            await asyncio.wait_for(call(), timeout=max(0.1, float(self._application_shutdown_timeout_seconds)))
        except asyncio.TimeoutError:
            logger.warning("telegram shutdown step timed out: %s", name)
        except Exception:
            logger.exception("telegram shutdown step failed: %s", name)

    def _record_get_updates_started(self) -> None:
        self._get_updates_in_flight_started_at = time.monotonic()

    def _record_get_updates_success(self) -> None:
        self._last_get_updates_activity_at = time.monotonic()
        self._get_updates_in_flight_started_at = 0.0
        self._last_poll_error = ""
        self._last_poll_error_at = 0.0

    def _record_get_updates_failure(self, exc: Exception) -> None:
        self._last_get_updates_activity_at = time.monotonic()
        self._get_updates_in_flight_started_at = 0.0
        message = str(exc).strip()
        self._last_poll_error = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
        if not self._last_poll_error_at:
            self._last_poll_error_at = time.monotonic()

    def _build_application(self):
        from telegram.ext import ApplicationBuilder, TypeHandler
        from telegram import Update
        from telegram.request import HTTPXRequest

        owner = self

        class _ObservedGetUpdatesRequest(HTTPXRequest):
            async def do_request(self, *args: Any, **kwargs: Any) -> tuple[int, bytes]:
                owner._record_get_updates_started()
                try:
                    result = await super().do_request(*args, **kwargs)
                except Exception as exc:
                    owner._record_get_updates_failure(exc)
                    raise
                owner._record_get_updates_success()
                return result

        bot_request = HTTPXRequest(
            connection_pool_size=32,
            read_timeout=30,
            write_timeout=30,
            connect_timeout=10,
            pool_timeout=30,
            proxy=self.proxy_url,
        )
        get_updates_request = _ObservedGetUpdatesRequest(
            connection_pool_size=4,
            read_timeout=float(self.poll_timeout_seconds) + 10.0,
            write_timeout=30,
            connect_timeout=10,
            pool_timeout=30,
            proxy=self.proxy_url,
        )

        builder = ApplicationBuilder().token(self.bot_token).concurrent_updates(True)
        builder = builder.request(bot_request).get_updates_request(get_updates_request)
        if self.base_url != "https://api.telegram.org":
            base = self.base_url.rstrip("/")
            builder = builder.base_url(f"{base}/bot").base_file_url(f"{base}/file/bot")
        app = builder.build()
        app.add_handler(TypeHandler(Update, self._on_update))
        app.add_error_handler(self._on_error)
        return app

    def _can_start(self) -> bool:
        return bool(self.enabled and self.attached and self.bot_token and self.endpoint.binding_key)

    async def _on_error(self, update: object, context: Any) -> None:
        _ = update
        self._last_poll_error = str(getattr(context, "error", "") or "telegram_error")
        if not self._last_poll_error_at:
            self._last_poll_error_at = time.monotonic()
        logger.warning("telegram poll error: %s", self._last_poll_error)

    def _on_polling_error(self, exc: Exception) -> None:
        self._last_poll_error = str(exc) or type(exc).__name__
        if not self._last_poll_error_at:
            self._last_poll_error_at = time.monotonic()
        logger.warning("telegram polling network error: %s", self._last_poll_error)

    async def _on_update(self, update: Any, context: Any) -> None:
        _ = context
        self._last_poll_error = ""
        self._last_poll_error_at = 0.0
        if getattr(update, "callback_query", None) is not None:
            interaction_result = await self._interaction_result_from_update(update)
            if interaction_result is None:
                return
            message = getattr(getattr(update, "callback_query", None), "message", None)
            reply_target = self._build_reply_target(
                chat_id=_safe_int(getattr(getattr(message, "chat", None), "id", None)),
                message_id=getattr(message, "message_id", "") or "",
                thread_id=getattr(message, "message_thread_id", "") or "",
            )
            self.emit_interaction_result(
                interaction_result,
                correlation_id=str(getattr(getattr(update, "callback_query", None), "id", "") or ""),
                reply_target=reply_target,
            )
            return
        payload = await self._payload_from_update(update)
        if payload is None:
            return
        envelope = self.accept_raw(
            payload,
            event_kind="user.message",
            correlation_id=str(payload.get("message_id") or ""),
            reply_target=self._build_reply_target(
                chat_id=payload["chat_id"],
                message_id=payload["message_id"],
                thread_id=payload.get("thread_id") or "",
            ),
        )
        if envelope is None:
            return
        self.queue_status("receipt_marker", response_handle=envelope.response_handle)

    async def _interaction_result_from_update(self, update: Any) -> InteractionResult | None:
        callback_query = getattr(update, "callback_query", None)
        if callback_query is None:
            return None
        data = str(getattr(callback_query, "data", "") or "").strip()
        if not data.startswith("ix:"):
            return None
        message = getattr(callback_query, "message", None)
        if message is None:
            return None
        chat = getattr(message, "chat", None)
        chat_id = _safe_int(getattr(chat, "id", None))
        from_user = getattr(callback_query, "from_user", None)
        user_id = _safe_int(getattr(from_user, "id", None))
        if not self._matches_binding(chat_id=chat_id, user_id=user_id):
            return None
        with contextlib.suppress(Exception):
            await callback_query.answer()
        interaction_id, button_token = self.parse_interaction_callback_data(data)
        if not interaction_id or not button_token:
            return None
        metadata = self._interactive_messages.get(interaction_id)
        if not metadata:
            return None
        if self.is_interaction_metadata_expired(metadata):
            target = self._interactive_messages.pop(interaction_id, None)
            if isinstance(target, dict):
                await self._edit_interaction_message_async(
                    target,
                    spec=InteractionMessageSpec(
                        interaction_id=interaction_id,
                        interaction_kind=str(metadata.get("interaction_kind") or ""),
                        text=self.expired_interaction_text(str(metadata.get("interaction_kind") or "")),
                        buttons=(),
                        expires_at=None,
                    ),
                    clear_keyboard=True,
                )
            return None
        return self.interaction_result_from_token(interaction_id, button_token)

    async def _payload_from_update(self, update: Any) -> dict[str, Any] | None:
        message = getattr(update, "effective_message", None)
        if message is None:
            return None
        chat = getattr(message, "chat", None)
        from_user = getattr(message, "from_user", None)
        chat_id = _safe_int(getattr(chat, "id", None))
        user_id = _safe_int(getattr(from_user, "id", None))
        if not self._matches_binding(chat_id=chat_id, user_id=user_id):
            return None
        attachments = await self._extract_attachments(message, chat_id=chat_id)
        return {
            "text": str(getattr(message, "text", None) or getattr(message, "caption", None) or ""),
            "chat_id": str(chat_id or ""),
            "message_id": str(getattr(message, "message_id", "") or ""),
            "thread_id": str(getattr(message, "message_thread_id", "") or ""),
            "from_user_id": str(user_id or ""),
            "attachments": attachments,
            "source_metadata": {"telegram_update_id": str(getattr(update, "update_id", "") or "")},
        }

    def _matches_binding(self, *, chat_id: int | None, user_id: int | None) -> bool:
        return self.binding.matches(chat_id=chat_id, user_id=user_id)

    async def _extract_attachments(self, message: Any, *, chat_id: int | None) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        if self._ingestor is None or self.application is None or chat_id is None:
            return attachments
        document = getattr(message, "document", None)
        if document is not None:
            item = await self._download_telegram_file(
                provider_file_id=str(getattr(document, "file_id", "") or ""),
                file_name=str(getattr(document, "file_name", "") or "document.bin"),
                mime_type=getattr(document, "mime_type", None),
                chat_id=chat_id,
            )
            if item is not None:
                attachments.append(item)
        photos = getattr(message, "photo", None) or []
        if photos:
            largest = photos[-1]
            item = await self._download_telegram_file(
                provider_file_id=str(getattr(largest, "file_id", "") or ""),
                file_name=f"{str(getattr(largest, 'file_unique_id', '') or 'photo')}.jpg",
                mime_type="image/jpeg",
                chat_id=chat_id,
            )
            if item is not None:
                attachments.append(item)
        audio = getattr(message, "audio", None)
        if audio is not None:
            item = await self._download_telegram_file(
                provider_file_id=str(getattr(audio, "file_id", "") or ""),
                file_name=str(getattr(audio, "file_name", "") or "audio.bin"),
                mime_type=getattr(audio, "mime_type", None),
                chat_id=chat_id,
            )
            if item is not None:
                attachments.append(item)
        voice = getattr(message, "voice", None)
        if voice is not None:
            item = await self._download_telegram_file(
                provider_file_id=str(getattr(voice, "file_id", "") or ""),
                file_name=f"{str(getattr(voice, 'file_unique_id', '') or 'voice')}.ogg",
                mime_type=getattr(voice, "mime_type", None) or "audio/ogg",
                chat_id=chat_id,
            )
            if item is not None:
                attachments.append(item)
        return attachments

    async def _download_telegram_file(
        self,
        *,
        provider_file_id: str,
        file_name: str,
        mime_type: str | None,
        chat_id: int,
    ) -> dict[str, Any] | None:
        if not provider_file_id or self._ingestor is None or self.application is None:
            return None
        try:
            tg_file = await self.application.bot.get_file(provider_file_id)
            content = await tg_file.download_as_bytearray()
        except Exception as exc:
            self._last_poll_error = str(exc)
            logger.exception("telegram file download failed: %s", provider_file_id)
            return None
        stored = self._ingestor.store_bytes(
            channel_kind="telegram",
            bucket_id=str(chat_id),
            file_name=file_name,
            content=bytes(content),
            mime_type=mime_type,
        )
        tg_file_path = str(getattr(tg_file, "file_path", "") or "")
        source_url = ""
        if tg_file_path:
            if re.match(r"^https?://", tg_file_path, flags=re.IGNORECASE):
                source_url = tg_file_path
            else:
                base = self.base_url.rstrip("/")
                source_url = f"{base}/file/bot{self.bot_token}/{tg_file_path.lstrip('/')}"
        return {
            "attachment_id": f"telegram_{provider_file_id}",
            "provider_file_id": provider_file_id,
            "file_name": file_name,
            "mime_type": stored.mime_type,
            "size_bytes": stored.size_bytes,
            "local_cached_path": stored.local_cached_path,
            "sha256": stored.sha256,
            "source_channel": "telegram",
            "source_metadata": {"telegram_file_path": tg_file_path, "source_url": source_url},
        }

    async def _send_receipt_marker_async(self, response_handle: ResponseHandle, payload: dict[str, Any]) -> None:
        _ = payload
        if self.application is None:
            return
        chat_id = _safe_int(response_handle.reply_target.get("chat_id"))
        message_id = _safe_int(response_handle.reply_target.get("message_id"))
        if chat_id is None or message_id is None:
            return
        try:
            reaction: list[Any]
            try:
                from telegram import ReactionTypeEmoji

                reaction = [ReactionTypeEmoji("👀")]
            except Exception:
                reaction = ["👀"]
            await self.application.bot.set_message_reaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=reaction,
            )
        except Exception as exc:
            self._last_status_error = str(exc)

    async def _typing_loop(self, response_handle: ResponseHandle) -> None:
        if self.application is None:
            return
        chat_id = _safe_int(response_handle.reply_target.get("chat_id"))
        if chat_id is None:
            return
        try:
            while True:
                await self.application.bot.send_chat_action(chat_id=chat_id, action="typing")
                await asyncio.sleep(4.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_status_error = str(exc)

    def _typing_key(self, response_handle: ResponseHandle) -> str:
        chat_id = str(response_handle.reply_target.get("chat_id") or "")
        thread_id = str(response_handle.reply_target.get("thread_id") or "")
        return f"{chat_id}:{thread_id}"

    def _schedule_ordered_send(
        self,
        response_handle: ResponseHandle,
        operation: Callable[[], Awaitable[None]],
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        key = self._typing_key(response_handle)
        previous = self._send_chains.get(key)

        async def run_after_previous(previous_task: asyncio.Task[None] | None) -> None:
            if previous_task is not None:
                try:
                    await previous_task
                except asyncio.CancelledError:
                    if not previous_task.cancelled():
                        raise
                except Exception:
                    pass
            try:
                await operation()
            finally:
                if self._send_chains.get(key) is task:
                    self._send_chains.pop(key, None)

        task = loop.create_task(run_after_previous(previous))
        self._send_chains[key] = task

    def _stop_typing(self, response_handle: ResponseHandle) -> None:
        key = self._typing_key(response_handle)
        task = self._typing_tasks.pop(key, None)
        if task is not None:
            task.cancel()

    async def _send_reply_async(self, response_handle: ResponseHandle, text: str) -> None:
        if self.application is None:
            return
        chat_id = _safe_int(response_handle.reply_target.get("chat_id"))
        thread_id = _safe_int(response_handle.reply_target.get("thread_id"))
        if chat_id is None:
            return
        max_chars = int(self.endpoint.send_policy.get("max_message_chars") or 4096)
        for part in _segment_text(text, limit=max_chars):
            rendered, parse_mode = _telegram_markdown(part)
            kwargs = {
                "chat_id": chat_id,
                "text": rendered,
            }
            if thread_id is not None:
                kwargs["message_thread_id"] = thread_id
            if parse_mode is not None:
                kwargs["parse_mode"] = parse_mode
            try:
                await self.application.bot.send_message(**kwargs)
            except Exception:
                kwargs.pop("parse_mode", None)
                kwargs["text"] = part
                try:
                    await self.application.bot.send_message(**kwargs)
                except Exception as exc:
                    self.last_delivery_error = str(exc)
                    break

    async def _send_attachment_async(self, response_handle: ResponseHandle, attachment: AttachmentSpec) -> None:
        if self.application is None:
            return
        chat_id = _safe_int(response_handle.reply_target.get("chat_id"))
        thread_id = _safe_int(response_handle.reply_target.get("thread_id"))
        if chat_id is None:
            return
        path = Path(attachment.path).expanduser()
        if not path.is_file():
            self.last_delivery_error = f"attachment file not found: {path}"
            return
        kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "filename": attachment.file_name or path.name,
        }
        if attachment.caption:
            kwargs["caption"] = attachment.caption
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        try:
            with path.open("rb") as file_obj:
                await self.application.bot.send_document(document=file_obj, **kwargs)
            self.last_delivery_error = ""
        except Exception as exc:
            self.last_delivery_error = str(exc)

    async def _apply_interactive_status_async(
        self,
        response_handle: ResponseHandle,
        *,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        if self.application is None:
            return
        await self._prune_interactive_messages_async()
        spec = payload.get("spec")
        if not isinstance(spec, InteractionMessageSpec):
            return
        if kind in {"interactive_open", "interactive_update"}:
            await self._open_or_update_interaction_async(response_handle, spec=spec, allow_update=(kind == "interactive_update"))
            return
        if kind in {"interactive_resolve", "interactive_expire"}:
            await self._resolve_interaction_async(spec)

    async def _open_or_update_interaction_async(
        self,
        response_handle: ResponseHandle,
        *,
        spec: InteractionMessageSpec,
        allow_update: bool,
    ) -> None:
        if self.application is None:
            return
        await self._prune_interactive_messages_async()
        existing = self._interactive_messages.get(spec.interaction_id)
        if allow_update and existing is not None:
            if await self._edit_interaction_message_async(existing, spec=spec):
                self.remember_interaction_message(spec, existing)
                return
            self.forget_interaction_message(spec.interaction_id)
        chat_id = _safe_int(response_handle.reply_target.get("chat_id"))
        thread_id = _safe_int(response_handle.reply_target.get("thread_id"))
        if chat_id is None:
            return
        kwargs: dict[str, Any] = {"chat_id": chat_id, "text": spec.text}
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        markup = self._build_interaction_markup(spec)
        if markup is not None:
            kwargs["reply_markup"] = markup
        try:
            sent = await self.application.bot.send_message(**kwargs)
        except Exception as exc:
            self.last_delivery_error = str(exc)
            return
        self.remember_interaction_message(
            spec,
            {
                "chat_id": chat_id,
                "message_id": _safe_int(getattr(sent, "message_id", None)),
            },
        )

    async def _resolve_interaction_async(self, spec: InteractionMessageSpec) -> None:
        if self.application is None:
            return
        target = self._interactive_messages.pop(spec.interaction_id, None)
        if target is None:
            return
        await self._edit_interaction_message_async(target, spec=spec, clear_keyboard=True)

    async def _edit_interaction_message_async(
        self,
        target: dict[str, Any],
        *,
        spec: InteractionMessageSpec,
        clear_keyboard: bool = False,
    ) -> bool:
        if self.application is None:
            return False
        chat_id = _safe_int(target.get("chat_id"))
        message_id = _safe_int(target.get("message_id"))
        if chat_id is None or message_id is None:
            return False
        kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": spec.text,
            "reply_markup": None if clear_keyboard else self._build_interaction_markup(spec),
        }
        try:
            await self.application.bot.edit_message_text(**kwargs)
        except Exception as exc:
            self.last_delivery_error = str(exc)
            return False
        return True

    def _prune_interactive_messages(self, *, now: float | None = None) -> None:
        self.prune_interactive_messages(now=now)

    async def _prune_interactive_messages_async(self, *, now: float | None = None) -> None:
        current = float(now if now is not None else time.monotonic())
        stale: list[tuple[str, dict[str, Any]]] = []
        for interaction_id, metadata in list(self._interactive_messages.items()):
            expires_at_monotonic = metadata.get("expires_at_monotonic")
            if isinstance(expires_at_monotonic, (int, float)) and self.is_interaction_metadata_expired(metadata, now=current):
                target = self._interactive_messages.pop(interaction_id, None)
                if isinstance(target, dict):
                    stale.append((interaction_id, target))
        for interaction_id, target in stale:
            await self._edit_interaction_message_async(
                target,
                spec=InteractionMessageSpec(
                    interaction_id=interaction_id,
                    interaction_kind=str(target.get("interaction_kind") or ""),
                    text=self.expired_interaction_text(str(target.get("interaction_kind") or "")),
                    buttons=(),
                    expires_at=None,
                ),
                clear_keyboard=True,
            )

    def _forget_interactive_message(self, interaction_id: str) -> None:
        self.forget_interaction_message(interaction_id)

    def _expired_interaction_text(self, interaction_kind: str) -> str:
        return self.expired_interaction_text(interaction_kind)

    def _build_interaction_action_map(self, spec: InteractionMessageSpec) -> dict[str, dict[str, Any]]:
        return self.build_interaction_action_map(spec)

    def _parse_interaction_callback_data(self, data: str) -> tuple[str, str]:
        return self.parse_interaction_callback_data(data)

    async def _apply_control_catalog_async(self, payload: dict[str, Any]) -> None:
        commands = self.normalize_control_commands(payload)
        self._control_commands_manifest = commands
        if self.application is None or not commands:
            return
        bot = getattr(self.application, "bot", None)
        if bot is None:
            return
        try:
            try:
                from telegram import BotCommand
            except Exception:
                BotCommand = _FallbackBotCommand

            await bot.set_my_commands(
                commands=[
                    BotCommand(command=item["command"], description=item["description"])
                    for item in commands
                ]
            )
        except Exception as exc:
            self._last_status_error = str(exc)
            logger.exception("telegram control command menu update failed")
            return

        try:
            try:
                from telegram import MenuButtonCommands
            except Exception:
                MenuButtonCommands = _FallbackMenuButtonCommands
            await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        except Exception as exc:
            self._last_status_error = str(exc)
            logger.exception("telegram command menu button update failed")

    def _build_interaction_markup(self, spec: InteractionMessageSpec):
        if not spec.buttons:
            return None
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        except Exception:
            return None
        rows = []
        button_index = 0
        for row_items in spec.buttons:
            row = []
            for item in row_items:
                if not isinstance(item, InteractionButtonSpec):
                    continue
                label = str(item.label or "").strip()
                if not label:
                    continue
                token = self.interaction_button_token(button_index)
                button_index += 1
                row.append(
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"ix:{spec.interaction_id}:{token}",
                    )
                )
            if row:
                rows.append(row)
        if not rows:
            return None
        return InlineKeyboardMarkup(rows)

@dataclass(frozen=True)
class TelegramChannelEndpointFactory:
    channel_kind: str = "telegram"
    reload_modules: tuple[str, ...] = ("pal.channel.factory", "pal.channel.endpoints.telegram_endpoint")

    def create(self, record: Any, *, runtime_root: Any) -> TelegramChannelEndpoint | None:
        metadata = dict(record.binding_metadata or {})
        endpoint = EndpointConfig(
            endpoint_id=record.endpoint_id,
            channel_kind=record.channel_kind,
            binding_key=record.binding_key,
            send_policy={
                "max_message_chars": int(record.max_message_chars or 4096),
                "preferred_parse_mode": record.preferred_parse_mode or "MarkdownV2",
                "segment_by_default": bool(record.segment_by_default),
                "preserve_code_blocks": bool(record.preserve_code_blocks),
            },
        )
        runtime_endpoint = TelegramChannelEndpoint(
            endpoint=endpoint,
            runtime_root=runtime_root,
            bot_token=str(metadata.get("bot_token") or ""),
            base_url=str(metadata.get("base_url") or "https://api.telegram.org"),
            poll_timeout_seconds=int(metadata.get("poll_timeout_seconds") or 30),
            allowed_updates=tuple(metadata.get("allowed_updates") or ("message", "edited_message", "callback_query")),
            binding=TelegramBinding.parse(record.binding_key or ""),
        )
        runtime_endpoint._poll_error_stale_threshold_seconds = float(metadata.get("poll_error_stale_threshold_seconds") or 10.0)
        runtime_endpoint._application_shutdown_timeout_seconds = float(metadata.get("application_shutdown_timeout_seconds") or 5.0)
        runtime_endpoint.enabled = bool(record.enabled)
        runtime_endpoint.attached = record.detached_at is None
        runtime_endpoint.paired = bool(record.binding_key)
        return runtime_endpoint
