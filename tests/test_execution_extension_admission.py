"""Execution implementation swaps retire captured entries without retargeting."""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict

from pal.core.main_context import MainContext
from pal.core.module_registry import ModuleHandle, MODULE_TIER_CORE_FOUNDATION, MODULE_TIER_DETACHABLE
from pal.execution.runtime import ExecutionRuntime
from pal.execution.tool_facade import CompleteResult, EmptyToolInput, RejectedResult
from pal.execution.tool_semantics import DIRECT_NONE
from pal.shared.tool_protocol import new_tool_call
from tests.capability_fixture import build_test_capability_handle, mount_test_capability


class ProbeOutput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    owner: str


class ProbeProvider:
    def __init__(self, owner):
        self.owner = owner
        self.calls = []
        self.before_call = lambda: None

    def invoke(self, _):
        self.before_call()
        self.calls.append(self.owner)
        return {"owner": self.owner}

    def build_mounted_subtree(self, *, module_id, **_):
        return build_test_capability_handle(
            alias="probe_execution", canonical_path="op_exec_probe", module_id=module_id,
            InputModel=EmptyToolInput, OutputModel=ProbeOutput, handler=self.invoke,
            execution=DIRECT_NONE,
        ).mounted_subtree


class ProbeExtension:
    def __init__(self):
        self.provider = ProbeProvider("extension")
        self.activate_callback = lambda: None
        self.close_callback = lambda: None
        self.closed = []

    def build_runtime(self, previous):
        self.candidate = ExecutionRuntime(sync_executor=previous.sync_executor)
        return self.candidate

    def build_provider(self, _):
        return self.provider

    def build_state_port(self, runtime):
        return runtime.build_runtime_state_port()

    def activate(self, *_):
        self.activate_callback()

    def check_detach(self, _):
        pass

    def close(self, runtime):
        self.closed.append(runtime)
        self.close_callback()


@pytest.fixture
def execution():
    context = MainContext()
    slot = context.execution_runtime
    provider = ProbeProvider("original")
    handle = ModuleHandle("execution", MODULE_TIER_CORE_FOUNDATION, introspection_provider=provider)
    context.register_module(handle)
    handle.published_capabilities = slot.mount_subtree(handle)
    mount_test_capability(
        slot, alias="unrelated_probe", canonical_path="op_other_probe", module_id="other",
        InputModel=EmptyToolInput, OutputModel=ProbeOutput,
        handler=lambda _: {"owner": "other"}, execution=DIRECT_NONE,
    )
    try:
        yield SimpleNamespace(context=context, slot=slot, original=slot.implementation, provider=provider,
                              handle=handle, owner=ModuleHandle("extension_owner", MODULE_TIER_DETACHABLE))
    finally:
        slot.shutdown()


def invoke(runtime, generation=None, *, asynchronous=False, alias="probe_execution"):
    call = new_tool_call(name=alias, args={})
    if asynchronous:
        return asyncio.run(runtime.invoke_direct_tool_async(call, generation=generation))
    return runtime.invoke_direct_tool(call, generation=generation)


def assert_withdrawn(result):
    assert isinstance(result, RejectedResult), result
    assert result.error_code == "unknown_tool"
    assert "withdrawn before admission" in result.error


def assert_owner(result, owner):
    assert isinstance(result, CompleteResult), result
    assert result.output == {"owner": owner}


