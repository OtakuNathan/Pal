from __future__ import annotations

import io
import logging
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pal.foundation.log_paths import pal_log_path


PAL_SERVICE_LOG_SINK_ENV = "PAL_SERVICE_LOG_SINK"
PAL_SERVICE_LOG_TAG_ENV = "PAL_SERVICE_LOG_TAG"

SERVICE_LOG_SINK_STDIO = "stdio"
SERVICE_LOG_SINK_SYSTEMD_JOURNAL = "systemd_journal"
SERVICE_LOG_SINK_MACOS_UNIFIED = "macos_unified"

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


@dataclass(frozen=True)
class ServiceLogPlan:
    kind: str
    tag: str
    prompt_debug_path: Path
    journal_command: str = ""
    stream_command: str = ""

    def environment(self) -> dict[str, str]:
        if self.kind == "systemd_journal":
            sink = SERVICE_LOG_SINK_SYSTEMD_JOURNAL
        elif self.kind == SERVICE_LOG_SINK_MACOS_UNIFIED:
            sink = SERVICE_LOG_SINK_MACOS_UNIFIED
        else:
            sink = SERVICE_LOG_SINK_STDIO
        return {
            PAL_SERVICE_LOG_SINK_ENV: sink,
            PAL_SERVICE_LOG_TAG_ENV: self.tag,
        }

    def summary_entries(self) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        if self.journal_command:
            entries.append(("Journal", self.journal_command))
        elif self.stream_command:
            entries.append(("Unified log", self.stream_command))
        else:
            entries.append(("Service log", "stdout/stderr"))
        entries.append(("Prompt debug log", str(self.prompt_debug_path)))
        return entries


def service_log_plan(
    runtime_root: Path | str | None,
    *,
    service_name: str = "pal",
    platform_name: str | None = None,
) -> ServiceLogPlan:
    system = str(platform_name or platform.system() or "").lower()
    tag = _safe_log_tag(service_name)
    if system == "linux":
        unit = str(service_name or "pal").strip() or "pal"
        return ServiceLogPlan(
            kind="systemd_journal",
            tag=tag,
            prompt_debug_path=pal_log_path(runtime_root),
            journal_command=f"journalctl --user -u {unit} -f",
        )
    if system == "darwin":
        return ServiceLogPlan(
            kind=SERVICE_LOG_SINK_MACOS_UNIFIED,
            tag=tag,
            prompt_debug_path=pal_log_path(runtime_root),
            stream_command=f"log stream --style compact --predicate 'eventMessage CONTAINS \"[{tag}]\"'",
        )
    return ServiceLogPlan(
        kind=SERVICE_LOG_SINK_STDIO,
        tag=tag,
        prompt_debug_path=pal_log_path(runtime_root),
    )


def service_log_environment(
    runtime_root: Path | str | None,
    *,
    service_name: str = "pal",
    platform_name: str | None = None,
) -> dict[str, str]:
    return service_log_plan(runtime_root, service_name=service_name, platform_name=platform_name).environment()


def current_service_log_sink_description() -> str:
    sink = str(os.environ.get(PAL_SERVICE_LOG_SINK_ENV) or "").strip() or _default_sink_for_platform()
    tag = str(os.environ.get(PAL_SERVICE_LOG_TAG_ENV) or "pal").strip() or "pal"
    if sink == SERVICE_LOG_SINK_MACOS_UNIFIED:
        return f"macos_unified:{tag}"
    if sink == SERVICE_LOG_SINK_SYSTEMD_JOURNAL:
        return "systemd_journal"
    if sink == SERVICE_LOG_SINK_STDIO:
        return "stdio"
    return sink


def configure_process_logging(*, component: str = "pal", level: int = logging.INFO) -> None:
    sink = str(os.environ.get(PAL_SERVICE_LOG_SINK_ENV) or "").strip() or _default_sink_for_platform()
    tag = _safe_log_tag(os.environ.get(PAL_SERVICE_LOG_TAG_ENV) or component)
    if sink == SERVICE_LOG_SINK_MACOS_UNIFIED and platform.system().lower() == "darwin":
        _configure_macos_unified_logging(tag=tag, level=level)
        return
    logging.basicConfig(level=level, format=_LOG_FORMAT, stream=sys.stderr, force=True)


def _configure_macos_unified_logging(*, tag: str, level: int) -> None:
    try:
        import syslog
    except Exception:
        logging.basicConfig(level=level, format=_LOG_FORMAT, stream=sys.stderr, force=True)
        return
    stdout = _SyslogTextStream(syslog_module=syslog, tag=tag, priority=syslog.LOG_INFO)
    stderr = _SyslogTextStream(syslog_module=syslog, tag=tag, priority=syslog.LOG_ERR)
    sys.stdout = stdout
    sys.stderr = stderr
    logging.basicConfig(level=level, format=_LOG_FORMAT, stream=stderr, force=True)


class _SyslogTextStream(io.TextIOBase):
    def __init__(self, *, syslog_module: Any, tag: str, priority: int) -> None:
        self._syslog = syslog_module
        self._tag = _safe_log_tag(tag)
        self._priority = priority
        self._buffer = ""
        try:
            self._syslog.openlog(self._tag)
        except Exception:
            pass

    @property
    def encoding(self) -> str:
        return "utf-8"

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def write(self, text: str) -> int:
        value = str(text or "")
        self._buffer += value
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit(line)
        return len(value)

    def flush(self) -> None:
        if not self._buffer:
            return
        line = self._buffer
        self._buffer = ""
        self._emit(line)

    def _emit(self, line: str) -> None:
        text = line.strip()
        if not text:
            return
        try:
            self._syslog.syslog(self._priority, f"[{self._tag}] {text}")
        except Exception:
            pass


def _default_sink_for_platform() -> str:
    return SERVICE_LOG_SINK_STDIO


def _safe_log_tag(value: object) -> str:
    text = str(value or "pal").strip() or "pal"
    safe = []
    for char in text:
        if char.isalnum() or char in {"-", "_", "."}:
            safe.append(char)
        else:
            safe.append("_")
    return "".join(safe).strip("._")[:80] or "pal"
