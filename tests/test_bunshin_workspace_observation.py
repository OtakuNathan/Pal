"""Workspace quiescence requires an observation, including without procfs."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from pal.bunshin.recovery import BunshinRecovery
from pal.bunshin.semantic_orchestration.workspace_safety import _raise_if_workspace_held
from pal.bunshin.workspace_resources import (
    WorkspaceProcessObservationError, workspace_process_holders,
)


@pytest.fixture
def without_proc(monkeypatch):
    original = Path.is_dir
    monkeypatch.setattr(Path, "is_dir", lambda path: False if str(path) == "/proc" else original(path))


@pytest.mark.parametrize("use_lsof", [False, True], ids=["platform-backend", "without-proc"])
def test_real_holder_blocks_snapshot_with_cwd_open_and_deleted_files(tmp_path, use_lsof, monkeypatch):
    if (use_lsof or not Path("/proc").is_dir()) and shutil.which("lsof") is None:
        pytest.skip("lsof is unavailable for the native workspace observation check")
    if use_lsof:
        original = Path.is_dir
        monkeypatch.setattr(Path, "is_dir", lambda path: False if str(path) == "/proc" else original(path))
    tmp_path = tmp_path / "workspace\nwith \\ literal and ^A"
    tmp_path.mkdir()
    read_name = "read file\nwith newline.txt"
    (tmp_path / read_name).write_text("content")
    script = (
        "import os, time; "
        f"r = open({read_name!r}, 'rb'); "
        "w = open('written.txt', 'ab'); "
        "d = open('removed.txt', 'ab'); os.unlink('removed.txt'); "
        "print('ready', flush=True); time.sleep(60)"
    )
    worker = subprocess.Popen([sys.executable, "-c", script], cwd=tmp_path,
        stdout=subprocess.PIPE, text=True)
    try:
        assert worker.stdout.readline().strip() == "ready"
        holders = workspace_process_holders(tmp_path)
        holder = next(item for item in holders if item.pid == worker.pid)
        assert holder.holds_cwd
        assert read_name in holder.read_paths
        assert {"written.txt", "removed.txt"} <= set(holder.write_paths)
        assert worker.poll() is None
        with pytest.raises(RuntimeError, match="still holds workspace"):
            _raise_if_workspace_held(tmp_path, "still holds workspace")
    finally:
        worker.kill()
        worker.wait(timeout=5)
        worker.stdout.close()
    assert workspace_process_holders(tmp_path) == ()


def test_lsof_retains_manager_snapshot_lock_exemption(tmp_path, without_proc):
    if shutil.which("lsof") is None:
        pytest.skip("lsof is unavailable")
    lock = tmp_path / "snapshot.lock"
    with lock.open("ab"):
        with pytest.raises(RuntimeError, match="held"):
            _raise_if_workspace_held(tmp_path, "held")
        _raise_if_workspace_held(tmp_path, "held", manager_snapshot_lock=lock)


@pytest.mark.parametrize("failure", ["missing", "timeout", "exit", "warning", "malformed"])
def test_unavailable_observation_cannot_clear_expired_lease(tmp_path, without_proc, failure):
    result = subprocess.CompletedProcess([], 0, "", "")
    error = None
    if failure == "missing":
        error = FileNotFoundError("lsof is missing")
    elif failure == "timeout":
        error = subprocess.TimeoutExpired("lsof", 15)
    elif failure == "exit":
        result.returncode = 2
    elif failure == "warning":
        result.stdout = f"p{os.getpid()}\0cpython\0\nf1\0aw\0n{tmp_path}/file.txt\0\n"
        result.stderr = "lsof: cannot inspect a process"
    else:
        result.stdout = "npath-without-process-identity\0\n"
    leases = SimpleNamespace(expired_leases=Mock(return_value=[{
        "resource_key": "workspace:held", "fencing_token": 9,
        "metadata": {"workspace_path": str(tmp_path)},
    }]), clear_expired_lease=Mock())
    service = SimpleNamespace(runtime_root=tmp_path, repository=SimpleNamespace(leases=leases))
    with patch("pal.bunshin.workspace_resources.subprocess.run", return_value=result, side_effect=error), \
            patch("pal.bunshin.recovery.LspManagerClient"):
        with pytest.raises(WorkspaceProcessObservationError):
            BunshinRecovery(service).recover()
        with pytest.raises(WorkspaceProcessObservationError):
            _raise_if_workspace_held(tmp_path, "cannot snapshot")
    leases.clear_expired_lease.assert_not_called()


@pytest.mark.parametrize("code", [0, 1])
def test_successful_empty_observation_distinguishes_workspace_prefix(tmp_path, without_proc, code):
    result = subprocess.CompletedProcess([], code,
        f"p{os.getpid()}\0cpython\0\nf1\0aw\0n{tmp_path}-sibling/file.txt\0\n", "")
    with patch("pal.bunshin.workspace_resources.subprocess.run", return_value=result):
        assert workspace_process_holders(tmp_path) == ()
        _raise_if_workspace_held(tmp_path, "cannot snapshot")


@pytest.mark.parametrize("directory,encoded", [
    ("workspace\x01", "workspace^A"), ("workspace^A", "workspace^A"),
    ("workspace^A\x02", "workspace^A^B"),
    ("workspace (deleted)", "workspace (deleted)"),
    ("workspaceé", "workspace\\xc3\\xa9"),
])
def test_lsof_escaped_paths_cannot_hide_workspace_holders(tmp_path, without_proc, directory, encoded):
    workspace = tmp_path / directory
    workspace.mkdir()
    result = subprocess.CompletedProcess([], 0,
        f"p{os.getpid()}\0cpython\0\nfcwd\0n{tmp_path}/{encoded}\0\n", "")
    with patch("pal.bunshin.workspace_resources.subprocess.run", return_value=result):
        assert workspace_process_holders(workspace)[0].holds_cwd
        with pytest.raises(RuntimeError, match="held"):
            _raise_if_workspace_held(workspace, "held")