@pytest.mark.parametrize("asynchronous", [False, True])
def test_install_and_uninstall_never_retarget_captured_entries(execution, asynchronous):
    state = execution
    original_generation = state.slot.registry_generation
    original_token = original_generation.direct_aliases["probe_execution"].binding.admission
    unrelated_token = original_generation.direct_aliases["unrelated_probe"].binding.admission
    extension = ProbeExtension()

    state.slot.install(extension, state.context, state.owner)
    candidate_generation = state.slot.registry_generation
    candidate_token = candidate_generation.direct_aliases["probe_execution"].binding.admission
    assert not original_token.live and candidate_token.live
    assert_withdrawn(invoke(state.original, original_generation, asynchronous=asynchronous))
    assert_owner(invoke(state.slot, asynchronous=asynchronous), "extension")
    assert_owner(invoke(state.slot, original_generation, asynchronous=asynchronous, alias="unrelated_probe"), "other")

    state.slot.uninstall(state.context, state.owner)
    restored_token = state.slot.registry_generation.direct_aliases["probe_execution"].binding.admission
    assert state.slot.implementation is state.original
    assert restored_token.live and restored_token is not original_token
    assert not candidate_token.live and not original_token.live
    assert unrelated_token.live
    assert_withdrawn(invoke(state.slot, original_generation, asynchronous=asynchronous))
    assert_withdrawn(invoke(extension.candidate, candidate_generation, asynchronous=asynchronous))
    assert_owner(invoke(state.slot, asynchronous=asynchronous), "original")
    assert state.provider.calls == ["original"]
    assert extension.provider.calls == ["extension"]


def test_failed_staging_preserves_original_admission(execution, monkeypatch):
    state = execution
    generation = state.slot.registry_generation
    extension = ProbeExtension()
    mount = ExecutionRuntime.mount_subtree

    def fail_after_mount(runtime, handle, **kwargs):
        mount(runtime, handle, **kwargs)
        if runtime is extension.candidate:
            raise RuntimeError("staging failed")

    monkeypatch.setattr(ExecutionRuntime, "mount_subtree", fail_after_mount)
    with pytest.raises(RuntimeError, match="staging failed"):
        state.slot.install(extension, state.context, state.owner)
    assert state.slot.implementation is state.original
    assert state.slot.registry_generation is generation
    assert generation.direct_aliases["probe_execution"].binding.admission.live
    assert_owner(invoke(state.slot, generation), "original")
    assert_withdrawn(invoke(extension.candidate))
    assert extension.closed == [extension.candidate]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_activation_rollback_uses_fresh_original_admission(execution, asynchronous):
    state = execution
    generation = state.slot.registry_generation
    extension = ProbeExtension()

    def fail_activation():
        assert not generation.direct_aliases["probe_execution"].binding.admission.live
        assert state.slot.implementation is extension.candidate
        raise RuntimeError("activation failed")

    extension.activate_callback = fail_activation
    with pytest.raises(RuntimeError, match="activation failed"):
        state.slot.install(extension, state.context, state.owner)
    assert state.slot.implementation is state.original
    assert state.context.introspection_registry["execution"] is state.provider
    assert state.slot.registry_generation is not generation
    assert_withdrawn(invoke(state.slot, generation, asynchronous=asynchronous))
    assert_withdrawn(invoke(extension.candidate, asynchronous=asynchronous))
    assert_owner(invoke(state.slot, asynchronous=asynchronous), "original")
    assert_owner(invoke(state.slot, generation, asynchronous=asynchronous, alias="unrelated_probe"), "other")
    assert extension.provider.calls == []
    assert extension.closed == [extension.candidate]


@pytest.mark.parametrize("fail_cleanup", [False, True])
def test_uninstall_restores_before_extension_cleanup(execution, fail_cleanup):
    state = execution
    extension = ProbeExtension()
    state.slot.install(extension, state.context, state.owner)
    generation = state.slot.registry_generation

    def check_cleanup():
        assert state.slot.implementation is state.original
        assert_withdrawn(invoke(extension.candidate, generation))
        assert_owner(invoke(state.slot), "original")
        if fail_cleanup:
            raise RuntimeError("cleanup failed")

    extension.close_callback = check_cleanup
    if fail_cleanup:
        with pytest.raises(RuntimeError, match="cleanup failed"):
            state.slot.uninstall(state.context, state.owner)
        restored_generation = state.slot.registry_generation
        assert state.slot._installed is not None
        with pytest.raises(RuntimeError, match="already active"):
            state.slot.install(ProbeExtension(), state.context, state.owner)
        extension.close_callback = lambda: None
        state.slot.uninstall(state.context, state.owner)
        assert state.slot.registry_generation is restored_generation
    else:
        state.slot.uninstall(state.context, state.owner)
    assert state.slot.implementation is state.original
    assert state.slot._installed is None
    assert extension.closed == [extension.candidate] * (2 if fail_cleanup else 1)


