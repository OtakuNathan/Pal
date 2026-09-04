from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pal.web_fetch.contracts import DEFAULT_WEB_FETCH_USER_AGENT

PLAYWRIGHT_CLI_PACKAGE = "@playwright/cli"
PLAYWRIGHT_CLI_VERSION = "0.1.19"
NODE_MINIMUM_MAJOR = 18
INSTALL_TIMEOUT_SECONDS = 420
PROFILE_RETENTION_SECONDS = 30 * 24 * 60 * 60
PROFILE_MAX_BYTES = 2 * 1024 * 1024 * 1024
SCREENSHOT_MAX_BYTES = 32 * 1024 * 1024
DEFAULT_IDLE_TIMEOUT_SECONDS = 60
DEFAULT_MAX_CONCURRENCY = 2

_SAFE_SESSION_RE = re.compile(r"^[a-f0-9]{64}$")
_BROWSER_MISSING_MARKERS = (
    "executable doesn't exist",
    "browser executable",
    "install-browser",
    "playwright install",
)
_LAYOUT_STYLE_PROPERTIES = (
    "display", "position", "box-sizing", "width", "height", "min-width",
    "min-height", "max-width", "max-height", "margin-top", "margin-right",
    "margin-bottom", "margin-left", "padding-top", "padding-right",
    "padding-bottom", "padding-left", "gap", "row-gap", "column-gap",
    "white-space", "line-height", "font-size", "overflow", "overflow-x",
    "overflow-y", "align-items", "justify-content", "flex-direction",
    "grid-template-columns", "grid-template-rows", "list-style-position",
    "list-style-type", "visibility", "opacity", "transform",
)


def browser_session_key(execution_lifetime_id: str) -> str:
    normalized = str(execution_lifetime_id or "").strip()
    if not normalized:
        raise ValueError("browser actions require an execution lifetime")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _validate_session_key(value: object) -> str:
    key = str(value or "").strip().lower()
    if not _SAFE_SESSION_RE.fullmatch(key):
        raise ValueError("invalid browser session key")
    return key


def _validate_url(value: object) -> str:
    from urllib.parse import urlparse

    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("browser URL must use http or https")
    if len(url) > 8192:
        raise ValueError("browser URL exceeds 8192 characters")
    return url


def _bounded_text(value: object, *, limit: int, field_name: str) -> str:
    text = str(value or "")
    if "\x00" in text:
        raise ValueError(f"{field_name} contains a NUL byte")
    if len(text) > limit:
        raise ValueError(f"{field_name} exceeds {limit} characters")
    return text


@dataclass(frozen=True)
class BrowserRuntimePaths:
    runtime_root: Path

    @property
    def root(self) -> Path:
        return Path(self.runtime_root) / "data" / "web_fetch"

    @property
    def tooling_root(self) -> Path:
        return self.root / "tooling"

    @property
    def tooling_current(self) -> Path:
        return self.tooling_root / "current"

    @property
    def cli(self) -> Path:
        return self.tooling_current / "node_modules" / ".bin" / "playwright-cli"

    @property
    def browser_cache(self) -> Path:
        return self.root / "browsers"

    @property
    def cli_cache(self) -> Path:
        return self.root / "cli-cache"

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    @property
    def config(self) -> Path:
        return self.workspace / ".playwright" / "cli.config.json"

    @property
    def output(self) -> Path:
        return self.root / "output"

    @property
    def temporary(self) -> Path:
        return self.root / "tmp"

    @property
    def profiles(self) -> Path:
        return self.root / "profiles"

    def prepare(self) -> None:
        for path in (
            self.root,
            self.tooling_root,
            self.browser_cache,
            self.cli_cache,
            self.workspace,
            self.output,
            self.temporary,
            self.profiles,
            self.config.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self.root.chmod(0o700)
            self.profiles.chmod(0o700)


def _detect_node_major() -> int | None:
    node = shutil.which("node")
    if not node:
        return None
    try:
        completed = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=5, check=False
        )
        return int(completed.stdout.strip().lstrip("v").split(".", 1)[0])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _installed_cli_version(paths: BrowserRuntimePaths) -> str:
    if not paths.cli.is_file():
        return ""
    package_json = paths.tooling_current / "node_modules" / "@playwright" / "cli" / "package.json"
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(payload.get("version") or "") if isinstance(payload, dict) else ""


def _chromium_installed(paths: BrowserRuntimePaths) -> bool:
    candidates = (
        "chrome-headless-shell",
        "chrome-headless-shell.exe",
        "headless_shell",
        "Chromium Headless Shell",
    )
    for browser_root in paths.browser_cache.glob("chromium_headless_shell-*"):
        for name in candidates:
            if any(candidate.is_file() for candidate in browser_root.rglob(name)):
                return True
    return False


class BrowserServiceError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "browser_error",
        retryable: bool = False,
        state_unknown: bool = False,
        curl_applicable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.retryable = bool(retryable)
        self.state_unknown = bool(state_unknown)
        self.curl_applicable = bool(curl_applicable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "state_unknown": self.state_unknown,
            "curl_applicable": self.curl_applicable,
        }


@dataclass
class _SessionRecord:
    key: str
    name: str
    persistent: bool
    last_url: str = ""
    opened_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)


