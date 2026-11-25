import asyncio
from unittest.mock import patch

import pytest

from pal.bunshin.coroutine_runtime import CoroutineRunSemaphore, NodeSlot
from pal.bunshin.interaction_suspend import InteractionParked
from pal.llm import generation_result_from_values
from pal.shared.tool_protocol import ToolCallIR, ToolExecutionResult
from tests.test_bunshin_completion_resume import make_bundle, make_runner


def test_one_slot_handoff_and_dependency_yield():
    async def run():
        keep = {node := NodeSlot("w", "e", 1, "a")}
        settled = set()
        pool = CoroutineRunSemaphore(1, keep.__contains__, settled.__contains__)
        coder = await pool.acquire("a-coder", node=node)
        coder.assignment_id = "first"
        other = asyncio.create_task(pool.acquire("b-coder", node=NodeSlot("w", "e", 1, "b")))
        await coder.release(reserve_successor=True)
        await asyncio.sleep(0)
        assert not other.done() and pool.active_count == 0
        waiting = asyncio.create_task(pool.acquire("a-verifier", node=node))
        await asyncio.sleep(0)
        assert not waiting.done()
        settled.add("first")
        await pool.reconcile_reservations()
        verifier = await asyncio.wait_for(waiting, 1)
        assert not other.done()
        await verifier.release(reserve_successor=True)
        keep.clear()
        await pool.reconcile_reservations()
        await (await asyncio.wait_for(other, 1)).release()
        assert not pool.reserved_nodes
    asyncio.run(run())


@pytest.mark.parametrize("two_questions", [False, True])
def test_encrypted_parked_batch_resumes_at_same_call(tmp_path, two_questions):
    async def scenario():
        first, second, third = make_bundle(), make_bundle(), make_bundle()
        output = tmp_path / "park.json"
        effects, events = [], []
        class Client:
            async def request(self, method, params):
                events.append(method)
                if method == "interaction_resume":
                    return {"answer": {"decision": {"decision": "accept", "approval_id": params["wait_id"]}}}
                return {"ok": True}
        calls = [ToolCallIR(name="op_read_file", call_id=key, args={"path": key}) for key in ("prefix", "ask", "suffix")]
        async def execute(runner, state, cont, call, **kwargs):
            if call.call_id == "ask":
                decision = await runner.components.control.request_approval("op_read_file", call)
                assert decision == "accept"
                if two_questions:
                    assert await runner.components.control.request_approval("op_read_file", call) == "accept"
            effects.append(call.call_id)
            return ToolExecutionResult(name=call.name, call_id=call.call_id, ok=True, llm_text="ok")

        async def run(bundle, runner):
            runner.pack.metadata["agent_session"]["durable_tool_interactions"] = True
            async def tool(*args, **kwargs):
                return await execute(runner, *args, **kwargs)
            with patch("pal.bunshin.runner_components.interactive_batch.role_gateway_client_from_env", return_value=Client()), patch.object(
                runner.components.agent_runtime.tool_observation, "execute_bunshin_tool_with_observation",
                side_effect=tool,
            ):
                return await runner.components.agent_session.run_agent_loop(bundle)
        try:
            with patch.object(first.llm_runtime, "_generate_response", return_value=generation_result_from_values(
                tool_calls=calls, finish_reason="tool_calls",
            )):
                with pytest.raises(InteractionParked):
                    await run(first, make_runner(tmp_path, output=output))
            assert effects == ["prefix"]
            resumed = make_runner(tmp_path, output=tmp_path / "next.json", restore=output, token=2)
            if two_questions:
                with pytest.raises(InteractionParked):
                    await run(second, resumed)
                assert effects == ["prefix"]
                await run(third, make_runner(tmp_path, output=tmp_path / "third.json", restore=tmp_path / "next.json", token=3))
            else:
                await run(second, resumed)
            assert effects == ["prefix", "ask", "suffix"]
            assert events == ["interaction_park", "interaction_resume"] * (2 if two_questions else 1) + ["interaction_consumed"]
        finally:
            first.execution_runtime.shutdown()
            second.execution_runtime.shutdown()
            third.execution_runtime.shutdown()
    asyncio.run(scenario())


def test_recovered_worker_group_counts_once_until_it_exits():
    async def scenario():
        pool = CoroutineRunSemaphore(2)
        with patch("pal.bunshin.coroutine_runtime.inherited_process_group_live", return_value=True) as observed:
            await pool.recover_occupancy((("old", "a", 123, "original"), ("old", "a", 123, "original")))
            assert pool.inherited_count == 1
            other = await pool.try_acquire("other")
            assert other and await pool.try_acquire("third") is None
            handoff = asyncio.create_task(pool.acquire("checker", node=NodeSlot("w", "e", 1, "a")))
            await other.release()
            await asyncio.sleep(0)
            assert not handoff.done()
            observed.return_value = False
            await pool.recheck_occupancy()
            await (await asyncio.wait_for(handoff, 1)).release()
    asyncio.run(scenario())


