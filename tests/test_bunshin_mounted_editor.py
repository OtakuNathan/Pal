"""Exercise both real mount namespaces and the lifetime of an exact-file edit grant."""
import asyncio
import inspect
import os
from pathlib import Path
import shutil
import socket
import sys

import pytest

from pal.bunshin.sandbox import build_sandboxed_runner_invocation
from pal.foundation.bwrap_companion import CHANNEL_ENV
from pal.shared import BunshinInvocationPack

pytestmark = pytest.mark.skipif(sys.platform != "linux" or not shutil.which("bwrap"), reason="requires bubblewrap")


def exercise_worker_tools():
    import asyncio
    import errno
    import hashlib
    import os
    import socket
    from pathlib import Path
    from pal.execution.file_state import FileContentChangedError
    from pal.execution.mounted_file_editor import commit_mounted_file as commit
    from pal.foundation.bwrap_companion import CHANNEL_ENV
    from pal.bunshin.scoped_execution import BunshinScopedExecutionRuntime
    from pal.core.main_context import MainContext
    from pal.core.runtime import PalCore
    from pal.execution import register_with_core
    from pal.execution.backend import build_execution_runtime
    from pal.shared.tool_protocol import new_tool_call

    assert not Path('/tmp/pal/bunshin').exists()
    for key in ('impl', 'other', 'frozen', 'fresh'):
        try:
            Path(key).write_text('raw write')
        except PermissionError:
            pass
        except OSError as exc:
            assert exc.errno == errno.EROFS
        else:
            raise AssertionError(key)
    old = Path('impl').stat()
    # Go through normal read delivery and edit admission.
    core = PalCore(context=MainContext(execution_runtime=build_execution_runtime()))
    register_with_core(core.context)
    core.publish_module_capabilities("execution")
    scoped = BunshinScopedExecutionRuntime(
        core.context.execution_runtime, ["op_file_read", "op_file_edit", "op_file_write"],
        workspace={"repo_path": str(Path.cwd()), "run_id": "edit", "coder_repository_read": True,
                   "write_path_scopes": [{"kind": "file", "path": key} for key in ("impl", "fresh")]},
    )
    async def edit():
        read = await scoped.execute_tool_async(new_tool_call(name="read_file", args={"file_path": str(Path("impl").resolve())}))
        assert read.ok, read.llm_text
        scoped.commit_tool_delivery(turn_id="edit:direct", context_delivery=read.context_delivery, result_id="read")
        changed = await scoped.execute_tool_async(new_tool_call(name="edit_file", args={
            "file_path": "impl", "edits": [{"old_string": "before", "new_string": "after"}]}))
        assert changed.ok, changed.llm_text
        created = await scoped.execute_tool_async(new_tool_call(name="write_file", args={"file_path": "fresh", "content": "created"}))
        assert created.ok, created.llm_text
    asyncio.run(edit())
    assert Path('impl').stat().st_ino != old.st_ino
    assert Path('impl').stat().st_mode == old.st_mode
    assert Path('impl').read_text() == 'after'
    try:
        commit('impl', expected_content='before', new_content='stale')
    except FileContentChangedError:
        pass
    else:
        raise AssertionError('stale commit succeeded')
    for key in ('frozen', 'other', './impl', '../impl'):
        try:
            commit(key, expected_content=None, new_content='unauthorized')
        except OSError as exc:
            assert 'outside' in str(exc)
        else:
            raise AssertionError(key)
    assert Path('fresh').read_text() == 'created'
    commit('fresh', delete_digest=hashlib.sha256(b'created').hexdigest())
    assert not Path('fresh').exists()
    with socket.socket(socket.AF_UNIX) as witness:
        witness.connect(str(Path('witness').resolve()))
        socket.send_fds(witness, [b'+'], [int(os.environ[CHANNEL_ENV])])


@pytest.mark.parametrize("cancelled", [False, True], ids=["worker-exit", "cancel"])
def test_private_editor_projects_atomic_updates_and_retires_with_role(tmp_path, monkeypatch, cancelled):
    repo = tmp_path / "worktree"
    repo.mkdir()
    target = repo / "impl"
    target.write_text("before")
    target.chmod(0o751)
    (repo / "frozen").write_text("contract")
    (repo / "other").write_text("sibling")
    endpoint = tmp_path / "runtime/data/bunshin-role/role.sock"
    endpoint.parent.mkdir(parents=True)
    endpoint.touch()
    pack = BunshinInvocationPack(
        invocation_id="module",
        workspace={"repo_path": str(repo), "coder_repository_read": True,
                   "write_path_scopes": [{"kind": "file", "path": key} for key in ("impl", "fresh", "frozen")],
                   "read_only_overlay_paths": ["frozen"]},
        metadata={"sandbox": {"enabled": True, "backend": "bwrap", "run_id": "role",
                              "scratch_dir": str(tmp_path / "scratch")}},
    )
    def invocation(script):
        return build_sandboxed_runner_invocation(
            runtime_root=tmp_path / "runtime", pack=pack,
            argv=[str(Path(sys.executable).resolve(strict=True)), "-c", script], env={"PATH": os.defpath},
        )

    from pal.bunshin.process_lifecycle import WorkerProcessOwner
    from pal.bunshin.workspace_resources import WorkspaceLockRegistry
    from pal.execution.mounted_file_editor import commit_mounted_file

    locks = WorkspaceLockRegistry()

    def owner(role, program):
        argv, env = invocation(program)
        return WorkerProcessOwner(argv=tuple(argv), env=env, invocation_id=role, run_id=role,
                                  workspace=repo, workspace_locks=locks, on_started=lambda owner: None,
                                  on_registered=lambda owner: None, on_unregistered=lambda owner: None)

    async def wait_ready(process):
        lines = process.stdout_lines()
        async for line in lines:
            if line == b"ready\n":
                break
        else:
            await process.close()
            raise AssertionError(process.stderr.decode() or "worker exited before signalling readiness")
        await lines.aclose()

    async def handover(witness):
        finish = "print('ready', flush=True)"
        wait = "\nimport sys; sys.stdin.readline()"
        script = inspect.getsource(exercise_worker_tools) + "\nexercise_worker_tools()\n"
        first = owner("first", script + finish + (wait if cancelled else ""))
        async with first:
            await asyncio.wait_for(wait_ready(first), 60)
            with witness.accept()[0] as peer:
                _, descriptors, _, _ = socket.recv_fds(peer, 1, 1)
            if not cancelled:
                assert await first.wait() == 0
        assert first.process_group_reaped and first.resources_released
        with socket.socket(fileno=descriptors[0]) as retired:
            second = owner("second", "from pal.execution.mounted_file_editor import commit_mounted_file; "
                           "commit_mounted_file('impl', expected_content='after', new_content='second'); " + finish + wait)
            async with second:
                await asyncio.wait_for(wait_ready(second), 60)
                monkeypatch.setenv(CHANNEL_ENV, str(retired.fileno()))
                with pytest.raises(OSError):
                    commit_mounted_file("impl", expected_content="second", new_content="stale role")
    with socket.socket(socket.AF_UNIX) as witness:
        witness.bind(str(repo / "witness"))
        witness.listen()
        asyncio.run(handover(witness))
    assert target.read_text() == "second"
    assert (repo / "frozen").read_text() == "contract"
    assert (repo / "other").read_text() == "sibling"
