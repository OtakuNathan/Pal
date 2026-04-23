from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from pal.channel.channel_endpoint_queue_base import ChannelEndpointQueueBase
from pal.channel.contracts import EndpointConfig, ResponseHandle
from pal.foundation.artifact import ArtifactIngestor
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
    try:
        import telegramify_markdown

        return telegramify_markdown.markdownify(text), "MarkdownV2"
    except Exception:
        return text, None


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
    _control_prompt_messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    _control_commands_manifest: list[dict[str, str]] = field(default_factory=list)
    _polling_running: bool = False
    _authorized: bool = False
    _last_poll_error: str = ""
    _last_status_error: str = ""
    _reconnect_delays: tuple[float, ...] = (1.0, 3.0, 10.0, 30.0)
    _reconnect_attempts: int = 0
    _reconnecting: bool = False

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

    def send_reply(self, response_handle: ResponseHandle, text: str) -> None:
        loop = asyncio.get_running_loop()
        loop.create_task(self._send_reply_async(response_handle, text))

    def send_status(self, response_handle: ResponseHandle, kind: str, payload: dict[str, Any]) -> None:
        loop = asyncio.get_running_loop()
        if kind == "control_catalog":
            loop.create_task(self._apply_control_catalog_async(payload))
            return
        if kind == "control_panel":
            loop.create_task(self._send_control_message_async(response_handle, payload, store_request_id=""))
            return
        if kind == "control_prompt":
            loop.create_task(
                self._send_control_message_async(
                    response_handle,
                    payload,
                    store_request_id=str(payload.get("request_id") or ""),
                )
            )
            return
        if kind == "control_request_expired":
            loop.create_task(self._expire_control_prompt_async(payload))
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
            return {"chat_id": chat_id}
        return {}

    def inspect_health(self) -> dict[str, Any]:
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
                        drop_pending_updates=True,
                        allowed_updates=list(self.allowed_updates),
                    )
                    self._authorized = True
                    self._polling_running = True
                    self._last_poll_error = ""
                    self._reconnect_attempts = 0
                    self._reconnecting = False
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
                            self._polling_running = False
                            break
                        await asyncio.sleep(0.5)
                except asyncio.CancelledError:
                    raise
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
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            return

    async def _shutdown_application(self, app: Any) -> None:
        with contextlib.suppress(Exception):
            if app.updater is not None:
                await app.updater.stop()
        with contextlib.suppress(Exception):
            if app.running:
                await app.stop()
        with contextlib.suppress(Exception):
            await app.shutdown()

    def _build_application(self):
        from telegram.ext import ApplicationBuilder, TypeHandler
        from telegram import Update

        builder = ApplicationBuilder().token(self.bot_token).concurrent_updates(True)
        builder = builder.read_timeout(30).write_timeout(30).connect_timeout(10).pool_timeout(30)
        if self.proxy_url:
            builder = builder.proxy(self.proxy_url).get_updates_proxy(self.proxy_url)
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

    async def _on_update(self, update: Any, context: Any) -> None:
        _ = context
        callback_payload = await self._control_payload_from_update(update)
        if callback_payload is not None:
            self.accept_raw(
                callback_payload,
                event_kind="slash_command",
                correlation_id=str(callback_payload.get("source_metadata", {}).get("telegram_callback_id") or ""),
                reply_target={
                    "chat_id": callback_payload["chat_id"],
                    "message_id": callback_payload["message_id"],
                    "thread_id": callback_payload.get("thread_id") or "",
                },
            )
            return
        payload = await self._payload_from_update(update)
        if payload is None:
            return
        envelope = self.accept_raw(
            payload,
            event_kind="user.message",
            correlation_id=str(payload.get("message_id") or ""),
            reply_target={
                "chat_id": payload["chat_id"],
                "message_id": payload["message_id"],
                "thread_id": payload.get("thread_id") or "",
            },
        )
        if envelope is None:
            return
        self.queue_status("receipt_marker", response_handle=envelope.response_handle)
        self.queue_status("typing_start", response_handle=envelope.response_handle)

    async def _control_payload_from_update(self, update: Any) -> dict[str, Any] | None:
        callback_query = getattr(update, "callback_query", None)
        if callback_query is None:
            return None
        data = str(getattr(callback_query, "data", "") or "").strip()
        if not data.startswith("ctl:"):
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
        command_text = data[4:].strip()
        if not command_text:
            return None
        return {
            "text": command_text,
            "chat_id": str(chat_id or ""),
            "message_id": str(getattr(message, "message_id", "") or ""),
            "thread_id": str(getattr(message, "message_thread_id", "") or ""),
            "from_user_id": str(user_id or ""),
            "attachments": [],
            "source_metadata": {
                "telegram_callback_id": str(getattr(callback_query, "id", "") or ""),
                "callback_data": "control",
            },
        }

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
        return {
            "attachment_id": f"telegram_{provider_file_id}",
            "provider_file_id": provider_file_id,
            "file_name": file_name,
            "mime_type": stored.mime_type,
            "size_bytes": stored.size_bytes,
            "local_cached_path": stored.local_cached_path,
            "sha256": stored.sha256,
            "source_channel": "telegram",
            "source_metadata": {"telegram_file_path": str(getattr(tg_file, "file_path", "") or "")},
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

    async def _send_control_message_async(
        self,
        response_handle: ResponseHandle,
        payload: dict[str, Any],
        *,
        store_request_id: str,
    ) -> None:
        if self.application is None:
            return
        chat_id = _safe_int(response_handle.reply_target.get("chat_id"))
        thread_id = _safe_int(response_handle.reply_target.get("thread_id"))
        if chat_id is None:
            return
        text = str(payload.get("text") or "").strip()
        buttons = list(payload.get("buttons") or [])
        markup = self._build_control_markup(buttons)
        kwargs: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        if markup is not None:
            kwargs["reply_markup"] = markup
        sent = None
        try:
            sent = await self.application.bot.send_message(**kwargs)
        except Exception as exc:
            self.last_delivery_error = str(exc)
            return
        if store_request_id:
            self._control_prompt_messages[store_request_id] = {
                "chat_id": chat_id,
                "message_id": _safe_int(getattr(sent, "message_id", None)),
            }

    async def _expire_control_prompt_async(self, payload: dict[str, Any]) -> None:
        if self.application is None:
            return
        request_id = str(payload.get("request_id") or "").strip()
        text = str(payload.get("text") or "This request expired.").strip()
        if not request_id:
            return
        target = self._control_prompt_messages.pop(request_id, None)
        if not target:
            return
        chat_id = _safe_int(target.get("chat_id"))
        message_id = _safe_int(target.get("message_id"))
        if chat_id is None or message_id is None:
            return
        with contextlib.suppress(Exception):
            await self.application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
            )

    async def _apply_control_catalog_async(self, payload: dict[str, Any]) -> None:
        commands = self._normalize_control_commands(payload)
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
            return
        with contextlib.suppress(Exception):
            try:
                from telegram import MenuButtonCommands
            except Exception:
                MenuButtonCommands = _FallbackMenuButtonCommands

            await bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    def _normalize_control_commands(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        manifest: list[dict[str, str]] = []
        for item in list(payload.get("commands") or []):
            if not isinstance(item, dict):
                continue
            command = str(item.get("command") or "").strip().lower()
            description = str(item.get("description") or "").strip()
            if not command or not description:
                continue
            if re.fullmatch(r"[a-z0-9_]{1,32}", command) is None:
                continue
            manifest.append(
                {
                    "command": command,
                    "description": description[:256],
                }
            )
        return manifest

    def _build_control_markup(self, buttons: list[list[dict[str, Any]]]):
        if not buttons:
            return None
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        except Exception:
            return None
        rows = []
        for row_items in buttons:
            row = []
            for item in row_items:
                label = str(item.get("label") or "").strip()
                command = str(item.get("command") or "").strip()
                if not label or not command:
                    continue
                row.append(InlineKeyboardButton(text=label, callback_data=f"ctl:{command}"))
            if row:
                rows.append(row)
        if not rows:
            return None
        return InlineKeyboardMarkup(rows)


@dataclass(frozen=True)
class TelegramChannelEndpointFactory:
    channel_kind: str = "telegram"

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
        runtime_endpoint.enabled = bool(record.enabled)
        runtime_endpoint.attached = record.detached_at is None
        runtime_endpoint.paired = bool(record.binding_key)
        return runtime_endpoint