def test_unrelated_worktree_holder_does_not_reserve_worker_capacity(tmp_path):
    import subprocess

    from pal.bunshin.repository import BunshinRepository

    leases = BunshinRepository(tmp_path / "runtime").leases
    leases.claim_lease("old", "gone-worker", metadata={
        "run_id": "old", "aggregate_id": "a", "workspace_path": str(tmp_path),
    })

    async def run():
        pool = CoroutineRunSemaphore(1)
        await pool.recover_occupancy(leases.inherited_role_process_groups())
        permit = await pool.try_acquire("new")
        assert permit is not None
        await permit.release()

    with subprocess.Popen(["sleep", "30"], cwd=tmp_path) as ordinary_process:
        try:
            asyncio.run(run())
        finally:
            ordinary_process.terminate()


def test_resize_checks_live_capacity_before_promoting_a_reservation():
    async def run():
        pool = CoroutineRunSemaphore(2)
        first, second = (NodeSlot("w", "e", 1, node) for node in ("a", "b"))
        for node in (first, second):
            coder = await pool.acquire("coder-" + node.node_id, node=node)
            await coder.release(reserve_successor=True)
        await pool.resize(1)
        verifier = await pool.acquire("verifier-a", node=first)
        waiter = asyncio.create_task(pool.acquire("verifier-b", node=second))
        await asyncio.sleep(0)
        assert pool.active_count == 1
        assert not waiter.done()
        await verifier.release()
        await (await asyncio.wait_for(waiter, 1)).release()

    asyncio.run(run())


def test_inherited_supervisor_group_retains_capacity_after_leader_exit(tmp_path):
    import os
    from pathlib import Path
    import signal
    import socket
    import subprocess
    import sys

    from pal.bunshin.workspace_resources import process_group_birth

    supervisor = Path(__file__).resolve().parents[1] / "src/pal/bunshin/process_group_supervisor.py"
    manager, lifeline = socket.socketpair()
    with manager, lifeline, subprocess.Popen(
        [sys.executable, str(supervisor), str(lifeline.fileno()),
         sys.executable, "-c", "import os,time; print(os.getpid(), flush=True); time.sleep(30)"],
        cwd=tmp_path, stdout=subprocess.PIPE, text=True, start_new_session=True,
        pass_fds=(lifeline.fileno(),),
    ) as leader:
        try:
            child = int(leader.stdout.readline())
            birth = process_group_birth(leader.pid)
            assert birth and os.getpgid(child) == leader.pid
            leader.kill()
            leader.wait(timeout=5)
            assert os.getpgid(child) == leader.pid

            async def run():
                pool = CoroutineRunSemaphore(1)
                await pool.recover_occupancy((("old", "a", leader.pid, birth),))
                assert pool.inherited_count == 1
                assert await pool.try_acquire("new") is None

            asyncio.run(run())
        finally:
            try:
                os.killpg(leader.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_mixed_verifier_suite_requires_an_explicit_runner(tmp_path):
    import subprocess

    from pal.bunshin.repair_precheck import repair_precheck_plan, repair_receipt_fields, suite_command

    scope = {"kind": "directory", "path": "tests"}
    python_corpus = ["tests/test_parser.py", "tests/cases.json", "tests/expected.golden", "tests/sample.bin"]
    assert "pytest" in suite_command(scope, python_corpus)
    for native in ("tests/test_native.sh", "tests/parser_tests.cpp", "tests/native_check.sh"):
        mixed_corpus = [*python_corpus, native]
        assert not suite_command(scope, mixed_corpus)
        assert suite_command(scope, [*mixed_corpus, "tests/run-tests.sh"]) == "sh tests/run-tests.sh"

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    corpus = tmp_path / "tests"
    corpus.mkdir()
    (corpus / "test_parser.py").write_text("def test_parser(): assert True\n")
    (corpus / "parser_tests.cpp").write_text("int main() { return 1; }\n")
    plan = repair_precheck_plan(tmp_path, scope)
    assert repair_receipt_fields(plan)["status"] == "requires_verifier"
    (corpus / "run-tests.sh").write_text("exit 1\n")
    assert repair_precheck_plan(tmp_path, scope)["command"] == "sh tests/run-tests.sh"