class _PlaywrightCliWorker:
    def __init__(self, *, runtime_root: Path, max_concurrency: int) -> None:
        self.paths = BrowserRuntimePaths(Path(runtime_root))
        self.paths.prepare()
        self.semaphore = threading.BoundedSemaphore(max(1, int(max_concurrency)))
        self.last_activity_at = time.monotonic()
        self.in_flight = 0
        self.last_error = ""
        self.sessions: dict[str, _SessionRecord] = {}
        self._session_locks: dict[str, threading.RLock] = {}
        self._lock = threading.RLock()
        self._install_lock = threading.Lock()
        self._install_thread: threading.Thread | None = None
        self._installer_process: subprocess.Popen[str] | None = None
        self._stopping = threading.Event()
        self._install_state: dict[str, Any] = {
            "attempted": False,
            "in_progress": False,
            "last_result": "",
        }
        self._write_config()
        self._node_major_cached = self._node_major()
        self._cli_version_cached = self._detected_cli_version()
        if self._cli_ready():
            self._close_workspace_sessions(force=True)
        self._prune_profiles()

    def _write_config(self) -> None:
        launch_options: dict[str, Any] = {"headless": True}
        proxy_url = str(
            os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("http_proxy")
            or os.environ.get("HTTP_PROXY")
            or ""
        ).strip()
        if proxy_url:
            proxy: dict[str, Any] = {"server": proxy_url}
            bypass = str(os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or "").strip()
            if bypass:
                proxy["bypass"] = bypass
            launch_options["proxy"] = proxy
        payload = {
            "browser": {
                "browserName": "chromium",
                "launchOptions": launch_options,
                "contextOptions": {
                    "acceptDownloads": False,
                    "userAgent": DEFAULT_WEB_FETCH_USER_AGENT,
                    "viewport": {"width": 1280, "height": 900},
                },
            },
            "outputDir": str(self.paths.output),
            "outputMode": "stdout",
            "timeouts": {"action": 10000, "navigation": 60000},
            "allowUnrestrictedFileAccess": False,
            "codegen": "none",
        }
        _atomic_write_json(self.paths.config, payload, mode=0o600)

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "CI": "1",
                "NO_UPDATE_NOTIFIER": "1",
                "PLAYWRIGHT_BROWSERS_PATH": str(self.paths.browser_cache),
                "XDG_CACHE_HOME": str(self.paths.cli_cache),
            }
        )
        return env

    def _node_major(self) -> int | None:
        return _detect_node_major()

    def _detected_cli_version(self) -> str:
        return _installed_cli_version(self.paths)

    def _cli_ready(self) -> bool:
        return bool(
            self._node_major_cached is not None
            and self._node_major_cached >= NODE_MINIMUM_MAJOR
            and self._cli_version_cached == PLAYWRIGHT_CLI_VERSION
            and self.paths.cli.is_file()
        )

    def _schedule_install(self, *, browser_only: bool = False, reason: str = "") -> None:
        with self._install_lock:
            if self._stopping.is_set():
                return
            if self._install_thread is not None and self._install_thread.is_alive():
                return
            self._install_state.update(
                {
                    "attempted": True,
                    "in_progress": True,
                    "last_result": f"installing ({reason or 'dependency missing'})",
                }
            )
            thread = threading.Thread(
                target=self._install_dependencies,
                kwargs={"browser_only": bool(browser_only)},
                name="pal-web-fetch-install",
                daemon=True,
            )
            self._install_thread = thread
            thread.start()

    def _install_dependencies(self, *, browser_only: bool) -> None:
        staging: Path | None = None
        try:
            node_major = self._node_major()
            npm = shutil.which("npm")
            if node_major is None or node_major < NODE_MINIMUM_MAJOR:
                raise RuntimeError(f"Node.js {NODE_MINIMUM_MAJOR}+ is required")
            if not browser_only:
                if not npm:
                    raise RuntimeError("npm is required to provision Playwright CLI")
                staging = self.paths.tooling_root / f"staging-{uuid.uuid4().hex}"
                completed = self._run_install_command(
                    [
                        npm, "install", "--prefix", str(staging), "--ignore-scripts",
                        "--no-audit", "--no-fund", "--omit=dev",
                        f"{PLAYWRIGHT_CLI_PACKAGE}@{PLAYWRIGHT_CLI_VERSION}",
                    ],
                )
                if completed.returncode != 0:
                    shutil.rmtree(staging, ignore_errors=True)
                    detail = (completed.stderr or completed.stdout or "")[-500:]
                    raise RuntimeError(f"npm install failed: {detail}")
                old = self.paths.tooling_root / f"old-{uuid.uuid4().hex}"
                moved_current = False
                try:
                    if self.paths.tooling_current.exists():
                        os.replace(self.paths.tooling_current, old)
                        moved_current = True
                    os.replace(staging, self.paths.tooling_current)
                    staging = None
                except Exception:
                    if (
                        moved_current
                        and old.exists()
                        and not self.paths.tooling_current.exists()
                    ):
                        os.replace(old, self.paths.tooling_current)
                    raise
                finally:
                    if self.paths.tooling_current.exists():
                        shutil.rmtree(old, ignore_errors=True)
            if not self.paths.cli.is_file():
                raise RuntimeError("Playwright CLI executable was not installed")
            completed = self._run_install_command(
                [str(self.paths.cli), "install-browser", "chromium", "--only-shell"],
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "")[-500:]
                raise RuntimeError(f"browser install failed: {detail}")
            self._node_major_cached = self._node_major()
            self._cli_version_cached = self._detected_cli_version()
            result = "ok"
        except Exception as exc:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            result = f"failed: {exc}"[-500:]
        with self._install_lock:
            self._install_state["in_progress"] = False
            self._install_state["last_result"] = result

    def _run_install_command(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            command,
            cwd=str(self.paths.workspace),
            env=self._child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=os.name != "nt",
        )
        with self._install_lock:
            if self._stopping.is_set():
                _terminate_process_tree(process)
                raise RuntimeError("browser dependency installation was cancelled")
            self._installer_process = process
        try:
            stdout, stderr = process.communicate(timeout=INSTALL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_tree(process)
            stdout, stderr = process.communicate()
            raise RuntimeError("browser dependency installation timed out") from exc
        finally:
            with self._install_lock:
                if self._installer_process is process:
                    self._installer_process = None
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def health(self) -> dict[str, Any]:
        with self._lock:
            sessions = list(self.sessions.values())
        with self._install_lock:
            install_state = dict(self._install_state)
        node_major = self._node_major_cached
        cli_version = self._cli_version_cached
        ready = bool(
            node_major is not None
            and node_major >= NODE_MINIMUM_MAJOR
            and cli_version == PLAYWRIGHT_CLI_VERSION
        )
        browser_installed = _chromium_installed(self.paths)
        return {
            "ok": True,
            "service": "playwright_cli",
            "healthy": ready and browser_installed and not self.last_error,
            "reason": "running" if ready and browser_installed and not self.last_error else ("dependency_missing" if not ready or not browser_installed else "last_action_failed"),
            "node_major": node_major,
            "required_node_major": NODE_MINIMUM_MAJOR,
            "cli_version": cli_version,
            "required_cli_version": PLAYWRIGHT_CLI_VERSION,
            "browser_installed": browser_installed,
            "in_flight": self.in_flight,
            "active_sessions": len(sessions),
            "persistent_sessions": sum(1 for item in sessions if item.persistent),
            "last_error": self.last_error,
            "self_heal": install_state,
            "profile_count": len(tuple(self.paths.profiles.glob("*"))),
            "profile_bytes": _tree_size(self.paths.profiles),
        }

    def install_in_progress(self) -> bool:
        with self._install_lock:
            return bool(self._install_state["in_progress"])

    def execute(
        self,
        *,
        session_key: str,
        action: str,
        args: dict[str, Any],
        persistent: bool,
        timeout_ms: int,
    ) -> dict[str, Any]:
        key = _validate_session_key(session_key)
        normalized_action = str(action or "").strip().lower()
        lock = self._session_lock(key)
        with self.semaphore, lock:
            with self._lock:
                self.in_flight += 1
                self.last_activity_at = time.monotonic()
            try:
                if normalized_action == "reset":
                    result = self._reset(key)
                    self.last_error = ""
                    return result
                if normalized_action == "close":
                    result = self._close(key)
                    self.last_error = ""
                    return result
                if not self._cli_ready():
                    self._schedule_install(reason="CLI missing or wrong version")
                    raise BrowserServiceError(
                        "Playwright CLI is being provisioned",
                        code="dependency_installing",
                        retryable=True,
                        curl_applicable=normalized_action in {"navigate", "read"},
                    )
                result = self._execute_ready(
                    key=key,
                    action=normalized_action,
                    args=dict(args or {}),
                    persistent=bool(persistent),
                    timeout_ms=max(1000, min(120000, int(timeout_ms))),
                )
                self.last_error = ""
                return result
            except BrowserServiceError as exc:
                if (
                    normalized_action in {"navigate", "read"}
                    and exc.code != "invalid_arguments"
                    and not exc.curl_applicable
                ):
                    wrapped = BrowserServiceError(
                        str(exc),
                        code=exc.code,
                        retryable=exc.retryable,
                        state_unknown=exc.state_unknown,
                        curl_applicable=True,
                    )
                    self.last_error = str(wrapped)[-500:]
                    raise wrapped from exc
                self.last_error = str(exc)[-500:]
                raise
            except (TypeError, ValueError) as exc:
                self.last_error = str(exc)[-500:]
                raise BrowserServiceError(
                    str(exc), code="invalid_arguments"
                ) from exc
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"[-500:]
                raise BrowserServiceError(str(exc)) from exc
            finally:
                with self._lock:
                    self.in_flight = max(0, self.in_flight - 1)
                    self.last_activity_at = time.monotonic()

    def _execute_ready(
        self,
        *,
        key: str,
        action: str,
        args: dict[str, Any],
        persistent: bool,
        timeout_ms: int,
    ) -> dict[str, Any]:
        if action not in {
            "navigate", "read", "snapshot", "find", "click", "fill", "type",
            "press", "hover", "select", "check", "scroll", "resize", "history",
            "tabs", "dialog", "inspect_layout", "screenshot", "status",
            "evaluate", "network",
        }:
            raise BrowserServiceError("unsupported browser action", code="unsupported_action")
        if action == "status":
            record = self.sessions.get(key)
            return {"session": self._session_payload(record), "runtime": self.health()}
        record, recovered = self._ensure_session(key, persistent=persistent, timeout_ms=timeout_ms)
        try:
            payload = self._dispatch_action(record, action=action, args=args, timeout_ms=timeout_ms)
        except BrowserServiceError as exc:
            if any(marker in str(exc).lower() for marker in _BROWSER_MISSING_MARKERS):
                self._schedule_install(browser_only=True, reason="Chromium build missing")
                raise BrowserServiceError(
                    "Chromium is being provisioned",
                    code="dependency_installing",
                    retryable=True,
                    state_unknown=exc.state_unknown,
                    curl_applicable=action in {"navigate", "read"},
                ) from exc
            raise
        record.last_used_at = time.time()
        page = dict(payload.get("page") or self._page_state(record, timeout_ms=timeout_ms))
        record.last_url = str(page.get("url") or record.last_url)
        if record.persistent:
            self._write_profile_meta(record)
        payload.update(
            {
                "action": action,
                "session": {**self._session_payload(record), "recovered": recovered},
                "page": page,
            }
        )
        return payload

    def _dispatch_action(
        self,
        record: _SessionRecord,
        *,
        action: str,
        args: dict[str, Any],
        timeout_ms: int,
    ) -> dict[str, Any]:
        if action == "navigate":
            self._run(record, _cli_args("goto", _validate_url(args.get("url"))), timeout_ms=timeout_ms, raw=True)
            return {}
        if action == "read":
            url = str(args.get("url") or "").strip()
            if url:
                self._run(record, _cli_args("goto", _validate_url(url)), timeout_ms=timeout_ms, raw=True)
            max_chars = max(1000, min(100000, int(args.get("max_chars") or 12000)))
            max_links = max(0, min(500, int(args.get("max_links") or 80)))
            raw = self._run(
                record,
                ["eval", _read_page_script(max_chars=max_chars, max_links=max_links)],
                timeout_ms=timeout_ms,
                raw=True,
            )
            document = _parse_json_object(raw, "browser read")
            text = str(document.get("text") or "").strip()
            document["text_truncated"] = len(text) > max_chars
            document["text"] = text[:max_chars].rstrip()
            links = list(document.get("links") or [])
            document["links_truncated"] = len(links) > max_links
            document["links"] = links[:max_links]
            return {"document": document}
        if action == "snapshot":
            options: list[str] = []
            target = str(args.get("target") or "").strip()
            if args.get("depth") is not None:
                options.append(f"--depth={max(1, min(30, int(args['depth'])))}")
            if bool(args.get("boxes")):
                options.append("--boxes")
            positionals = [_bounded_text(target, limit=500, field_name="target")] if target else []
            command = _cli_args("snapshot", *positionals, options=options)
            raw = self._run(record, command, timeout_ms=timeout_ms, raw=True)
            max_chars = max(1000, min(100000, int(args.get("max_chars") or 12000)))
            return {"snapshot": raw[:max_chars], "truncated": len(raw) > max_chars}
        if action == "find":
            text = str(args.get("text") or "")
            regex = str(args.get("regex") or "")
            if bool(text) == bool(regex):
                raise BrowserServiceError("provide exactly one of text or regex", code="invalid_arguments")
            command = ["find"]
            if regex:
                command.append(f"--regex={_bounded_text(regex, limit=500, field_name='regex')}")
            else:
                command = _cli_args("find", _bounded_text(text, limit=500, field_name="text"))
            raw = self._run(record, command, timeout_ms=timeout_ms, raw=True)
            return {"matches": raw[:12000], "truncated": len(raw) > 12000}
        if action == "click":
            command_name = "dblclick" if bool(args.get("double")) else "click"
            button = str(args.get("button") or "left").lower()
            if button not in {"left", "right", "middle"}:
                raise BrowserServiceError("invalid mouse button", code="invalid_arguments")
            options = []
            for modifier in list(args.get("modifiers") or []):
                options.append(f"--modifiers={_bounded_text(modifier, limit=20, field_name='modifier')}")
            command = _cli_args(command_name, self._target(args), button, options=options)
            self._run_write(record, command, timeout_ms=timeout_ms)
            return {}
        if action == "fill":
            options = []
            if bool(args.get("submit")):
                options.append("--submit")
            command = _cli_args(
                "fill",
                self._target(args),
                _bounded_text(args.get("text"), limit=20000, field_name="text"),
                options=options,
            )
            self._run_write(record, command, timeout_ms=timeout_ms)
            return {}
        if action == "type":
            self._run_write(record, _cli_args("type", _bounded_text(args.get("text"), limit=20000, field_name="text")), timeout_ms=timeout_ms)
            return {}
        if action == "press":
            self._run_write(record, _cli_args("press", _bounded_text(args.get("key"), limit=80, field_name="key")), timeout_ms=timeout_ms)
            return {}
        if action == "hover":
            self._run_write(record, _cli_args("hover", self._target(args)), timeout_ms=timeout_ms)
            return {}
        if action == "select":
            self._run_write(record, _cli_args("select", self._target(args), _bounded_text(args.get("value"), limit=1000, field_name="value")), timeout_ms=timeout_ms)
            return {}
        if action == "check":
            self._run_write(record, _cli_args("check" if bool(args.get("checked", True)) else "uncheck", self._target(args)), timeout_ms=timeout_ms)
            return {}
        if action == "scroll":
            dx = max(-100000, min(100000, int(args.get("dx") or 0)))
            dy = max(-100000, min(100000, int(args.get("dy") or 0)))
            self._run_write(record, _cli_args("mousewheel", str(dx), str(dy)), timeout_ms=timeout_ms)
            return {}
        if action == "resize":
            width = max(320, min(4096, int(args.get("width") or 1280)))
            height = max(320, min(4096, int(args.get("height") or 900)))
            self._run(record, _cli_args("resize", str(width), str(height)), timeout_ms=timeout_ms, raw=True)
            return {"viewport": {"width": width, "height": height}}
        if action == "history":
            mapping = {"back": "go-back", "forward": "go-forward", "reload": "reload"}
            requested = str(args.get("operation") or "").lower()
            if requested not in mapping:
                raise BrowserServiceError("invalid history operation", code="invalid_arguments")
            self._run(record, [mapping[requested]], timeout_ms=timeout_ms, raw=True)
            return {}
        if action == "tabs":
            operation = str(args.get("operation") or "list").lower()
            command = {"list": "tab-list", "new": "tab-new", "select": "tab-select", "close": "tab-close"}.get(operation)
            if command is None:
                raise BrowserServiceError("invalid tab operation", code="invalid_arguments")
            argv = [command]
            if operation == "new" and str(args.get("url") or "").strip():
                argv = _cli_args(command, _validate_url(args["url"]))
            if operation in {"select", "close"} and args.get("index") is not None:
                argv = _cli_args(command, str(max(0, int(args["index"]))))
            return {"tabs": self._run_write(record, argv, timeout_ms=timeout_ms)}
        if action == "dialog":
            operation = str(args.get("operation") or "").lower()
            if operation == "accept":
                argv = ["dialog-accept"]
                if args.get("prompt") is not None:
                    argv = _cli_args("dialog-accept", _bounded_text(args.get("prompt"), limit=4000, field_name="prompt"))
            elif operation == "dismiss":
                argv = ["dialog-dismiss"]
            else:
                raise BrowserServiceError("invalid dialog operation", code="invalid_arguments")
            self._run_write(record, argv, timeout_ms=timeout_ms)
            return {}
        if action == "inspect_layout":
            selector = _bounded_text(args.get("selector"), limit=500, field_name="selector").strip()
            if not selector:
                raise BrowserServiceError("selector is required", code="invalid_arguments")
            limit = max(1, min(20, int(args.get("max_elements") or 20)))
            raw = self._run(record, ["eval", _layout_script(selector=selector, limit=limit)], timeout_ms=timeout_ms, raw=True)
            return {"inspection": _parse_json_object(raw, "layout inspection")}
        if action == "evaluate":
            func = _bounded_text(args.get("func"), limit=20000, field_name="func").strip()
            if not func:
                raise BrowserServiceError("func is required", code="invalid_arguments")
            target = _bounded_text(args.get("target") or "", limit=500, field_name="target").strip()
            if func.startswith("-") or target.startswith("-"):
                raise BrowserServiceError("func/target must not start with '-'", code="invalid_arguments")
            argv = ["eval", func] + ([target] if target else [])
            raw = self._run(record, argv, timeout_ms=timeout_ms, raw=True)
            max_chars = max(200, min(100000, int(args.get("max_chars") or 20000)))
            value = _parse_lenient_json(raw)
            truncated = isinstance(value, str) and len(value) > max_chars
            if truncated:
                value = value[:max_chars]
            return {"result": value, "result_type": type(value).__name__, "truncated": truncated}
        if action == "network":
            operation = str(args.get("operation") or "read").lower()
            if operation == "start":
                raw = self._run(record, ["eval", _network_start_script()], timeout_ms=timeout_ms, raw=True)
                return {"network": _parse_json_object(raw, "network start")}
            if operation == "clear":
                raw = self._run(record, ["eval", _network_clear_script()], timeout_ms=timeout_ms, raw=True)
                return {"network": _parse_json_object(raw, "network clear")}
            if operation == "read":
                url_filter = _bounded_text(args.get("url_filter") or "", limit=500, field_name="url_filter").strip()
                since = max(0, int(args.get("since") or 0))
                limit = max(1, min(200, int(args.get("limit") or 50)))
                clear_on_read = bool(args.get("clear_on_read"))
                raw = self._run(
                    record,
                    ["eval", _network_read_script(url_filter=url_filter, since=since, limit=limit, clear=clear_on_read)],
                    timeout_ms=timeout_ms,
                    raw=True,
                )
                return {"network": _parse_json_object(raw, "network read")}
            raise BrowserServiceError("invalid network operation", code="invalid_arguments")
        if action == "screenshot":
            file_path = self.paths.temporary / f"shot-{uuid.uuid4().hex}.png"
            options = [f"--filename={file_path}"]
            target = str(args.get("target") or "").strip()
            if bool(args.get("full_page")):
                options.append("--full-page")
            if bool(args.get("hires")):
                options.append("--hires")
            positionals = [_bounded_text(target, limit=500, field_name="target")] if target else []
            command = _cli_args("screenshot", *positionals, options=options)
            try:
                self._run(record, command, timeout_ms=timeout_ms, raw=True)
                if file_path.stat().st_size > SCREENSHOT_MAX_BYTES:
                    raise BrowserServiceError(
                        f"browser screenshot exceeds {SCREENSHOT_MAX_BYTES} bytes",
                        code="screenshot_too_large",
                    )
                content = file_path.read_bytes()
            finally:
                file_path.unlink(missing_ok=True)
            return {"png_base64": base64.b64encode(content).decode("ascii")}
        raise BrowserServiceError("unsupported browser action", code="unsupported_action")

    @staticmethod
    def _target(args: dict[str, Any]) -> str:
        target = _bounded_text(args.get("target"), limit=500, field_name="target").strip()
        if not target:
            raise BrowserServiceError("target is required", code="invalid_arguments")
        return target

    def _run_write(self, record: _SessionRecord, argv: list[str], *, timeout_ms: int) -> str:
        try:
            return self._run(record, argv, timeout_ms=timeout_ms, raw=True)
        except BrowserServiceError as exc:
            raise BrowserServiceError(
                str(exc), code=exc.code, retryable=False, state_unknown=True
            ) from exc

    def _run(self, record: _SessionRecord, argv: list[str], *, timeout_ms: int, raw: bool) -> str:
        command = [str(self.paths.cli), f"-s={record.name}"]
        if raw:
            command.append("--raw")
        command.extend(str(item) for item in argv)
        try:
            completed = subprocess.run(
                command,
                cwd=str(self.paths.workspace),
                env=self._child_env(),
                capture_output=True,
                text=True,
                timeout=max(1.0, timeout_ms / 1000.0 + 5.0),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BrowserServiceError(
                "Playwright CLI command timed out", code="command_timeout", state_unknown=True
            ) from exc
        except OSError as exc:
            raise BrowserServiceError(str(exc), code="cli_unavailable", retryable=True) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "Playwright CLI command failed").strip()[-1000:]
            raise BrowserServiceError(detail, code="cli_command_failed")
        return completed.stdout.strip()

    def _ensure_session(self, key: str, *, persistent: bool, timeout_ms: int) -> tuple[_SessionRecord, bool]:
        with self._lock:
            current = self.sessions.get(key)
        if current is not None:
            return current, False
        record = _SessionRecord(key=key, name=f"pal-{key[:24]}", persistent=bool(persistent))
        open_options = [f"--config={self.paths.config}"]
        restored_url = ""
        if persistent:
            profile_dir = self.paths.profiles / key
            profile_dir.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                profile_dir.chmod(0o700)
            restored_url = str(self._read_profile_meta(key).get("last_url") or "")
            open_options.append(f"--profile={profile_dir / 'user-data'}")
        initial_url = restored_url if restored_url.startswith(("http://", "https://")) else "about:blank"
        open_args = _cli_args("open", initial_url, options=open_options)
        try:
            self._run(record, open_args, timeout_ms=timeout_ms, raw=True)
        except BrowserServiceError:
            self._close_named(record, force=True)
            raise
        record.last_url = restored_url
        with self._lock:
            self.sessions[key] = record
        return record, bool(restored_url)

    def _page_state(self, record: _SessionRecord, *, timeout_ms: int) -> dict[str, Any]:
        raw = self._run(record, ["eval", _PAGE_STATE_SCRIPT], timeout_ms=timeout_ms, raw=True)
        return _parse_json_object(raw, "page state")

    @staticmethod
    def _session_payload(record: _SessionRecord | None) -> dict[str, Any]:
        return {
            "running": record is not None,
            "persistent": bool(record.persistent) if record is not None else True,
            "last_url": str(record.last_url if record is not None else ""),
        }

    def _session_lock(self, key: str) -> threading.RLock:
        with self._lock:
            return self._session_locks.setdefault(key, threading.RLock())

    def _close(self, key: str) -> dict[str, Any]:
        with self._lock:
            record = self.sessions.pop(key, None)
        was_running = record is not None
        if record is None:
            record = _SessionRecord(key=key, name=f"pal-{key[:24]}", persistent=True)
        self._close_named(record, force=False)
        with self._lock:
            active = set(self.sessions)
        self._prune_profiles(exclude=active)
        return {"action": "close", "closed": was_running, "profile_retained": True, "session": self._session_payload(None)}

    def _reset(self, key: str) -> dict[str, Any]:
        self._close(key)
        shutil.rmtree(self.paths.profiles / key, ignore_errors=True)
        return {"action": "reset", "reset": True, "profile_retained": False, "session": self._session_payload(None)}

    def _close_named(self, record: _SessionRecord, *, force: bool) -> None:
        if not self.paths.cli.is_file():
            return
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                [str(self.paths.cli), f"-s={record.name}", "close"],
                cwd=str(self.paths.workspace), env=self._child_env(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10 if force else 20, check=False,
            )

    def _close_workspace_sessions(self, *, force: bool) -> None:
        _ = force
        if not self.paths.cli.is_file():
            return
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                [str(self.paths.cli), "close-all"],
                cwd=str(self.paths.workspace), env=self._child_env(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=20, check=False,
            )

    def shutdown(self) -> None:
        self._stopping.set()
        with self._install_lock:
            installer = self._installer_process
        if installer is not None:
            _terminate_process_tree(installer)
        self._close_workspace_sessions(force=False)
        with self._lock:
            self.sessions.clear()
        for path in self.paths.temporary.glob("*"):
            if path.is_file():
                path.unlink(missing_ok=True)

    def _profile_meta_path(self, key: str) -> Path:
        return self.paths.profiles / key / "session.json"

    def _read_profile_meta(self, key: str) -> dict[str, Any]:
        try:
            payload = json.loads(self._profile_meta_path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_profile_meta(self, record: _SessionRecord) -> None:
        _atomic_write_json(
            self._profile_meta_path(record.key),
            {"last_url": record.last_url, "last_used_at": record.last_used_at},
            mode=0o600,
        )

    def _prune_profiles(self, *, exclude: set[str] | None = None) -> None:
        now = time.time()
        excluded = set(exclude or ())
        entries: list[tuple[float, int, Path]] = []
        for path in self.paths.profiles.iterdir():
            if not path.is_dir() or not _SAFE_SESSION_RE.fullmatch(path.name):
                continue
            if path.name in excluded:
                continue
            meta = self._read_profile_meta(path.name)
            last_used = float(meta.get("last_used_at") or path.stat().st_mtime)
            size = _tree_size(path)
            if now - last_used > PROFILE_RETENTION_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
                continue
            entries.append((last_used, size, path))
        total = _tree_size(self.paths.profiles)
        for _last_used, size, path in sorted(entries):
            if total <= PROFILE_MAX_BYTES:
                break
            shutil.rmtree(path, ignore_errors=True)
            total = max(0, total - size)


_PAGE_STATE_SCRIPT = "() => JSON.stringify({url: location.href, title: document.title || ''})"


def _read_page_script(*, max_chars: int, max_links: int) -> str:
    args = json.dumps({"maxChars": max_chars, "maxLinks": max_links})
    return f"""() => JSON.stringify((() => {{
      const args = {args};
      const metadata = {{}};
      const canonical = document.querySelector('link[rel~="canonical"]');
      if (canonical && canonical.href) metadata.canonical_url = canonical.href;
      if (document.documentElement && document.documentElement.lang) metadata.language = document.documentElement.lang;
      for (const [key, selector] of [
        ['description', 'meta[name="description"]'],
        ['open_graph_description', 'meta[property="og:description"]'],
        ['open_graph_title', 'meta[property="og:title"]']
      ]) {{
        const node = document.querySelector(selector);
        if (node && node.content) metadata[key] = String(node.content).slice(0, 2000);
      }}
      const text = document.body ? (document.body.innerText || '') : '';
      const links = Array.from(document.querySelectorAll('a[href]')).slice(0, args.maxLinks + 1).map(a => ({{
        href: String(a.href || '').slice(0, 8192),
        text: String(a.innerText || a.textContent || '').trim().slice(0, 1000),
        rel: String(a.rel || '').slice(0, 500)
      }}));
      return {{
        requested_url: location.href, final_url: location.href,
        title: String(document.title || '').slice(0, 2000),
        text: text.slice(0, args.maxChars + 1),
        content_type: String(document.contentType || '').slice(0, 200),
        links, metadata
      }};
    }})())"""


def _layout_script(*, selector: str, limit: int) -> str:
    args = json.dumps(
        {"selector": selector, "limit": limit, "properties": list(_LAYOUT_STYLE_PROPERTIES)},
        ensure_ascii=False,
    )
    return f"""() => JSON.stringify((() => {{
      const args = {args};
      const round = value => Number.isFinite(value) ? Math.round(value * 100) / 100 : null;
      const rectPayload = rect => ({{x: round(rect.x), y: round(rect.y), width: round(rect.width), height: round(rect.height), top: round(rect.top), right: round(rect.right), bottom: round(rect.bottom), left: round(rect.left)}});
      const all = Array.from(document.querySelectorAll(args.selector));
      const elements = all.slice(0, args.limit).map(node => {{
        const rect = node.getBoundingClientRect();
        const parentRect = node.parentElement ? node.parentElement.getBoundingClientRect() : null;
        const previousRect = node.previousElementSibling ? node.previousElementSibling.getBoundingClientRect() : null;
        const nextRect = node.nextElementSibling ? node.nextElementSibling.getBoundingClientRect() : null;
        const computed = getComputedStyle(node);
        const styles = Object.fromEntries(args.properties.map(name => [name, computed.getPropertyValue(name)]));
        return {{tag: String(node.tagName || '').toLowerCase(), id: String(node.id || '').slice(0,160), classes: String(node.getAttribute('class') || '').split(/\\s+/).filter(Boolean).slice(0,20), role: String(node.getAttribute('role') || '').slice(0,80), text: String(node.innerText || node.textContent || '').replace(/\\s+/g,' ').trim().slice(0,240), geometry: rectPayload(rect), parent_geometry: parentRect ? rectPayload(parentRect) : null, previous_sibling_vertical_gap_px: previousRect ? round(rect.top - previousRect.bottom) : null, next_sibling_vertical_gap_px: nextRect ? round(nextRect.top - rect.bottom) : null, computed_styles: styles}};
      }});
      return {{selector: args.selector, matched_count: all.length, truncated: all.length > args.limit, elements}};
    }})())"""


def _parse_json_object(raw: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or ""))
        if isinstance(value, str):
            value = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise BrowserServiceError(f"{label} returned invalid JSON", code="invalid_cli_output") from exc
    if not isinstance(value, dict):
        raise BrowserServiceError(f"{label} returned a non-object", code="invalid_cli_output")
    return value


def _parse_lenient_json(raw: str) -> Any:
    text = str(raw or "")
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _network_start_script() -> str:
    return """() => JSON.stringify((() => {
  if (window.__palNetHooked) {
    return {installed: false, already_hooked: true, log_length: (window.__palNetLog || []).length};
  }
  const log = [];
  window.__palNetLog = log;
  const push = entry => { try { log.push(entry); if (log.length > 800) log.splice(0, log.length - 800); } catch (err) {} };
  const headersOf = headers => {
    const out = {};
    try {
      if (!headers) return out;
      if (typeof headers.forEach === 'function') headers.forEach((value, key) => { out[key] = String(value); });
      else if (typeof headers === 'object') for (const key of Object.keys(headers)) out[key] = String(headers[key]);
    } catch (err) {}
    return out;
  };
  const bodyOf = body => {
    try {
      if (body === null || body === undefined) return null;
      if (typeof body === 'string') return body.slice(0, 2048);
      if (typeof body === 'object') return JSON.stringify(body).slice(0, 2048);
      return String(body).slice(0, 2048);
    } catch (err) { return null; }
  };
  const originalFetch = window.fetch;
  if (typeof originalFetch === 'function') {
    window.fetch = function(input, init) {
      const url = typeof input === 'string' ? input : String((input && input.url) || input);
      const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
      const request = {method: method, headers: headersOf((init && init.headers) || (input && input.headers)), body: bodyOf(init && init.body)};
      const startedAt = Date.now();
      const at = new Date(startedAt).toISOString();
      return originalFetch.apply(this, arguments).then(
        response => { push({kind: 'fetch', url: url, method: method, status: response.status, request: request, ms: Date.now() - startedAt, at: at}); return response; },
        error => { push({kind: 'fetch', url: url, method: method, status: 0, request: request, error: String((error && error.message) || error), ms: Date.now() - startedAt, at: at}); throw error; }
      );
    };
  }
  const xhrProto = XMLHttpRequest.prototype;
  const originalOpen = xhrProto.open, originalSend = xhrProto.send, originalSetHeader = xhrProto.setRequestHeader;
  xhrProto.open = function(method, url) { this.__palNetMeta = {method: String(method || 'GET').toUpperCase(), url: String(url || ''), headers: {}}; return originalOpen.apply(this, arguments); };
  xhrProto.setRequestHeader = function(name, value) { if (this.__palNetMeta) this.__palNetMeta.headers[String(name)] = String(value); return originalSetHeader.apply(this, arguments); };
  xhrProto.send = function(body) {
    const meta = this.__palNetMeta || {method: 'GET', url: '', headers: {}};
    if (this.__palNetMeta && body !== undefined && body !== null) meta.body = bodyOf(body);
    const startedAt = Date.now();
    const at = new Date(startedAt).toISOString();
    this.addEventListener('loadend', () => {
      push({kind: 'xhr', url: String(this.responseURL || meta.url), method: meta.method, status: this.status, request: meta, ms: Date.now() - startedAt, at: at});
    });
    return originalSend.apply(this, arguments);
  };
  window.__palNetHooked = true;
  return {installed: true, origin: location.origin};
})())"""


def _network_read_script(*, url_filter: str, since: int, limit: int, clear: bool) -> str:
    args = json.dumps({"filter": url_filter, "since": since, "limit": limit, "clear": clear}, ensure_ascii=False)
    return f"""() => JSON.stringify((() => {{
  const args = {args};
  const log = window.__palNetLog;
  if (!log) return {{hooked: false, entries: [], note: 'hook not installed; run network start after navigation'}};
  let entries = log.slice(Math.max(0, args.since));
  if (args.filter) entries = entries.filter(entry => String(entry.url || '').indexOf(args.filter) !== -1);
  const total_matching = entries.length;
  const truncated = entries.length > args.limit;
  entries = entries.slice(0, args.limit);
  if (args.clear) window.__palNetLog = [];
  return {{hooked: true, entries: entries, returned: entries.length, total_matching: total_matching, truncated: truncated, next_since: args.clear ? 0 : log.length, cleared: !!args.clear}};
}})())"""


def _network_clear_script() -> str:
    return "() => JSON.stringify((() => { const count = (window.__palNetLog || []).length; window.__palNetLog = []; return {cleared: count}; })())"


def _cli_args(command: str, *positionals: str, options: list[str] | None = None) -> list[str]:
    argv = [str(command), *(str(item) for item in (options or []))]
    if positionals:
        argv.extend(["--", *(str(item) for item in positionals)])
    return argv


def _atomic_write_json(path: Path, payload: dict[str, Any], *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.chmod(mode)
    os.replace(temporary, path)


def _tree_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path):
        for file_name in files:
            with contextlib.suppress(OSError):
                total += (Path(root) / file_name).stat().st_size
    return total


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=2.0)
        return
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(ProcessLookupError, OSError):
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)