@pytest.mark.parametrize("rollback", [False, True])
def test_empty_candidate_subtree_loses_mount_authority(execution, rollback):
    state = execution
    extension = ProbeExtension()
    extension.provider = object()

    def activate():
        if rollback:
            raise RuntimeError("activation failed")

    extension.activate_callback = activate
    if rollback:
        with pytest.raises(RuntimeError, match="activation failed"):
            state.slot.install(extension, state.context, state.owner)
    else:
        state.slot.install(extension, state.context, state.owner)
        state.slot.uninstall(state.context, state.owner)
    candidate_subtree = extension.candidate.registry_generation.mounted_subtrees["execution"]
    assert candidate_subtree.bound_actions == ()
    assert not candidate_subtree.admission.live
    assert_owner(invoke(state.slot), "original")


def test_failed_restore_staging_keeps_extension_live(execution, monkeypatch):
    state = execution
    extension = ProbeExtension()
    state.slot.install(extension, state.context, state.owner)
    generation = state.slot.registry_generation

    def fail_hydration(_):
        raise RuntimeError("restoration staging failed")

    with monkeypatch.context() as patch:
        patch.setattr(state.original, "hydrate_module_handle", fail_hydration)
        with pytest.raises(RuntimeError, match="restoration staging failed"):
            state.slot.uninstall(state.context, state.owner)
    assert state.slot.implementation is extension.candidate
    assert state.slot.registry_generation is generation
    assert_owner(invoke(state.slot, generation), "extension")
    assert extension.closed == []
    state.slot.uninstall(state.context, state.owner)
    assert_withdrawn(invoke(extension.candidate, generation))
    assert_owner(invoke(state.slot), "original")


@pytest.mark.parametrize("rollback", [False, True])
def test_queued_original_and_candidate_entries_do_not_escape_swap(execution, monkeypatch, rollback):
    state = execution
    gate = state.slot.lifecycle_gate
    acquire_read = gate._acquire_read
    queued = threading.Event()
    extension = ProbeExtension()
    tasks = []

    def observe_read():
        queued.set()
        acquire_read()

    monkeypatch.setattr(gate, "_acquire_read", observe_read)

    def activate():
        candidate = extension.candidate
        tasks.append(asyncio.create_task(candidate.invoke_direct_tool_async(
            new_tool_call(name="probe_execution", args={}), generation=candidate.registry_generation)))
        if rollback:
            raise RuntimeError("activation failed")

    extension.activate_callback = activate

    async def scenario():
        original_generation = state.slot.registry_generation
        with gate.write():
            tasks.append(asyncio.create_task(state.original.invoke_direct_tool_async(
                new_tool_call(name="probe_execution", args={}), generation=original_generation)))
            assert await asyncio.to_thread(queued.wait, 2)
            queued.clear()
            if rollback:
                with pytest.raises(RuntimeError, match="activation failed"):
                    state.slot.install(extension, state.context, state.owner)
            else:
                state.slot.install(extension, state.context, state.owner)
            assert await asyncio.to_thread(queued.wait, 2)
            if not rollback:
                state.slot.uninstall(state.context, state.owner)
        for result in await asyncio.gather(*tasks):
            assert_withdrawn(result)
        assert state.provider.calls == []
        assert extension.provider.calls == []
        assert_owner(await state.slot.invoke_direct_tool_async(new_tool_call(name="probe_execution", args={})),
                     "original")

    asyncio.run(asyncio.wait_for(scenario(), timeout=6))


