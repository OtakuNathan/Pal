from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pal.core import PalCore, TurnContinuation, register_with_core
from pal.core.prompt_compiler import PromptCompiler
from pal.core.prompt_fragment_registry import PromptFragmentRegistry
from pal.foundation import EventEnvelope
from pal.memory import MemoryService, register_with_core as register_memory
from pal.shared import PromptAssemblyContext


@pytest.mark.parametrize("error_type", [OSError, KeyError])
def test_compiler_exposes_redacted_memory_load_failure(error_type):
    memory = SimpleNamespace(build_pack=Mock(side_effect=error_type("history unavailable token=private-value")))
    compiler = PromptCompiler(SimpleNamespace(
        prompt_fragment_registry=PromptFragmentRegistry(), execution_runtime=None,
        require_port=lambda _: memory, port_registry={},
    ))
    request = compiler.build_canonical_prompt(PromptAssemblyContext())
    text = request.metadata["runtime_reminder_text"]
    assert "Memory context loading failed" in text
    assert error_type.__name__ in text and "history unavailable" in text
    assert "private-value" not in text
    assert "does not mean no earlier conversation" in text
    assert memory.build_pack.call_count == 1


def test_host_without_memory_service_does_not_report_a_failed_load():
    compiler = PromptCompiler(SimpleNamespace(
        prompt_fragment_registry=PromptFragmentRegistry(), execution_runtime=None,
        require_port=Mock(side_effect=KeyError("memory:memory")), port_registry={},
    ))
    assert compiler.build_canonical_prompt(PromptAssemblyContext()).metadata["runtime_reminder_text"] == ""


@pytest.mark.parametrize("implicit", [False, True])
def test_memory_failure_keeps_deep_root_cause_without_stack_frames(implicit):
    error = OSError("history.db: permission denied token=PRIVATE_CANARY")
    for index in range(5):
        wrapper = RuntimeError(f"memory layer {index}")
        if implicit:
            wrapper.__context__ = error
        else:
            wrapper.__cause__ = error
        error = wrapper
    memory = SimpleNamespace(build_pack=Mock(side_effect=error))
    compiler = PromptCompiler(SimpleNamespace(
        prompt_fragment_registry=PromptFragmentRegistry(), execution_runtime=None,
        require_port=lambda _: memory, port_registry={},
    ))
    text = compiler.build_canonical_prompt(PromptAssemblyContext()).metadata["runtime_reminder_text"]
    assert "history.db: permission denied" in text
    assert "PRIVATE_CANARY" not in text
    assert "Traceback" not in text


def test_active_turn_load_failure_reaches_model_without_a_hidden_retry(monkeypatch):
    core, memory = PalCore(), MemoryService()
    register_with_core(core)
    register_memory(core.context, memory)
    reader = Mock(side_effect=OSError("active history unavailable"))
    monkeypatch.setattr(memory, "active_l1_turn", reader)
    try:
        request = core.turn_executor.build_turn_prompt(
            TurnContinuation(turn_id="failed-active", program=iter(()), correlation_id="failed-active"),
            PromptAssemblyContext(event=EventEnvelope(
                event_kind="user.message", source_kind="channel", payload={"text": "Current input"},
            )), max_output_tokens=128,
        )
        text = "\n".join(message.text for message in request.messages)
        assert "Current input" in text
        assert "active_turn: OSError: active history unavailable" in text
        assert reader.call_count == 1
    finally:
        core.context.execution_runtime.shutdown()


def test_turn_delivers_memory_failure_once_and_withdraws_it_after_recovery(monkeypatch):
    core, memory = PalCore(), MemoryService()
    register_with_core(core)
    register_memory(core.context, memory)
    memory.begin_l1_turn("memory-failure", user_text="Continue the earlier task")
    continuation = TurnContinuation(turn_id="memory-failure", program=iter(()), correlation_id="memory-failure")
    context = PromptAssemblyContext(event=EventEnvelope(
        event_kind="user.message", source_kind="channel", payload={"text": "Continue the earlier task"},
    ))
    original = memory.build_pack
    failed = Mock(side_effect=OSError("history database unavailable"))
    monkeypatch.setattr(memory, "build_pack", failed)
    try:
        def request():
            return core.turn_executor.build_turn_prompt(continuation, context, max_output_tokens=128)

        first = request()
        text = "\n".join(message.text for message in first.messages)
        assert "Continue the earlier task" in text
        assert "Memory context loading failed" in text
        assert "OSError: history database unavailable" in text
        assert failed.call_count == 1  # No hidden retry in the compiler.
        second = request()
        assert sum("Memory context loading failed" in message.text for message in second.messages) == 1
        assert failed.call_count == 2
        monkeypatch.setattr(memory, "build_pack", original)
        recovered = request()
        assert "memory_context_status" not in recovered.metadata["reminder_sections"]
        withdrawal = [message for message in recovered.messages
                      if message.metadata.get("context_title") == "Memory Context Status"
                      and message.metadata.get("withdrawn")]
        assert withdrawal
    finally:
        core.context.execution_runtime.shutdown()
