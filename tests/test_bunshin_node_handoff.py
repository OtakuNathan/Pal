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


def test_recovered_workspace_counts_once_until_its_holder_leaves(tmp_path):
    async def scenario():
        pool = CoroutineRunSemaphore(2)
        with patch("pal.bunshin.coroutine_runtime.workspace_process_holders", return_value=("live",)) as holders:
            await pool.recover_occupancy((("old", "a", str(tmp_path)), ("old", "a", str(tmp_path))))
            assert pool.inherited_count == 1
            other = await pool.try_acquire("other")
            assert other and await pool.try_acquire("third") is None
            handoff = asyncio.create_task(pool.acquire("checker", node=NodeSlot("w", "e", 1, "a")))
            await other.release()
            await asyncio.sleep(0)
            assert not handoff.done()
            holders.return_value = ()
            await pool.recheck_occupancy()
            await (await asyncio.wait_for(handoff, 1)).release()
    asyncio.run(scenario())
