from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterator


class PackageError(RuntimeError):
    pass


def error_text(value: object) -> str:
    from pal.foundation.service_logging import redact_service_log_text
    text = redact_service_log_text(value)
    return re.sub(r"(https?://)[^/\s]+@", r"\1<redacted>@", text)[-4000:]


_local = threading.local()


class CommandControl:
    def __init__(self):
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.process = None

    def stop(self):
        self.stopping.set()
        with self.lock:
            if self.process is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)


def check_cancelled() -> None:
    control = getattr(_local, "control", None)
    if control and control.stopping.is_set():
        raise PackageError("Installation interrupted; retry the same package operation")


@contextlib.contextmanager
def command_control(control: CommandControl):
    _local.control = control
    try:
        yield
    finally:
        del _local.control


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextlib.contextmanager
def install_lock(runtime_root: Path) -> Iterator[None]:
    root = runtime_root / "packages"
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".install.lock").open("a") as lock:
        while True:
            check_cancelled()
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextlib.contextmanager
def runtime_lease(runtime_root: Path):
    """Prevent an offline installer from replacing files under a running host."""
    root = Path(runtime_root).expanduser().resolve() / "packages"
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".runtime.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PackageError("Pal is running on this runtime root. Use package_install inside Pal for a coordinated switch, or stop Pal before CLI installation.") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def run_command(argv: list[str], *, env: dict[str, str] | None = None,
                cwd: Path | None = None, timeout: float = 900) -> str:
    # File-backed output avoids unbounded RAM when pip/npm are verbose.
    with tempfile.TemporaryFile() as output:
        check_cancelled()
        process = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=output, start_new_session=True)
        control = getattr(_local, "control", None)
        if control:
            with control.lock:
                control.process = process
                if control.stopping.is_set():
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
        try:
            process.wait(timeout=timeout)
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
        finally:
            if control:
                with control.lock:
                    control.process = None
        output.seek(0, os.SEEK_END)
        output.seek(max(0, output.tell() - 4000))
        detail = output.read().decode("utf-8", errors="replace")
    if process.returncode:
        raise PackageError(f"Command {Path(argv[0]).name} failed ({process.returncode}): {error_text(detail)}")
    return detail