def test_admitted_original_handler_finishes_after_swap(execution):
    state = execution
    started, release = threading.Event(), threading.Event()

    def pause_handler():
        started.set()
        assert release.wait(5)

    state.provider.before_call = pause_handler
    generation = state.slot.registry_generation
    extension = ProbeExtension()
    with ThreadPoolExecutor(max_workers=1) as executor:
        admitted = executor.submit(invoke, state.original, generation)
        try:
            assert started.wait(2)
            state.slot.install(extension, state.context, state.owner)
            assert not generation.direct_aliases["probe_execution"].binding.admission.live
            assert not admitted.done()
        finally:
            release.set()
        assert_owner(admitted.result(timeout=2), "original")
    assert_withdrawn(invoke(state.original, generation))
    assert state.provider.calls == ["original"]


@pytest.mark.parametrize("phase", ["adopt", "provider", "activate"])
def test_failed_install_cleanup_is_retried_without_republishing(execution, monkeypatch, phase):
    state = execution
    extension = ProbeExtension()

    def fail(*args):
        raise RuntimeError("installation failed")

    def cleanup():
        raise RuntimeError("candidate cleanup pending")

    extension.close_callback = cleanup
    if phase == "adopt":
        monkeypatch.setattr(ExecutionRuntime, "adopt_host_state", fail)
    elif phase == "provider":
        extension.build_provider = fail
    else:
        extension.activate_callback = fail
    with pytest.raises(RuntimeError, match="installation failed"):
        state.slot.install(extension, state.context, state.owner)
    restored = state.slot.registry_generation
    assert state.slot.implementation is state.original
    assert_owner(invoke(state.slot), "original")
    with pytest.raises(RuntimeError, match="cleanup is pending"):
        state.slot.install(ProbeExtension(), state.context, state.owner)
    state.slot.uninstall(state.context, ModuleHandle("unrelated", MODULE_TIER_DETACHABLE))
    assert extension.closed == [extension.candidate]
    with pytest.raises(RuntimeError, match="candidate cleanup pending"):
        state.slot.uninstall(state.context, state.owner)
    assert extension.closed == [extension.candidate] * 2
    extension.close_callback = lambda: None
    state.slot.uninstall(state.context, state.owner)
    state.slot.uninstall(state.context, state.owner)
    assert extension.closed == [extension.candidate] * 3
    assert state.slot.registry_generation is restored
    assert state.slot.implementation is state.original
    monkeypatch.undo()
    replacement = ProbeExtension()
    state.slot.install(replacement, state.context, state.owner)
    state.slot.uninstall(state.context, state.owner)


@pytest.mark.parametrize("fail_close", [False, True])
def test_failed_activation_and_failed_restore_keep_candidate_owned_and_withdrawn(execution, monkeypatch, fail_close):
    state = execution
    extension = ProbeExtension()
    captured = []

    def fail_activation():
        captured.append(state.slot.registry_generation)
        raise RuntimeError("activation failed")

    def fail_restore(*args, **kwargs):
        raise RuntimeError("restore failed")

    extension.activate_callback = fail_activation
    with monkeypatch.context() as patch:
        patch.setattr(state.original, "mount_subtree", fail_restore)
        with pytest.raises(RuntimeError, match="activation failed"):
            state.slot.install(extension, state.context, state.owner)
        assert_withdrawn(invoke(extension.candidate, captured[0]))
        assert extension.closed == []
        with pytest.raises(RuntimeError, match="cleanup is pending"):
            state.slot.install(ProbeExtension(), state.context, state.owner)
        with pytest.raises(RuntimeError, match="restore failed"):
            state.slot.uninstall(state.context, state.owner)
    if fail_close:
        def fail_cleanup():
            raise RuntimeError("close failed")

        extension.close_callback = fail_cleanup
        with pytest.raises(RuntimeError, match="close failed"):
            state.slot.uninstall(state.context, state.owner)
        restored = state.slot.registry_generation
        extension.close_callback = lambda: None
    state.slot.uninstall(state.context, state.owner)
    assert_owner(invoke(state.slot), "original")
    assert_withdrawn(invoke(extension.candidate, captured[0]))
    assert extension.closed == [extension.candidate] * (2 if fail_close else 1)
    if fail_close:
        assert state.slot.registry_generation is restored
