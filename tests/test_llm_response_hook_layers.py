"""Protocol hooks adapt raw blocks before common JSON/tool validation."""
import asyncio
import os
from dataclasses import replace

import pytest

from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.endpoint_components import compile_endpoint_codec
from pal.llm.endpoint_spec import LLMEndpointSpec
from pal.llm.ir import LLMResponseDeltaKind, WireShape
from pal.llm.response_blocks import ResponseBlock
from pal.llm.response_hooks import (
    EndpointResponseHook, ProviderResponseHook, ProviderResponseHookError,
    ProviderResponseHookRegistry,
)
from pal.llm.shapes.base import ShapeDecodeError, _JSONFrame
from tests.test_llm_response_hooks import _endpoint, _request, _dsml_call


@pytest.fixture(autouse=True)
def private_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("PAL_LOG_ROOT", str(tmp_path / "logs"))


def rewrite(updates, transform):
    for update in updates:
        def block(value):
            if isinstance(value, ResponseBlock):
                return replace(value, payload={**dict(value.payload), "arguments": transform(value.arguments)})
            return value
        yield replace(update, response=replace(update.response, message=replace(
            update.response.message, parts=tuple(block(p) for p in update.response.message.parts),
        )), tool_block=block(update.tool_block))


def frames(shape, raw, streaming):
    if shape == WireShape.ANTHROPIC_MESSAGES:
        tool = {"type": "tool_use", "id": "native", "name": "read_file", "input": raw}
        if not streaming:
            return [{"content": [tool], "stop_reason": "tool_use"}]
        return [
            {"type": "content_block_start", "index": 0, "content_block": {**tool, "input": {}}},
            *({"type": "content_block_delta", "index": 0, "delta": {
                "type": "input_json_delta", "partial_json": char,
            }} for char in raw),
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        ]
    if shape == WireShape.OPENAI_COMPLETION:
        tool = {"id": "native", "type": "function", "function": {"name": "read_file", "arguments": raw}}
        if not streaming:
            return [{"choices": [{"message": {"tool_calls": [tool]}, "finish_reason": "tool_calls"}]}]
        return [
            {"choices": [{"delta": {"tool_calls": [{**tool, "index": 0, "function": {"name": "read_file", "arguments": ""}}]}}]},
            *({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": char}}]}}]} for char in raw),
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    tool = {"type": "function_call", "id": "item", "call_id": "native", "name": "read_file", "arguments": raw}
    if not streaming:
        return [{"output": [tool]}]
    return [
        {"type": "response.output_item.added", "output_index": 0, "item": {**tool, "arguments": ""}},
        *({"type": "response.function_call_arguments.delta", "output_index": 0, "delta": char} for char in raw),
        {"type": "response.output_item.done", "output_index": 0, "item": tool},
        {"type": "response.completed", "response": {"status": "completed"}},
    ]


class Transport:
    def __init__(self, payloads):
        self.payloads = payloads

    def frames(self, endpoint, request):
        for index, payload in enumerate(self.payloads):
            yield _JSONFrame(index, payload)


@pytest.mark.parametrize("shape", list(WireShape))
@pytest.mark.parametrize("streaming", [False, True])
def test_provider_then_endpoint_can_repair_non_json_before_shared_decode(shape, streaming):
    trace = []

    def provider(context, updates):
        assert context.provider_id == "vendor"
        def transform(value):
            assert value.startswith("provider:")
            trace.append("provider")
            return value.removeprefix("provider:")
        yield from rewrite(updates, transform)

    def endpoint(context, updates):
        assert context.endpoint_id == "vendor-endpoint"
        def transform(value):
            assert value.startswith("endpoint:")
            trace.append("endpoint")
            return value.removeprefix("endpoint:")
        yield from rewrite(updates, transform)

    registry = ProviderResponseHookRegistry(
        {"vendor": ProviderResponseHook("vendor", provider)},
        {"vendor-endpoint": EndpointResponseHook("vendor-endpoint", endpoint)},
    )
    ep = _endpoint(provider="vendor", wire_shape=shape.value)
    invoker = ShapeEndpointInvoker(
        transport=Transport(frames(shape, 'provider:endpoint:{"target":5}', streaming)), response_hooks=registry,
    )
    candidates = []
    response = (list(invoker.invoke_updates(ep, _request(), native_sink=candidates.append))[-1].response if streaming
                else invoker.invoke(ep, _request(), native_sink=candidates.append)[0])
    assert response.tool_calls[0].call_id == "native"
    assert response.tool_calls[0].args == {"target": 5}
    assert trace and trace[0] == "provider"
    assert trace.count("provider") == trace.count("endpoint")
    assert response.message.replay is not None
    assert response.message.replay.source_payload is not None
    assert len(candidates) == 1
    assert "provider:endpoint:" not in candidates[0].payload_json


def test_endpoint_hook_is_exact_and_provider_default_is_inherited_by_siblings():
    def provider(context, updates):
        yield from rewrite(updates, lambda raw: raw.removeprefix("provider:"))

    def first_only(context, updates):
        yield from rewrite(updates, lambda raw: raw.removeprefix("endpoint:"))

    registry = ProviderResponseHookRegistry(
        {"vendor": ProviderResponseHook("vendor", provider)},
        {"first": EndpointResponseHook("first", first_only)},
    )
    ep = _endpoint(provider="vendor", wire_shape="openai_completion")
    invoker = ShapeEndpointInvoker(
        transport=Transport(frames(WireShape.OPENAI_COMPLETION, 'provider:{"target":5}', False)),
        response_hooks=registry,
    )
    ep.endpoint_id = "sibling"
    assert invoker.invoke(ep, _request())[0].tool_calls[0].args == {"target": 5}
    invoker.transport = Transport(frames(WireShape.OPENAI_COMPLETION, 'provider:endpoint:{"target":5}', False))
    ep.endpoint_id = "first"
    assert invoker.invoke(ep, _request())[0].tool_calls[0].args == {"target": 5}
    ep.endpoint_id = "sibling"
    with pytest.raises(ShapeDecodeError, match="invalid JSON"):
        invoker.invoke(ep, _request())
    ep.provider = "other"
    with pytest.raises(ShapeDecodeError, match="invalid JSON"):
        invoker.invoke(ep, _request())


@pytest.mark.parametrize("shape", list(WireShape))
def test_endpoint_can_repair_tool_name_before_common_validation_and_native_replay(shape):
    from pal.llm.replay_acceptance import validate_native_for_send

    def fix_names(value):
        if isinstance(value, dict):
            return {key: ("" if key == "name" else fix_names(item)) for key, item in value.items()}
        if isinstance(value, list):
            return [fix_names(item) for item in value]
        return value

    def endpoint(context, updates):
        for update in updates:
            def fix(block):
                return replace(block, payload={**dict(block.payload), "name": "read_file"}) if isinstance(block, ResponseBlock) else block
            yield replace(update, tool_block=fix(update.tool_block), response=replace(
                update.response, message=replace(update.response.message,
                                                  parts=tuple(fix(p) for p in update.response.message.parts)),
            ))

    ep = _endpoint(provider="vendor", wire_shape=shape.value)
    registry = ProviderResponseHookRegistry({}, {ep.endpoint_id: EndpointResponseHook(ep.endpoint_id, endpoint)})
    invoker = ShapeEndpointInvoker(transport=Transport(fix_names(frames(shape, '{}', True))), response_hooks=registry)
    response = list(invoker.invoke_updates(ep, _request()))[-1].response
    assert response.tool_calls[0].name == "read_file"
    assert response.message.replay.source_payload is not None
    validate_native_for_send(response.message)


@pytest.mark.parametrize("shape", list(WireShape))
def test_deepseek_native_dsml_is_translated_before_json_validation(shape):
    raw = '<｜DSML｜parameter name="target" string="false">5</｜DSML｜parameter>'
    invoker = ShapeEndpointInvoker(transport=Transport(frames(shape, raw, True)))
    response = list(invoker.invoke_updates(_endpoint(wire_shape=shape.value), _request()))[-1].response
    assert response.tool_calls[0].args == {"target": 5}
    assert response.tool_calls[0].call_id == "native"
    assert response.message.replay.source_payload is not None
    with pytest.raises(ShapeDecodeError, match="invalid JSON"):
        invoker.invoke(_endpoint(provider="zhipu", wire_shape=shape.value), _request())


def test_endpoint_hook_observes_deepseek_body_recovered_as_standard_tool_block():
    from pal.llm.replay_acceptance import validate_native_for_send
    observed = []

    def endpoint(context, updates):
        for update in updates:
            if update.tool_block is not None:
                observed.append(update.tool_block.arguments)
            yield update

    builtin = ProviderResponseHookRegistry.builtin()
    registry = ProviderResponseHookRegistry(builtin.hooks, {
        "deepseek-endpoint": EndpointResponseHook("deepseek-endpoint", endpoint),
    })
    invoker = ShapeEndpointInvoker(transport=Transport([
        {"content": [{"type": "text", "text": _dsml_call()}], "stop_reason": "stop"},
    ]), response_hooks=registry)
    response, _ = invoker.invoke(_endpoint(), _request())
    assert response.tool_calls[0].name == "search_tools"
    assert observed and dict(observed[0]) == {"namespace": "introspection", "top_k": 3}
    validate_native_for_send(response.message)


@pytest.mark.parametrize("shape", list(WireShape))
def test_adapted_tool_inventory_replays_through_common_binding_with_opaque_reasoning(shape):
    from pal.llm.replay_acceptance import validate_native_for_send

    if shape == WireShape.ANTHROPIC_MESSAGES:
        payloads = [{"content": [
            {"type": "thinking", "thinking": "reason", "signature": "opaque-signature"},
            {"type": "text", "text": _dsml_call()},
        ], "stop_reason": "stop"}]
    elif shape == WireShape.OPENAI_RESPONSE:
        payloads = [{"output": [
            {"type": "reasoning", "id": "r", "encrypted_content": "opaque-signature", "summary": []},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": _dsml_call()}]},
        ]}]
    else:
        payloads = [{"choices": [{"message": {
            "role": "assistant", "reasoning_content": "reason", "reasoning_details": [{"opaque": "opaque-signature"}],
            "content": _dsml_call(),
        }, "finish_reason": "stop"}]}]
    response, _ = ShapeEndpointInvoker(transport=Transport(payloads)).invoke(
        _endpoint(wire_shape=shape.value), _request(),
    )
    validate_native_for_send(response.message)
    assert "opaque-signature" in str(response.message.replay.payload)
    assert response.message.replay.source_payload is not None
    assert response.tool_calls[0].name == "search_tools"


def test_hook_files_load_both_layers_and_reject_duplicate_or_invalid_exports(tmp_path):
    providers, endpoints = tmp_path / "llm/providers", tmp_path / "llm/endpoints"
    providers.mkdir(parents=True)
    endpoints.mkdir()
    (providers / "vendor.py").write_text('PROVIDER_ID="Vendor"\ndef normalize_blocks(context, updates):\n    yield from updates\n')
    (endpoints / "one.py").write_text('ENDPOINT_ID="one"\ndef normalize_blocks(context, updates):\n    yield from updates\n')
    registry = ProviderResponseHookRegistry.load(tmp_path)
    ep = _endpoint(provider="vendor")
    ep.endpoint_id = "one"
    bound = compile_endpoint_codec(LLMEndpointSpec.from_value(ep), registry)
    assert bound.response_hooks.provider is registry.hooks["vendor"].normalize_updates
    assert bound.response_hooks.endpoint is registry.endpoint_hooks["one"].normalize_updates
    assert "deepseek" in registry.hooks
    (providers / "duplicate.py").write_text('PROVIDER_ID="vendor"\ndef normalize_blocks(context, updates):\n    yield from updates\n')
    with pytest.raises(ProviderResponseHookError, match="duplicate"):
        ProviderResponseHookRegistry.load(tmp_path)
    (providers / "duplicate.py").unlink()
    (endpoints / "one.py").write_text('ENDPOINT_ID="one"\n')
    with pytest.raises(ProviderResponseHookError, match="normalize_blocks"):
        ProviderResponseHookRegistry.load(tmp_path)


def test_rebinding_does_not_change_inflight_request_hook_generation():
    def old(context, updates):
        yield from rewrite(updates, lambda _: '{"generation":1}')

    def new(context, updates):
        yield from rewrite(updates, lambda _: '{"generation":2}')

    ep = _endpoint(provider="vendor")
    # SDK-shaped injection exercises the real immutable transport composition.
    class SDKTransport:
        def frames(self, request):
            yield _JSONFrame(0, {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
            yield _JSONFrame(1, {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "begin"}})
            yield _JSONFrame(2, {"type": "content_block_stop", "index": 0})
            for index, payload in enumerate(frames(WireShape.ANTHROPIC_MESSAGES, '{}', True), 3):
                yield _JSONFrame(index, {**payload, "index": 1} if "index" in payload else payload)

        def activate_endpoint(self, endpoint_id):
            pass

        def close(self):
            pass

    invoker = ShapeEndpointInvoker(credential_resolver=lambda _: "secret", transport=SDKTransport(),
                                  response_hooks=ProviderResponseHookRegistry({"vendor": ProviderResponseHook("vendor", old)}))
    pending = invoker.invoke_updates(ep, _request())
    assert next(pending).delta_kind == LLMResponseDeltaKind.TEXT
    previous = invoker.transport.bind(ep)
    invoker.configure_response_hooks(ProviderResponseHookRegistry({"vendor": ProviderResponseHook("vendor", new)}))
    current = invoker.transport.bind(ep)
    assert current is not previous
    assert previous.response_hooks.provider is old
    assert current.response_hooks.provider is new
    assert list(pending)[-1].response.tool_calls[0].args == {"generation": 1}
    assert list(invoker.invoke_updates(ep, _request()))[-1].response.tool_calls[0].args == {"generation": 2}


def test_runtime_refresh_loads_edits_and_removals_and_rejects_invalid_reload(tmp_path):
    from pal.core.runtime_config import RuntimeConfig
    from pal.llm.runtime import EndpointResolver, LLMRuntime
    from tests.test_llm_runtime_ir import _Settings

    directory = tmp_path / "llm/endpoints"
    directory.mkdir(parents=True)
    path = directory / "vendor.py"
    def source(generation):
        return ('from dataclasses import replace\n'
                'from pal.llm.response_blocks import ResponseBlock\n'
                'ENDPOINT_ID="vendor-endpoint"\n'
                'def normalize_blocks(context, updates):\n'
                '    for u in updates:\n'
                '        def rewrite(p):\n'
                '            return replace(p, payload={**dict(p.payload), "arguments": {"generation": '
                + str(generation) + '}}) if isinstance(p, ResponseBlock) else p\n'
                '        yield replace(u, tool_block=rewrite(u.tool_block), response=replace(u.response, message=replace(\n'
                '            u.response.message, parts=tuple(rewrite(p) for p in u.response.message.parts))))\n')
    path.write_text(source(1))
    stamp = path.stat()
    ep = _endpoint(provider="vendor", wire_shape="openai_completion")
    runtime = LLMRuntime(
        EndpointResolver(endpoints=(ep,)), _Settings(active=ep.endpoint_id),
        endpoint_invoker=ShapeEndpointInvoker(transport=Transport(frames(WireShape.OPENAI_COMPLETION, '{}', False))),
        config=RuntimeConfig(runtime_root=tmp_path),
    )
    assert runtime.generate(_request()).tool_calls[0].args == {"generation": 1}
    path.write_text(source(2))
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert runtime.refresh_llm_endpoints()["endpoint_response_hook_count"] == 1
    assert runtime.generate(_request()).tool_calls[0].args == {"generation": 2}
    path.unlink()
    assert runtime.refresh_llm_endpoints()["endpoint_response_hook_count"] == 0
    assert runtime.generate(_request()).tool_calls[0].args == {}
    path.write_text('raise RuntimeError("broken configuration")\n')
    previous = runtime.provider_response_hooks
    with pytest.raises(ProviderResponseHookError, match="import failed"):
        runtime.refresh_llm_endpoints()
    assert runtime.provider_response_hooks is previous
    assert runtime.generate(_request()).tool_calls[0].args == {}


def test_output_continuation_does_not_apply_endpoint_hook_again_to_merged_result(tmp_path):
    from pal.core.runtime_config import RuntimeConfig
    from pal.llm.runtime import EndpointResolver, LLMRuntime
    from tests.test_llm_runtime_ir import _Settings

    admissions = []
    def endpoint(context, updates):
        admissions.append(context.endpoint_id)
        yield from updates

    class ContinuingTransport:
        count = 0
        def frames(self, endpoint, request):
            self.count += 1
            yield _JSONFrame(0, {"choices": [{"message": {"content": "first " if self.count == 1 else "second"},
                                              "finish_reason": "length" if self.count == 1 else "stop"}]})

    ep = _endpoint(provider="vendor", wire_shape="openai_completion")
    ep.context_window = 100_000
    ep.max_output_tokens = 100
    ep.capabilities_blob = {"max_output_recovery": {"enabled": True, "upper_limit": 1000, "max_continuations": 1}}
    invoker = ShapeEndpointInvoker(transport=ContinuingTransport(), response_hooks=ProviderResponseHookRegistry(
        {}, {ep.endpoint_id: EndpointResponseHook(ep.endpoint_id, endpoint)},
    ))
    runtime = LLMRuntime(EndpointResolver(endpoints=(ep,)), _Settings(active=ep.endpoint_id),
                         endpoint_invoker=invoker, config=RuntimeConfig(runtime_root=tmp_path))
    async def consume():
        return [update async for update in runtime.astream(_request())]
    result = asyncio.run(consume())[-1].response
    assert result.text == "first second"
    assert admissions == [ep.endpoint_id, ep.endpoint_id]


def test_sandboxed_worker_loads_both_hook_layers_from_read_only_configuration(tmp_path, monkeypatch):
    import json
    import shutil
    import subprocess
    import sys
    from pal.bunshin.sandbox import build_sandboxed_runner_invocation
    from pal.shared import BunshinInvocationPack

    if not shutil.which("bwrap"):
        pytest.skip("bubblewrap is not available")
    repo = tmp_path / "repo"
    repo.mkdir()
    role_endpoint = tmp_path / "data/bunshin-role/role.sock"
    role_endpoint.parent.mkdir(parents=True)
    role_endpoint.write_text("test endpoint")
    for directory, source in {
        "models": 'MODEL_ID="sandbox-model"\n',
        "providers": 'PROVIDER_ID="vendor"\ndef normalize_blocks(context, updates):\n    yield from updates\n',
        "endpoints": 'ENDPOINT_ID="one"\ndef normalize_blocks(context, updates):\n    yield from updates\n',
    }.items():
        path = tmp_path / "llm" / directory
        path.mkdir(parents=True)
        (path / "hook.py").write_text(source)
    script = (
        'import json\nfrom pathlib import Path\n'
        'from pal.llm.response_hooks import ProviderResponseHookRegistry\n'
        'from pal.llm.model_hooks import ModelHookRegistry\n'
        f'root=Path({str(tmp_path)!r})\n'
        'registry=ProviderResponseHookRegistry.load(root)\n'
        'read_only=False\n'
        'try:\n    (root / "llm/endpoints/hook.py").write_text("changed")\n'
        'except OSError:\n    read_only=True\n'
        'print(json.dumps({"providers":sorted(registry.hooks), "endpoints":sorted(registry.endpoint_hooks), '
        '"models":sorted(ModelHookRegistry.load(root).hooks), "read_only":read_only}))\n'
    )
    pack = BunshinInvocationPack(invocation_id="hook-config", goal="g", workspace={"repo_path": str(repo)},
                                 metadata={"sandbox": {"enabled": True, "backend": "bwrap", "run_id": "hook-config"}})
    monkeypatch.setenv("PAL_BUNSHIN_SANDBOX_SCRATCH_ROOT", str(tmp_path / "scratch"))
    argv, env = build_sandboxed_runner_invocation(
        runtime_root=tmp_path, pack=pack, argv=[sys.executable, "-c", script], env={"PATH": "/usr/bin:/bin"},
    )
    result = subprocess.run(argv, env=env, cwd=repo, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "providers": ["deepseek", "vendor"], "endpoints": ["one"], "models": ["sandbox-model"], "read_only": True,
    }


def test_proxy_hook_refresh_cannot_republish_an_old_binding_after_configuration_changes(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from pal.llm import endpoint as endpoint_module

    compiling, refreshing, refreshed = Event(), Event(), Event()
    def old(context, updates):
        yield from rewrite(updates, lambda _: '{"generation":1}')
    def new(context, updates):
        yield from rewrite(updates, lambda _: '{"generation":2}')
    original = endpoint_module.compile_endpoint_codec
    first = True
    def pause_compile(spec, registry):
        nonlocal first
        if first:
            first = False
            compiling.set()
            assert refreshing.wait(5)
            # Without serialized admission, refresh completes here and the old
            # compilation would subsequently overwrite the newly cleared cache.
            refreshed.wait(0.2)
        return original(spec, registry)

    ep = _endpoint(provider="vendor", wire_shape="openai_completion")
    invoker = ShapeEndpointInvoker(transport=Transport(frames(WireShape.OPENAI_COMPLETION, '{}', False)),
                                  response_hooks=ProviderResponseHookRegistry({"vendor": ProviderResponseHook("vendor", old)}))
    monkeypatch.setattr(endpoint_module, "compile_endpoint_codec", pause_compile)
    def refresh():
        refreshing.set()
        invoker.configure_response_hooks(ProviderResponseHookRegistry({"vendor": ProviderResponseHook("vendor", new)}))
        refreshed.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(invoker.invoke, ep, _request())
        assert compiling.wait(5)
        replacement = pool.submit(refresh)
        assert pending.result(timeout=10)[0].tool_calls[0].args == {"generation": 1}
        replacement.result(timeout=10)
    assert invoker.invoke(ep, _request())[0].tool_calls[0].args == {"generation": 2}
