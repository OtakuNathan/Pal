from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from pal.packages.integration import RuntimeActivation
from pal.packages.process import CommandControl, PackageError, atomic_json, command_control, error_text
from pal.packages.service import PackageService


@dataclass
class _Completion:
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    background: bool = False
    callback: Callable[[dict], None] | None = None


class PackageJobs:
    def __init__(self, host):
        self.service = PackageService(host.runtime_root, activation=RuntimeActivation(host))
        self.root = self.service.root / "jobs"
        self.threads: dict[str, tuple[threading.Thread, CommandControl]] = {}
        self.lock = threading.Lock()
        self.completions: dict[str, _Completion] = {}
        self.closed = False

    def start(self, operation: str, *, wait_ms: int = 0,
              on_complete: Callable[[dict], None] | None = None, **args) -> dict:
        _validate_wait(wait_ms)
        with self.lock:
            if self.closed:
                raise PackageError("Package manager is stopping")
            self.threads = {key: value for key, value in self.threads.items() if value[0].is_alive()}
            if self.threads:
                raise PackageError("A package operation is already running; inspect package_status before retrying")
            self.completions.clear()
            job_id = uuid.uuid4().hex
            state = dict(job_id=job_id, operation=operation, status="running", started_at=time.time())
            atomic_json(self.root / f"{job_id}.json", state)
            control = CommandControl()
            completion = _Completion(callback=on_complete)
            self.completions[job_id] = completion
            thread = threading.Thread(target=self._run, args=(state, operation, args, control, completion), daemon=True,
                                      name="pal-package-install")
            self.threads[job_id] = (thread, control)
            thread.start()
        completion.done.wait(wait_ms / 1000)
        with completion.lock:
            if completion.done.is_set():
                return dict(state)
            completion.background = True
            state["notification"] = "scheduled" if on_complete else "unavailable"
            state["next_step"] = (
                "Completion will be delivered to Pal to continue the initiating task when it is idle; do not poll or repeat the operation."
                if on_complete else
                "No completion notice is available; use package_status with this job_id and a bounded wait_ms. Do not repeat the operation."
            )
            atomic_json(self.root / f"{job_id}.json", state)
            return dict(state)

    def _run(self, state: dict, operation: str, args: dict, control: CommandControl, completion: _Completion):
        try:
            with command_control(control):
                result = getattr(self.service, operation)(**args)
            updates = dict(status="ready", result=result)
        except Exception as exc:
            updates = dict(status="failed", error=error_text(exc))
        with completion.lock:
            state.update(**updates, finished_at=time.time())
            state.pop("next_step", None)
            notify = completion.callback if completion.background else None
            state["notification"] = "pending" if notify else "not_needed" if not completion.background else "unavailable"
            atomic_json(self.root / f"{state['job_id']}.json", state)
            completion.done.set()
        if notify:
            try:
                notify(dict(state))
                notification_update = dict(notification="queued")
            except Exception as exc:
                notification_update = dict(notification="failed", notification_error=error_text(exc))
            with completion.lock:
                state.update(notification_update)
                atomic_json(self.root / f"{state['job_id']}.json", state)

    def status(self, job_id: str | None = None, *, wait_ms: int = 0) -> dict:
        _validate_wait(wait_ms)
        if wait_ms and not job_id:
            raise PackageError("wait_ms requires job_id")
        if job_id:
            if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
                raise PackageError("Invalid job id")
            paths = [self.root / f"{job_id}.json"]
            with self.lock:
                completion = self.completions.get(job_id)
            if completion is not None and wait_ms:
                completion.done.wait(wait_ms / 1000)
        else:
            paths = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)[:20]
        jobs = []
        with self.lock:
            active = {key for key, value in self.threads.items() if value[0].is_alive()}
        for path in paths:
            if path.is_file():
                state = json.loads(path.read_text())
                if state["status"] == "running" and state["job_id"] not in active:
                    state.update(status="interrupted", next_step="Retry the same package operation, including its uninstall data option")
                jobs.append(state)
        return {"jobs": jobs, **self.service.status()}

    def shutdown(self):
        with self.lock:
            self.closed = True
            tasks = list(self.threads.values())
        for _, control in tasks:
            control.stop()
        for thread, _ in tasks:
            thread.join(timeout=10)


def _validate_wait(wait_ms: int) -> None:
    if isinstance(wait_ms, bool) or not isinstance(wait_ms, int) or not 0 <= wait_ms <= 5000:
        raise PackageError("wait_ms must be an integer between 0 and 5000")
