from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable

from pal.channel.channel_endpoint_queue_base import ChannelEndpointQueueBase
from pal.channel.contracts import (
    ChannelDeliveryError,
    ChannelMessage,
    ChannelStreamUpdate,
    EndpointConfig,
    QueuedAttachment,
    QueuedReply,
    QueuedStreamUpdate,
    ResponseHandle,
)
from pal.control.contracts import InteractionButtonSpec, InteractionMessageSpec, InteractionResult
from pal.foundation.artifact import ArtifactIngestor, StoredArtifact
from pal.foundation import AttachmentSpec, EventEnvelope
from pal.shared import ChannelStreamUpdateKind, EventKind, LLMFinishReason, SourceKind

from .interaction_store import TelegramInteractionStore


logger = logging.getLogger(__name__)

_TELEGRAM_MAX_MESSAGE_UTF16 = 4096


@dataclass(frozen=True)
class _FallbackBotCommand:
    command: str
    description: str


@dataclass(frozen=True)
class _FallbackMenuButtonCommands:
    type: str = "commands"


@dataclass(frozen=True)
class _TelegramTextSegment:
    rendered_text: str
    fallback_text: str
    parse_mode: str | None


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
    effective_limit = max(int(limit), 1)

    parts: list[str] = []
    remaining = stripped
    while remaining:
        hard_end = _utf16_prefix_end(remaining, limit=effective_limit)
        if hard_end >= len(remaining):
            parts.append(remaining)
            break
        split_at = remaining.rfind("\n\n", 0, hard_end + 1)
        if split_at <= 0:
            split_at = remaining.rfind("\n", 0, hard_end + 1)
        if split_at <= 0:
            split_at = hard_end
        chunk = remaining[:split_at].strip()
        if not chunk:
            chunk = remaining[:hard_end].strip()
            split_at = hard_end
        parts.append(chunk)
        remaining = remaining[split_at:].strip()
    return [part for part in parts if part]


def _utf16_prefix_end(text: str, *, limit: int) -> int:
    """Return the largest code-point boundary whose prefix fits the UTF-16 limit."""

    consumed_units = 0
    for index, character in enumerate(text):
        character_units = 2 if ord(character) > 0xFFFF else 1
        if consumed_units + character_units > limit:
            # A policy limit of one UTF-16 unit cannot contain an astral code
            # point. Keep it intact rather than corrupting the text.
            return index or 1
        consumed_units += character_units
    return len(text)


def _truncate_telegram_text(text: str, *, limit: int) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return "Checklist updated."
    end = _utf16_prefix_end(normalized, limit=max(int(limit), 1))
    if end >= len(normalized):
        return normalized
    suffix = "\n…"
    suffix_units = len(suffix.encode("utf-16-le")) // 2
    prefix_end = _utf16_prefix_end(normalized, limit=max(int(limit) - suffix_units, 1))
    return f"{normalized[:prefix_end].rstrip()}{suffix}"


def _telegram_text_segments(text: str, *, limit: int) -> list[_TelegramTextSegment]:
    """Render and split one completed reply into independently sendable messages."""

    normalized = _flatten_gfm_tables_for_telegram(str(text or "")).strip()
    if not normalized:
        return []
    effective_limit = max(min(int(limit), _TELEGRAM_MAX_MESSAGE_UTF16), 1)
    try:
        import telegramify_markdown

        plain_text, entities = telegramify_markdown.convert(normalized)
        chunks = telegramify_markdown.split_entities(
            plain_text,
            entities,
            max_utf16_len=effective_limit,
        )
        rendered = [
            _TelegramTextSegment(
                rendered_text=telegramify_markdown.entities_to_markdownv2(chunk_text, chunk_entities),
                fallback_text=chunk_text,
                parse_mode="MarkdownV2",
            )
            for chunk_text, chunk_entities in chunks
            if str(chunk_text or "").strip()
        ]
        if rendered:
            return rendered
    except Exception:
        logger.debug("telegram entity-aware rendering failed; using plain-text segmentation", exc_info=True)

    return [
        _TelegramTextSegment(
            rendered_text=part,
            fallback_text=part,
            parse_mode=None,
        )
        for part in _segment_text(normalized, limit=effective_limit)
    ]


def _safe_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except Exception:
        return None