def _json_response(handler: BaseHTTPRequestHandler, status_code: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def run_browser_service_cli(
    *, runtime_root: Path, host: str, port: int, token: str,
    idle_timeout_seconds: int, max_concurrency: int,
) -> int:
    worker = _PlaywrightCliWorker(runtime_root=Path(runtime_root), max_concurrency=max_concurrency)

    class BrowserHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            _ = format, args

        def _authorized(self) -> bool:
            return str(self.headers.get("Authorization") or "") == f"Bearer {token}"

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length < 0 or length > 256 * 1024:
                raise ValueError("request body exceeds 256 KiB")
            raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else "{}"
            decoded = json.loads(raw or "{}")
            return decoded if isinstance(decoded, dict) else {}

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                _json_response(self, 401, {"ok": False, "error": {"code": "unauthorized", "message": "unauthorized"}})
                return
            if self.path != "/health":
                _json_response(self, 404, {"ok": False, "error": {"code": "not_found", "message": "not found"}})
                return
            worker.last_activity_at = time.monotonic()
            _json_response(self, 200, worker.health())

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                _json_response(self, 401, {"ok": False, "error": {"code": "unauthorized", "message": "unauthorized"}})
                return
            try:
                payload = self._read_json()
            except (UnicodeError, ValueError, TypeError) as exc:
                _json_response(self, 400, {"ok": False, "error": {"code": "invalid_arguments", "message": str(exc)}})
                return
            worker.last_activity_at = time.monotonic()
            if self.path == "/shutdown":
                worker.shutdown()
                _json_response(self, 200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path != "/action":
                _json_response(self, 404, {"ok": False, "error": {"code": "not_found", "message": "not found"}})
                return
            try:
                result = worker.execute(
                    session_key=str(payload.get("session_key") or ""),
                    action=str(payload.get("action") or ""),
                    args=dict(payload.get("args") or {}),
                    persistent=bool(payload.get("persistent", True)),
                    timeout_ms=int(payload.get("timeout_ms") or 15000),
                )
            except (BrowserServiceError, ValueError, TypeError) as exc:
                error = exc.to_dict() if isinstance(exc, BrowserServiceError) else BrowserServiceError(str(exc), code="invalid_arguments").to_dict()
                status = 503 if error["code"] == "dependency_installing" else 400 if error["code"] == "invalid_arguments" else 500
                _json_response(self, status, {"ok": False, "error": error})
                return
            _json_response(self, 200, {"ok": True, "result": result})

    server = ThreadingHTTPServer((host, int(port)), BrowserHandler)

    def idle_monitor() -> None:
        while True:
            time.sleep(1.0)
            if worker.in_flight > 0 or worker.install_in_progress():
                continue
            if time.monotonic() - worker.last_activity_at < max(5, int(idle_timeout_seconds)):
                continue
            worker.shutdown()
            server.shutdown()
            return

    threading.Thread(target=idle_monitor, daemon=True).start()
    server.serve_forever(poll_interval=0.5)
    server.server_close()
    return 0


@dataclass(frozen=True)
class _BrowserServiceProcess:
    process: subprocess.Popen[bytes]
    host: str
    port: int
    token: str


@dataclass
class BrowserServiceManager:
    runtime_root: Path
    host: str = "127.0.0.1"
    port: int | None = None
    token: str = ""
    idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_SECONDS
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    last_error: str = ""
    _process: _BrowserServiceProcess | None = field(default=None, init=False, repr=False)
    _start_lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def execute(
        self, *, session_key: str, action: str, args: dict[str, Any] | None = None,
        persistent: bool = True, timeout_ms: int = 15000,
    ) -> dict[str, Any]:
        resource = self._ensure_started()
        try:
            payload = self._request_json(
                "POST", "/action",
                {
                    "session_key": _validate_session_key(session_key), "action": str(action),
                    "args": dict(args or {}), "persistent": bool(persistent),
                    "timeout_ms": max(1000, min(120000, int(timeout_ms))),
                },
                timeout_seconds=max(10.0, timeout_ms / 1000.0 + 10.0), resource=resource,
            )
        except BrowserServiceError:
            raise
        except Exception as exc:
            self.last_error = f"browser sidecar transport failed: {exc}"[-500:]
            self.stop_sync()
            raise BrowserServiceError(
                self.last_error,
                code="sidecar_transport_failed",
                retryable=True,
                state_unknown=True,
                curl_applicable=str(action) in {"navigate", "read"},
            ) from exc
        if not bool(payload.get("ok")):
            error = dict(payload.get("error") or {})
            raise BrowserServiceError(
                str(error.get("message") or "browser action failed"),
                code=str(error.get("code") or "browser_error"),
                retryable=bool(error.get("retryable")),
                state_unknown=bool(error.get("state_unknown")),
                curl_applicable=bool(error.get("curl_applicable")),
            )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise BrowserServiceError("browser service returned invalid payload", code="invalid_sidecar_output")
        self.last_error = ""
        return result

    def health(self) -> dict[str, Any]:
        resource = self._process
        running = self._process_running(resource)
        paths = BrowserRuntimePaths(Path(self.runtime_root))
        node_major = _detect_node_major()
        cli_version = _installed_cli_version(paths)
        browser_installed = _chromium_installed(paths)
        dependencies_ready = bool(
            node_major is not None
            and node_major >= NODE_MINIMUM_MAJOR
            and cli_version == PLAYWRIGHT_CLI_VERSION
            and browser_installed
        )
        payload: dict[str, Any] = {
            "service_running": running, "host": self.host, "port": self.port,
            "required_cli_version": PLAYWRIGHT_CLI_VERSION,
            "cli_installed": paths.cli.is_file(), "cli_version": cli_version,
            "node_major": node_major, "required_node_major": NODE_MINIMUM_MAJOR,
            "browser_installed": browser_installed, "last_error": self.last_error,
            "idle_timeout_seconds": int(self.idle_timeout_seconds),
            "max_concurrency": int(self.max_concurrency),
        }
        if not running:
            payload.update({"healthy": dependencies_ready, "reason": "idle" if dependencies_ready else "dependency_missing"})
            return payload
        try:
            health = self._request_json("GET", "/health", None, timeout_seconds=1.0, resource=resource)
            payload.update(health)
            self.last_error = ""
        except Exception as exc:
            self.last_error = str(exc)
            payload.update({"healthy": False, "reason": "health_check_failed", "last_error": self.last_error})
        return payload

    def stop_sync(self) -> None:
        with self._start_lock:
            resource = self._process
            if resource is None:
                return
            try:
                if self._process_running(resource):
                    with contextlib.suppress(Exception):
                        self._request_json("POST", "/shutdown", {}, timeout_seconds=5.0, resource=resource)
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        resource.process.wait(timeout=5.0)
            finally:
                if self._process is resource:
                    self._process = None
                process = resource.process
                if process.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        if os.name == "nt":
                            process.kill()
                        else:
                            os.killpg(process.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=2.0)

    async def shutdown_async(self) -> None:
        self.stop_sync()

    def _ensure_started(self) -> _BrowserServiceProcess:
        with self._start_lock:
            resource = self._process
            if self._process_running(resource):
                assert resource is not None
                return resource
            self.stop_sync()
            port = self._choose_port()
            token = secrets.token_urlsafe(24)
            command = [
                sys.executable, "-m", "pal.main", "browser-service",
                "--runtime-root", str(self.runtime_root), "--host", self.host,
                "--port", str(port),
                "--idle-timeout-seconds", str(max(5, int(self.idle_timeout_seconds))),
                "--max-concurrency", str(max(1, int(self.max_concurrency))),
            ]
            child_env = dict(os.environ)
            child_env["PAL_BROWSER_SERVICE_TOKEN"] = token
            process = subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=str(Path(self.runtime_root).parent), env=child_env,
                start_new_session=os.name != "nt",
            )
            resource = _BrowserServiceProcess(process=process, host=self.host, port=port, token=token)
            self._process = resource
            self.port = port
            self.token = token
            deadline = time.monotonic() + 60.0
            while time.monotonic() < deadline:
                if not self._process_running(resource):
                    break
                try:
                    payload = self._request_json("GET", "/health", None, timeout_seconds=0.25, resource=resource)
                except Exception:
                    time.sleep(0.1)
                    continue
                if bool(payload.get("ok")):
                    self.last_error = ""
                    return resource
            self.last_error = "browser service failed to start"
            self.stop_sync()
            raise BrowserServiceError(self.last_error, code="sidecar_start_failed", retryable=True)

    def _request_json(
        self, method: str, path: str, payload: dict[str, Any] | None,
        *, timeout_seconds: float, resource: _BrowserServiceProcess,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"http://{resource.host}:{resource.port}{path}", data=body, method=method,
            headers={"Authorization": f"Bearer {resource.token}", "Content-Type": "application/json"},
        )
        try:
            response = urlopen(request, timeout=max(0.1, float(timeout_seconds)))  # noqa: S310
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                decoded = json.loads(raw)
            except ValueError:
                decoded = {"ok": False, "error": {"code": "http_error", "message": raw or str(exc)}}
            return decoded if isinstance(decoded, dict) else {"ok": False}
        with response:
            decoded = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
        if not isinstance(decoded, dict):
            raise BrowserServiceError("browser service returned invalid JSON", code="invalid_sidecar_output")
        return decoded

    @staticmethod
    def _process_running(resource: _BrowserServiceProcess | None) -> bool:
        return resource is not None and resource.process.poll() is None

    def _choose_port(self) -> int:
        if self.port is not None:
            return int(self.port)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((self.host, 0))
            return int(sock.getsockname()[1])


__all__ = [
    "BrowserRuntimePaths", "BrowserServiceError", "BrowserServiceManager",
    "DEFAULT_IDLE_TIMEOUT_SECONDS", "DEFAULT_MAX_CONCURRENCY", "NODE_MINIMUM_MAJOR",
    "PLAYWRIGHT_CLI_PACKAGE", "PLAYWRIGHT_CLI_VERSION", "PROFILE_MAX_BYTES",
    "PROFILE_RETENTION_SECONDS", "SCREENSHOT_MAX_BYTES", "browser_session_key",
    "run_browser_service_cli",
]
