from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from pal.packages.integration import RuntimeActivation
from pal.packages.process import CommandControl, PackageError, atomic_json, command_control, error_text
from pal.packages.service import PackageService


class PackageJobs:
    def __init__(self, host):
        self.service = PackageService(host.runtime_root, activation=RuntimeActivation(host))
        self.root = self.service.root / "jobs"
        self.threads: dict[str, tuple[threading.Thread, CommandControl]] = {}
        self.lock = threading.Lock()
        self.closed = False

    def start(self, operation: str, **args) -> dict:
        with self.lock:
            if self.closed:
                raise PackageError("Package manager is stopping")
            self.threads = {key: value for key, value in self.threads.items() if value[0].is_alive()}
            if self.threads:
                raise PackageError("An installation is already running; inspect package_status before retrying")
            job_id = uuid.uuid4().hex
            state = dict(job_id=job_id, operation=operation, status="running", started_at=time.time())
            atomic_json(self.root / f"{job_id}.json", state)
            control = CommandControl()
            thread = threading.Thread(target=self._run, args=(state, operation, args, control), daemon=True,
                                      name="pal-package-install")
            self.threads[job_id] = (thread, control)
            thread.start()
            return dict(state)

    def _run(self, state: dict, operation: str, args: dict, control: CommandControl):
        try:
            with command_control(control):
                result = getattr(self.service, operation)(**args)
            state.update(status="ready", result=result)
        except Exception as exc:
            state.update(status="failed", error=error_text(exc))
        state["finished_at"] = time.time()
        atomic_json(self.root / f"{state['job_id']}.json", state)

    def status(self, job_id: str | None = None) -> dict:
        if job_id:
            if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
                raise PackageError("Invalid job id")
            paths = [self.root / f"{job_id}.json"]
        else:
            paths = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)[:20]
        jobs = []
        with self.lock:
            active = {key for key, value in self.threads.items() if value[0].is_alive()}
        for path in paths:
            if path.is_file():
                state = json.loads(path.read_text())
                if state["status"] == "running" and state["job_id"] not in active:
                    state.update(status="interrupted", next_step="Retry the same install or prepare operation")
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