def _telegram_message_is_not_modified(exc: Exception | str) -> bool:
    normalized = " ".join(str(exc or "").lower().replace("_", " ").split())
    return "message is not modified" in normalized


def _telegram_interaction_target_is_stale(error: Exception | str) -> bool:
    """Return whether retrying the existing Telegram message cannot succeed."""

    normalized = " ".join(str(error or "").lower().replace("_", " ").split())
    stale_markers = (
        "message to edit not found",
        "message to delete not found",
        "message can't be edited",
        "message can not be edited",
        "message cannot be edited",
        "message id invalid",
    )
    return any(marker in normalized for marker in stale_markers)


@dataclass
class TelegramChannelEndpoint(ChannelEndpointQueueBase):
    runtime_root: Any = None
    data_root: Path | None = None
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
    _pending_reply_deliveries: dict[str, tuple[QueuedReply, asyncio.Task[None]]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _pending_attachment_deliveries: dict[str, tuple[QueuedAttachment, asyncio.Task[None]]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _pending_stream_deliveries: dict[str, tuple[QueuedStreamUpdate, asyncio.Task[None]]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _tracked_delivery_tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False, repr=False)
    drop_pending_updates_on_start: bool = True
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
    _interaction_store: TelegramInteractionStore | None = field(default=None, init=False, repr=False)
    _turn_stream_text: dict[tuple[str, str, str, str], str] = field(default_factory=dict, init=False, repr=False)
    _tagged_message_targets: dict[tuple[str, str, str], dict[str, int]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not self.binding.chat_id and not self.binding.user_id and self.endpoint.binding_key:
            self.binding = TelegramBinding.parse(self.endpoint.binding_key)
        if self.data_root is None and self.runtime_root is not None:
            self.data_root = (
                Path(self.runtime_root)
                / "data"
                / "channel"
                / self.endpoint.endpoint_id
            )
        if self.data_root is not None:
            self._interaction_store = TelegramInteractionStore(self.data_root)

    def supports_stream_delivery(self) -> bool:
        return True

    def prepare_replacement(self, old_endpoint: ChannelEndpointQueueBase) -> None:
        _ = old_endpoint
        # A hot generation swap must consume updates that arrived while the old
        # poller was stopping.  Only a genuinely fresh installation may discard
        # Telegram's pre-existing update backlog.
        self.drop_pending_updates_on_start = False

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
        await self._recover_pending_deliveries_async()
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
        self._turn_stream_text.clear()

    async def _recover_pending_deliveries_async(self) -> None:
        deliveries = [
            *self._pending_reply_deliveries.values(),
            *self._pending_attachment_deliveries.values(),
            *self._pending_stream_deliveries.values(),
        ]
        tasks = list(dict.fromkeys(task for _, task in deliveries))
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for item, task in self._pending_reply_deliveries.values():
            if task.cancelled() or task.exception() is not None:
                self.outbox.append(
                    QueuedReply(
                        reply_id=item.reply_id,
                        response_handle=item.response_handle,
                        endpoint=item.endpoint,
                        text=item.text,
                        tag=item.tag,
                        payload=dict(item.payload),
                        attempts=item.attempts + 1,
                    )
                )
        for item, task in self._pending_attachment_deliveries.values():
            if task.cancelled() or task.exception() is not None:
                self.attachment_outbox.append(
                    QueuedAttachment(
                        attachment_id=item.attachment_id,
                        response_handle=item.response_handle,
                        endpoint=item.endpoint,
                        attachment=item.attachment,
                        attempts=item.attempts + 1,
                    )
                )
        for item, task in self._pending_stream_deliveries.values():
            if task.cancelled() or task.exception() is not None:
                self.stream_update_outbox.append(
                    replace(item, attempts=item.attempts + 1)
                )
        self._pending_reply_deliveries.clear()
        self._pending_attachment_deliveries.clear()
        self._pending_stream_deliveries.clear()
        self._tracked_delivery_tasks.clear()

    def send_reply(self, response_handle: ResponseHandle, text: str) -> None:
        if self.application is None:
            raise ChannelDeliveryError("telegram application not running", permanent=False)
        self._schedule_ordered_send(response_handle, lambda: self._send_reply_async(response_handle, text))

    def send_channel_message(self, response_handle: ResponseHandle, message: ChannelMessage) -> None:
        self._schedule_ordered_send(
            response_handle,
            lambda: self._send_channel_message_async(response_handle, message),
        )

    def send_attachment(self, response_handle: ResponseHandle, attachment: AttachmentSpec) -> None:
        self._schedule_ordered_send(response_handle, lambda: self._send_attachment_async(response_handle, attachment))

    def flush_outbox(self) -> list[EventEnvelope]:
        pending = list(self.outbox)
        self.outbox.clear()
        emitted = self._collect_reply_delivery_results()
        if not pending:
            return emitted
        for item in pending:
            unavailable_reason = self._delivery_unavailable_reason()
            if unavailable_reason:
                self.outbox.append(
                    QueuedReply(
                        reply_id=item.reply_id,
                        response_handle=item.response_handle,
                        endpoint=item.endpoint,
                        text=item.text,
                        tag=item.tag,
                        payload=dict(item.payload),
                        attempts=item.attempts + 1,
                    )
                )
                failure = self._reply_failure_event_once(item, unavailable_reason, permanent=False)
                if failure is not None:
                    emitted.append(failure)
                continue
            task = self._schedule_ordered_send(
                item.response_handle,
                lambda item=item: self._send_channel_message_async(item.response_handle, item.message),
            )
            if task is None:
                self.outbox.append(item)
                failure = self._reply_failure_event_once(item, "telegram event loop not running", permanent=False)
                if failure is not None:
                    emitted.append(failure)
                continue
            self._tracked_delivery_tasks.add(task)
            self._pending_reply_deliveries[item.reply_id] = (item, task)
        return emitted

    def flush_attachment_outbox(self) -> list[EventEnvelope]:
        pending = list(self.attachment_outbox)
        self.attachment_outbox.clear()
        emitted = self._collect_attachment_delivery_results()
        if not pending:
            return emitted
        for item in pending:
            unavailable_reason = self._delivery_unavailable_reason()
            if unavailable_reason:
                self.attachment_outbox.append(
                    QueuedAttachment(
                        attachment_id=item.attachment_id,
                        response_handle=item.response_handle,
                        endpoint=item.endpoint,
                        attachment=item.attachment,
                        attempts=item.attempts + 1,
                    )
                )
                failure = self._delivery_failure_event_once(
                    delivery_id=item.attachment_id,
                    attempts=item.attempts + 1,
                    reason=unavailable_reason,
                    permanent=False,
                )
                if failure is not None:
                    emitted.append(failure)
                continue
            task = self._schedule_ordered_send(
                item.response_handle,
                lambda item=item: self._send_attachment_async(item.response_handle, item.attachment),
            )
            if task is None:
                self.attachment_outbox.append(item)
                continue
            self._tracked_delivery_tasks.add(task)
            self._pending_attachment_deliveries[item.attachment_id] = (item, task)
        return emitted

    def flush_stream_update_outbox(self) -> list[EventEnvelope]:
        pending = list(self.stream_update_outbox)
        self.stream_update_outbox.clear()
        emitted = self._collect_stream_delivery_results()
        for item in pending:
            update = item.update
            if update.kind == ChannelStreamUpdateKind.DONE and not update.text:
                buffered = self._turn_stream_text.get(
                    self._turn_stream_key(item.response_handle),
                    "",
                )
                if buffered:
                    update = replace(update, text=buffered)
                    item = replace(item, update=update)
            task = self.send_stream_update(item.response_handle, update)
            if task is None:
                continue
            self._tracked_delivery_tasks.add(task)
            self._pending_stream_deliveries[item.update_id] = (item, task)
        return emitted

    def _delivery_unavailable_reason(self) -> str:
        if not self.attached or not self.enabled:
            return "endpoint_unavailable"
        if self.application is None:
            return "telegram application not running"
        return ""

    def has_queued_replies(self) -> bool:
        return bool(self.outbox or self._pending_reply_deliveries)

    def has_queued_stream_updates(self) -> bool:
        return bool(self.stream_update_outbox or self._pending_stream_deliveries)

    def has_queued_attachments(self) -> bool:
        return bool(self.attachment_outbox or self._pending_attachment_deliveries)

    def _collect_reply_delivery_results(self) -> list[EventEnvelope]:
        emitted: list[EventEnvelope] = []
        for reply_id, (item, task) in list(self._pending_reply_deliveries.items()):
            if not task.done():
                continue
            self._pending_reply_deliveries.pop(reply_id, None)
            self._tracked_delivery_tasks.discard(task)
            try:
                task.result()
            except asyncio.CancelledError:
                self.outbox.append(item)
                continue
            except Exception as exc:
                permanent = isinstance(exc, ChannelDeliveryError) and bool(exc.permanent)
                if not permanent:
                    self.outbox.append(
                        QueuedReply(
                            reply_id=item.reply_id,
                            response_handle=item.response_handle,
                            endpoint=item.endpoint,
                            text=item.text,
                            tag=item.tag,
                            payload=dict(item.payload),
                            attempts=item.attempts + 1,
                        )
                    )
                failure = self._reply_failure_event_once(item, str(exc), permanent=permanent)
                if failure is not None:
                    emitted.append(failure)
                continue
            self.last_delivery_error = ""
            self._reported_reply_failures.pop(reply_id, None)
            emitted.append(self._delivery_event(reply_id))
        return emitted

    def _collect_attachment_delivery_results(self) -> list[EventEnvelope]:
        emitted: list[EventEnvelope] = []
        for attachment_id, (item, task) in list(self._pending_attachment_deliveries.items()):
            if not task.done():
                continue
            self._pending_attachment_deliveries.pop(attachment_id, None)
            self._tracked_delivery_tasks.discard(task)
            try:
                task.result()
            except asyncio.CancelledError:
                self.attachment_outbox.append(item)
                continue
            except Exception as exc:
                permanent = isinstance(exc, ChannelDeliveryError) and bool(exc.permanent)
                if not permanent:
                    self.attachment_outbox.append(
                        QueuedAttachment(
                            attachment_id=item.attachment_id,
                            response_handle=item.response_handle,
                            endpoint=item.endpoint,
                            attachment=item.attachment,
                            attempts=item.attempts + 1,
                        )
                    )
                failure = self._delivery_failure_event_once(
                    delivery_id=item.attachment_id,
                    attempts=item.attempts + 1,
                    reason=str(exc),
                    permanent=permanent,
                )
                if failure is not None:
                    emitted.append(failure)
                continue
            self.last_delivery_error = ""
            self._reported_reply_failures.pop(attachment_id, None)
            emitted.append(self._delivery_event(attachment_id))
        return emitted

    def _collect_stream_delivery_results(self) -> list[EventEnvelope]:
        emitted: list[EventEnvelope] = []
        for update_id, (item, task) in list(self._pending_stream_deliveries.items()):
            if not task.done():
                continue
            self._pending_stream_deliveries.pop(update_id, None)
            self._tracked_delivery_tasks.discard(task)
            try:
                task.result()
            except asyncio.CancelledError:
                self.stream_update_outbox.append(item)
                continue
            except Exception as exc:
                permanent = isinstance(exc, ChannelDeliveryError) and bool(exc.permanent)
                if not permanent:
                    self.stream_update_outbox.append(
                        replace(item, attempts=item.attempts + 1)
                    )
                failure = self._delivery_failure_event_once(
                    delivery_id=item.update_id,
                    attempts=item.attempts + 1,
                    reason=str(exc),
                    permanent=permanent,
                )
                if failure is not None:
                    emitted.append(failure)
                continue
            self.last_delivery_error = ""
            self._reported_reply_failures.pop(update_id, None)
        return emitted

    def _delivery_event(self, delivery_id: str) -> EventEnvelope:
        return EventEnvelope(
            event_kind=EventKind.REPLY_DELIVERED,
            source_kind=SourceKind.CHANNEL,
            payload={
                "reply_id": delivery_id,
                "endpoint_id": self.endpoint.endpoint_id,
                "channel_kind": self.endpoint.channel_kind,
            },
        )

    def send_status(self, response_handle: ResponseHandle, kind: str, payload: dict[str, Any]) -> None:
        loop = asyncio.get_running_loop()
        if kind == "control_catalog":
            loop.create_task(self._apply_control_catalog_async(payload))
            return
        if kind in {"interactive_open", "interactive_update", "interactive_resolve", "interactive_expire"}:
            self._schedule_ordered_send(
                response_handle,
                lambda: self._apply_interactive_status_async(
                    response_handle,
                    kind=kind,
                    payload=payload,
                ),
            )
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

    def send_stream_update(
        self,
        response_handle: ResponseHandle,
        update: ChannelStreamUpdate,
    ) -> asyncio.Task[None] | None:
        key = self._turn_stream_key(response_handle)
        if update.kind == ChannelStreamUpdateKind.TEXT_DELTA:
            self._turn_stream_text[key] = f"{self._turn_stream_text.get(key, '')}{update.text}"
            return None
        if update.kind == ChannelStreamUpdateKind.PROGRESS:
            text = str(update.text or "").strip()
            if text:
                return self._schedule_ordered_send(
                    response_handle,
                    lambda: self._send_reply_async(response_handle, text),
                )
            return None
        if update.kind == ChannelStreamUpdateKind.MESSAGE:
            message = update.message or ChannelMessage(text=update.text)
            return self._schedule_ordered_send(
                response_handle,
                lambda: self._send_channel_message_async(response_handle, message),
            )
        if update.kind == ChannelStreamUpdateKind.DONE:
            buffered = self._turn_stream_text.pop(key, "")
            if str(update.finish_reason or "") in {
                LLMFinishReason.TOOL_CALLS.value,
                LLMFinishReason.COMPACT_REQUIRED.value,
            }:
                return None
            text = str(update.text or buffered).strip()
            if text:
                return self._schedule_ordered_send(
                    response_handle,
                    lambda: self._send_reply_async(response_handle, text),
                )
            return None
        if update.kind == ChannelStreamUpdateKind.ERROR:
            self._turn_stream_text.pop(key, None)
            text = str(update.error_text or update.text or "").strip()
            if text:
                return self._schedule_ordered_send(
                    response_handle,
                    lambda: self._send_reply_async(response_handle, text),
                )
        return None

    def abort_stream(self, response_handle: ResponseHandle, *, reason: str = "interrupted") -> None:
        self._turn_stream_text.pop(self._turn_stream_key(response_handle), None)
        super().abort_stream(response_handle, reason=reason)

    @staticmethod
    def _turn_stream_key(response_handle: ResponseHandle) -> tuple[str, str, str, str]:
        target = response_handle.reply_target
        return (
            str(response_handle.endpoint_id or ""),
            str(target.get("chat_id") or ""),
            str(target.get("message_id") or ""),
            str(target.get("thread_id") or ""),
        )

    def prepare_final_reply(self, response_handle: ResponseHandle, text: str) -> str | None:
        # Core marks the assistant text attached to a tool-call round
        # explicitly. This is the only reply Telegram suppresses. A tool echo
        # is also non-terminal, but is independent user-visible progress and
        # must still be delivered. Terminal replies always win regardless of
        # stream flush timing.
        if bool(response_handle.reply_target.get("_pal_stream_companion")):
            return None
        return text

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

    def validate_replacement_startup(self) -> None:
        if not self._can_start() or self._polling_running:
            return
        raise ChannelDeliveryError(
            self._last_poll_error or "telegram polling did not start",
            permanent=False,
            reason="telegram_startup_failed",
        )

    def inspect_backlog(self) -> dict[str, Any]:
        payload = super().inspect_backlog()
        payload["typing_sessions"] = sum(1 for task in self._typing_tasks.values() if not task.done())
        payload["reply_deliveries_in_flight"] = len(self._pending_reply_deliveries)
        payload["attachment_deliveries_in_flight"] = len(self._pending_attachment_deliveries)
        payload["stream_deliveries_in_flight"] = len(self._pending_stream_deliveries)
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
                        drop_pending_updates=bool(
                            first_attempt and self.drop_pending_updates_on_start
                        ),
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
                    self.drop_pending_updates_on_start = False
                    logger.info("telegram polling started successfully")
                    if self._control_commands_manifest:
                        await self._apply_control_catalog_async({"commands": list(self._control_commands_manifest)})
                except Exception as exc:
                    self.drop_pending_updates_on_start = False
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
        metadata = self._restore_interaction(interaction_id)
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
            if self._interaction_store is not None:
                self._interaction_store.set_state(interaction_id, "expired")
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

    async def _extract_attachments(self, message: Any, *, chat_id: int | None) -> list[StoredArtifact]:
        attachments: list[StoredArtifact] = []
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
    ) -> StoredArtifact | None:
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
        # Preserve the trusted StoredArtifact object until core ingress claims
        # it. Flattening it to a user-shaped dict would lose the ownership
        # proof and leave the provider download cache outside artifact RAII.
        return replace(
            stored,
            metadata={
                "attachment_id": f"telegram_{provider_file_id}",
                "provider_file_id": provider_file_id,
                "file_name": file_name,
                "source_channel": "telegram",
                "source_metadata": {
                    "telegram_file_path": tg_file_path,
                    "source_url": source_url,
                },
            },
        )

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
    ) -> asyncio.Task[None] | None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        key = self._typing_key(response_handle)
        previous = self._send_chains.get(key)

        async def run_after_previous(previous_task: asyncio.Task[None] | None) -> None:
            try:
                if previous_task is not None:
                    try:
                        await previous_task
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        raise ChannelDeliveryError(
                            "previous telegram delivery in this conversation failed",
                            permanent=False,
                            reason="telegram_send_chain_failed",
                        ) from exc
                await operation()
            finally:
                if self._send_chains.get(key) is task:
                    self._send_chains.pop(key, None)

        task = loop.create_task(run_after_previous(previous))
        task.add_done_callback(self._on_send_task_done)
        self._send_chains[key] = task
        return task

    def _on_send_task_done(self, task: asyncio.Task[None]) -> None:
        self._notify_ready()
        if task in self._tracked_delivery_tasks:
            return
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self.last_delivery_error = str(exc)

    def _stop_typing(self, response_handle: ResponseHandle) -> None:
        key = self._typing_key(response_handle)
        task = self._typing_tasks.pop(key, None)
        if task is not None:
            task.cancel()

    async def _send_reply_async(self, response_handle: ResponseHandle, text: str) -> None:
        if self.application is None:
            raise ChannelDeliveryError(
                "telegram application not running",
                permanent=False,
                reason="telegram_not_running",
            )
        chat_id = _safe_int(response_handle.reply_target.get("chat_id"))
        thread_id = _safe_int(response_handle.reply_target.get("thread_id"))
        if chat_id is None:
            raise ChannelDeliveryError(
                "telegram reply target is missing chat_id",
                permanent=True,
                reason="telegram_target_invalid",
            )
        max_chars = int(self.endpoint.send_policy.get("max_message_chars") or _TELEGRAM_MAX_MESSAGE_UTF16)
        for segment in _telegram_text_segments(text, limit=max_chars):
            kwargs = {
                "chat_id": chat_id,
                "text": segment.rendered_text,
            }
            if thread_id is not None:
                kwargs["message_thread_id"] = thread_id
            if segment.parse_mode is not None:
                kwargs["parse_mode"] = segment.parse_mode
            try:
                await self.application.bot.send_message(**kwargs)
            except Exception as exc:
                if segment.parse_mode is None:
                    self.last_delivery_error = str(exc)
                    raise ChannelDeliveryError(
                        str(exc) or "telegram message delivery failed",
                        permanent=False,
                        reason="telegram_send_failed",
                    ) from exc
                kwargs.pop("parse_mode", None)
                kwargs["text"] = segment.fallback_text
                try:
                    await self.application.bot.send_message(**kwargs)
                except Exception as exc:
                    self.last_delivery_error = str(exc)
                    raise ChannelDeliveryError(
                        str(exc) or "telegram message delivery failed",
                        permanent=False,
                        reason="telegram_send_failed",
                    ) from exc
        self.last_delivery_error = ""

    async def _send_channel_message_async(
        self,
        response_handle: ResponseHandle,
        message: ChannelMessage,
    ) -> None:
        if message.tag != "checklist":
            await self._send_reply_async(response_handle, message.text)
            return
        await self._send_checklist_message_async(response_handle, message)

    async def _send_checklist_message_async(
        self,
        response_handle: ResponseHandle,
        message: ChannelMessage,
    ) -> None:
        if self.application is None:
            raise ChannelDeliveryError(
                "telegram application not running",
                permanent=False,
                reason="telegram_not_running",
            )
        chat_id = _safe_int(response_handle.reply_target.get("chat_id"))
        thread_id = _safe_int(response_handle.reply_target.get("thread_id"))
        if chat_id is None:
            raise ChannelDeliveryError(
                "telegram reply target is missing chat_id",
                permanent=True,
                reason="telegram_target_invalid",
            )
        key = (str(chat_id), str(thread_id or ""), "checklist")
        action = str(message.payload.get("action") or "update").strip().lower()
        target = self._tagged_message_targets.get(key)

        if action == "clear":
            if target is None:
                await self._send_reply_async(response_handle, message.text)
                return
            try:
                await self.application.bot.delete_message(
                    chat_id=int(target["chat_id"]),
                    message_id=int(target["message_id"]),
                )
            except Exception as exc:
                if _telegram_interaction_target_is_stale(exc):
                    self._tagged_message_targets.pop(key, None)
                    self.last_delivery_error = ""
                    return
                self.last_delivery_error = str(exc)
                raise ChannelDeliveryError(
                    str(exc) or "telegram checklist delete failed",
                    permanent=False,
                    reason="telegram_delete_failed",
                ) from exc
            self._tagged_message_targets.pop(key, None)
            self.last_delivery_error = ""
            return

        max_chars = max(
            1,
            min(
                int(self.endpoint.send_policy.get("max_message_chars") or _TELEGRAM_MAX_MESSAGE_UTF16),
                _TELEGRAM_MAX_MESSAGE_UTF16,
            ),
        )
        text = _truncate_telegram_text(message.text, limit=max_chars)
        if target is not None:
            try:
                await self.application.bot.edit_message_text(
                    chat_id=int(target["chat_id"]),
                    message_id=int(target["message_id"]),
                    text=text,
                )
                self.last_delivery_error = ""
                return
            except Exception as exc:
                if _telegram_message_is_not_modified(exc):
                    self.last_delivery_error = ""
                    return
                if not _telegram_interaction_target_is_stale(exc):
                    self.last_delivery_error = str(exc)
                    raise ChannelDeliveryError(
                        str(exc) or "telegram checklist edit failed",
                        permanent=False,
                        reason="telegram_edit_failed",
                    ) from exc
                self._tagged_message_targets.pop(key, None)

        kwargs: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        try:
            sent = await self.application.bot.send_message(**kwargs)
        except Exception as exc:
            self.last_delivery_error = str(exc)
            raise ChannelDeliveryError(
                str(exc) or "telegram checklist delivery failed",
                permanent=False,
                reason="telegram_send_failed",
            ) from exc
        message_id = _safe_int(getattr(sent, "message_id", None))
        if message_id is not None:
            self._tagged_message_targets[key] = {
                "chat_id": chat_id,
                "message_id": message_id,
            }
        self.last_delivery_error = ""

    async def _send_attachment_async(self, response_handle: ResponseHandle, attachment: AttachmentSpec) -> None:
        if self.application is None:
            raise ChannelDeliveryError(
                "telegram application not running",
                permanent=False,
                reason="telegram_not_running",
            )
        chat_id = _safe_int(response_handle.reply_target.get("chat_id"))
        thread_id = _safe_int(response_handle.reply_target.get("thread_id"))
        if chat_id is None:
            raise ChannelDeliveryError(
                "telegram reply target is missing chat_id",
                permanent=True,
                reason="telegram_target_invalid",
            )
        path = Path(attachment.path).expanduser()
        if not path.is_file():
            self.last_delivery_error = f"attachment file not found: {path}"
            raise ChannelDeliveryError(
                self.last_delivery_error,
                permanent=True,
                reason="attachment_not_found",
            )
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
            raise ChannelDeliveryError(
                str(exc) or "telegram attachment delivery failed",
                permanent=False,
                reason="telegram_send_failed",
            ) from exc

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
        if kind == "interactive_expire" and bool(payload.get("delete")):
            await self._delete_interaction_async(spec)
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
        existing = self._restore_interaction(spec.interaction_id)
        max_chars = max(int(self.endpoint.send_policy.get("max_message_chars") or 4096), 1)
        if allow_update and existing is not None and len(spec.text) <= max_chars:
            if await self._edit_interaction_message_async(existing, spec=spec):
                self._remember_interaction(spec, existing)
                return
            if not _telegram_interaction_target_is_stale(self.last_delivery_error):
                # A transient edit failure must not create a second active
                # keyboard. Keep the durable target and let a later update
                # retry the same message.
                return
            super().forget_interaction_message(spec.interaction_id)
        chat_id = _safe_int(response_handle.reply_target.get("chat_id"))
        thread_id = _safe_int(response_handle.reply_target.get("thread_id"))
        if chat_id is None:
            return
        markup = self._build_interaction_markup(spec)
        sent = None
        parts = _segment_text(spec.text, limit=max_chars)
        for index, part in enumerate(parts):
            kwargs: dict[str, Any] = {"chat_id": chat_id, "text": part}
            if thread_id is not None:
                kwargs["message_thread_id"] = thread_id
            if index == len(parts) - 1 and markup is not None:
                kwargs["reply_markup"] = markup
            try:
                sent = await self.application.bot.send_message(**kwargs)
            except Exception as exc:
                self.last_delivery_error = str(exc)
                return
        if sent is None:
            return
        self._remember_interaction(
            spec,
            {
                "chat_id": chat_id,
                "message_id": _safe_int(getattr(sent, "message_id", None)),
            },
        )

    async def _resolve_interaction_async(self, spec: InteractionMessageSpec) -> None:
        if self.application is None:
            return
        target = self._restore_interaction(spec.interaction_id)
        if target is None:
            return
        await self._edit_interaction_message_async(target, spec=spec, clear_keyboard=True)
        super().forget_interaction_message(spec.interaction_id)
        if self._interaction_store is not None:
            self._interaction_store.set_state(spec.interaction_id, "resolved")

    async def _delete_interaction_async(self, spec: InteractionMessageSpec) -> None:
        if self.application is None:
            return
        target = self._restore_interaction(spec.interaction_id)
        if target is None:
            return
        chat_id = _safe_int(target.get("chat_id"))
        message_id = _safe_int(target.get("message_id"))
        if chat_id is not None and message_id is not None:
            try:
                await self.application.bot.delete_message(
                    chat_id=chat_id,
                    message_id=message_id,
                )
            except Exception as exc:
                if not _telegram_interaction_target_is_stale(exc):
                    self.last_delivery_error = str(exc)
                    return
        self.last_delivery_error = ""
        super().forget_interaction_message(spec.interaction_id)
        if self._interaction_store is not None:
            self._interaction_store.set_state(spec.interaction_id, "expired")

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
            if _telegram_message_is_not_modified(exc):
                self.last_delivery_error = ""
                return True
            self.last_delivery_error = str(exc)
            return False
        self.last_delivery_error = ""
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
            if self._interaction_store is not None:
                self._interaction_store.set_state(interaction_id, "expired")

    def _forget_interactive_message(self, interaction_id: str) -> None:
        super().forget_interaction_message(interaction_id)
        if self._interaction_store is not None:
            self._interaction_store.set_state(interaction_id, "resolved")

    def _expired_interaction_text(self, interaction_kind: str) -> str:
        return self.expired_interaction_text(interaction_kind)

    def _build_interaction_action_map(self, spec: InteractionMessageSpec) -> dict[str, dict[str, Any]]:
        return self.build_interaction_action_map(spec)

    def _parse_interaction_callback_data(self, data: str) -> tuple[str, str]:
        return self.parse_interaction_callback_data(data)

    def _remember_interaction(
        self,
        spec: InteractionMessageSpec,
        target: dict[str, Any],
    ) -> None:
        super().remember_interaction_message(spec, target)
        if self._interaction_store is None:
            return
        metadata = self._interactive_messages.get(spec.interaction_id) or {}
        self._interaction_store.put_open(
            interaction_id=spec.interaction_id,
            interaction_kind=spec.interaction_kind,
            target=dict(target),
            actions=dict(metadata.get("actions") or {}),
            expires_at=spec.expires_at,
        )

    def _restore_interaction(self, interaction_id: str) -> dict[str, Any] | None:
        metadata = self._interactive_messages.get(interaction_id)
        if metadata is not None:
            return metadata
        if self._interaction_store is None:
            return None
        stored = self._interaction_store.get_open(interaction_id)
        if stored is None:
            return None
        expires_at_monotonic = None
        if stored.expires_at:
            expires_at_monotonic = self.interaction_expiry_monotonic(
                InteractionMessageSpec(
                    interaction_id=stored.interaction_id,
                    interaction_kind=stored.interaction_kind,
                    expires_at=stored.expires_at,
                )
            )
        metadata = {
            **dict(stored.target),
            "interaction_kind": stored.interaction_kind,
            "expires_at_monotonic": expires_at_monotonic,
            "actions": dict(stored.actions),
        }
        self._interactive_messages[interaction_id] = metadata
        return metadata

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
    reload_modules: tuple[str, ...] = ()

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
            data_root=(
                Path(runtime_root)
                / "data"
                / "channel"
                / str(record.endpoint_id)
            ),
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
