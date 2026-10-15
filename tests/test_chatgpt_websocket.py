from __future__ import annotations

import json
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import replace

import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.http11 import Response
from websockets.datastructures import Headers
from websockets.protocol import State
from websockets.sync.client import connect
from websockets.sync.server import serve

from pal.llm.chatgpt import API_URL, QUOTA_CODE, ChatGPTError
from pal.llm.websocket_transport import ResponsesWebSocketTransport
from pal.llm.credentials import LLMCredentialResolver
from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.ir import WireShape
from pal.llm.secret_store import InMemorySecretStore
from pal.llm.shapes.base import _JSONFrame
from pal.llm.transport import (
    DirectSDKTransport, EncodedTransportRequest, LLMStreamCancelledError,
    LLMProviderStartedError, LLMStreamControl, SDKTransportRequest,
)
from pal.llm.endpoint_spec import LLMEndpointSpecError
from tests.test_chatgpt_subscription import REF, endpoint, request, seed


def _completed(cached=0):
    return {"type": "response.completed", "response": {
        "id": "response-test", "model": "account-model", "status": "completed",
        "output": [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "OK"},
        ]}],
        "usage": {"input_tokens": 3000, "input_tokens_details": {
            "cached_tokens": cached, "cache_write_tokens": 0,
        }, "output_tokens": 1},
    }}


def _request(**changes):
    return replace(SDKTransportRequest(
        request_id="test", endpoint_id="chatgpt", wire_shape=WireShape.OPENAI_RESPONSE,
        api_key="token", base_url=API_URL, timeout_seconds=30,
        payload={"model": "account-model", "store": False, "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
        ]}, stream=True,
    ), **changes)


class _Socket:
    def __init__(self, responses=(), *, send_error=None, close_error=None):
        self.responses = deque(responses)
        self.pending = deque()
        self.sent = []
        self.state = State.OPEN
        self.send_error = send_error
        self.close_error = close_error
        self.close_threads = []

    def send(self, message):
        self.sent.append(json.loads(message))
        if self.send_error:
            raise self.send_error
        self.pending.extend(self.responses.popleft())

    def recv(self, timeout):
        item = self.pending.popleft()
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item)

    def close(self):
        self.close_threads.append(threading.get_ident())
        if self.close_error:
            raise self.close_error
        self.state = State.CLOSED


class _Connector:
    def __init__(self, *sockets):
        self.sockets = deque(sockets)
        self.requests = []

    def __call__(self, req):
        self.requests.append(req)
        return self.sockets.popleft()


def test_api_and_subscription_have_separate_transport_and_shared_request_contract():
    class Port:
        def __init__(self):
            self.requests, self.activated, self.closed = [], [], 0

        def frames(self, req):
            self.requests.append(req)
            yield _JSONFrame(0, _completed())

        def activate_endpoint(self, endpoint_id):
            self.activated.append(endpoint_id)

        def close(self):
            self.closed += 1

    api, subscription = Port(), Port()
    transport = DirectSDKTransport(lambda ep: "credential", api, subscription)
    encoded = EncodedTransportRequest("test", WireShape.OPENAI_RESPONSE, 30, {}, False)
    ep = endpoint()
    list(transport.frames(ep, encoded))
    assert not api.requests
    assert subscription.requests[0].api_key == "credential"
    assert subscription.requests[0].stream
    ep.capabilities_blob, ep.auth_kind = {}, "api_key_ref"
    ep.endpoint_id, ep.transport = "api", "http"
    list(transport.frames(ep, encoded))
    assert len(api.requests) == len(subscription.requests) == 1
    assert not api.requests[0].stream
    transport.activate_endpoint("other")
    transport.close()
    assert api.activated == subscription.activated == ["other"]
    assert api.closed == subscription.closed == 1


def test_reuses_socket_and_keeps_full_input_keys_and_submission_receipts():
    socket = _Socket([[_completed()], [_completed(2800)]])
    connector = _Connector(socket)
    transport = ResponsesWebSocketTransport(connector)
    submitted = []
    req = _request(extra_body={"prompt_cache_key": "stable"},
                   on_submitted=lambda: submitted.append(True))
    try:
        list(transport.frames(req))
        frames = list(transport.frames(req))
        assert len(connector.requests) == 1
        assert socket.sent[0] == socket.sent[1]
        assert socket.sent[0]["input"] == req.payload["input"]
        assert socket.sent[0]["prompt_cache_key"] == "stable"
        assert socket.sent[0]["type"] == "response.create"
        assert socket.sent[0]["store"] is False
        assert "previous_response_id" not in socket.sent[0]
        assert "stream" not in socket.sent[0]
        assert frames[-1].payload["response"]["usage"]["input_tokens_details"]["cached_tokens"] == 2800
        assert submitted == [True, True]
        assert not socket.close_threads
    finally:
        transport.close()


@pytest.mark.parametrize("event", ["response.failed", "response.incomplete", "error"])
def test_failed_terminal_retires_socket(event):
    failed = _Socket([[{"type": event, "response": {"error": {"code": "failed"}}}]])
    successful = _Socket([[_completed()]])
    transport = ResponsesWebSocketTransport(_Connector(failed, successful))
    try:
        assert list(transport.frames(_request()))[-1].payload["type"] == event
        assert failed.close_threads
        assert list(transport.frames(_request()))[-1].payload["type"] == "response.completed"
    finally:
        transport.close()


def test_abandoned_response_closes_socket_before_next_request():
    first = _Socket([[{"type": "response.created"}, _completed()]])
    second = _Socket([[_completed()]])
    connector = _Connector(first, second)
    transport = ResponsesWebSocketTransport(connector)
    try:
        frames = transport.frames(_request())
        next(frames)
        frames.close()
        assert first.close_threads
        list(transport.frames(_request()))
        assert len(connector.requests) == 2
    finally:
        transport.close()


def test_concurrent_requests_get_exclusive_sockets_and_cannot_mix_frames():
    first = _Socket([[{"type": "response.created", "response": {"id": "first"}}, _completed()]])
    second = _Socket([[_completed(1234)]])
    connector = _Connector(first, second)
    transport = ResponsesWebSocketTransport(connector)
    try:
        pending = transport.frames(_request())
        assert next(pending).payload["response"]["id"] == "first"
        other = list(transport.frames(_request(request_id="second")))
        assert other[-1].payload["response"]["usage"]["input_tokens_details"]["cached_tokens"] == 1234
        assert list(pending)[-1].payload["response"]["usage"]["input_tokens_details"]["cached_tokens"] == 0
        assert len(connector.requests) == 2
        assert len(first.sent) == len(second.sent) == 1
        assert not first.close_threads
        assert second.close_threads
    finally:
        transport.close()


@pytest.mark.parametrize("action", ["activate", "close", "rotate"])
def test_retirement_waits_for_active_request(action):
    first = _Socket([[{"type": "response.created"}, _completed()]])
    second = _Socket([[_completed()]])
    transport = ResponsesWebSocketTransport(_Connector(first, second))
    try:
        pending = transport.frames(_request())
        next(pending)
        if action == "rotate":
            list(transport.frames(_request(api_key="new-token")))
        elif action == "activate":
            transport.activate_endpoint("other")
        else:
            transport.close()
        assert not first.close_threads
        list(pending)
        assert first.close_threads == [threading.get_ident()]
    finally:
        transport.close()


@pytest.mark.parametrize("reason", ["expired", "server_closed", "credential", "model"])
def test_idle_socket_reconnects_before_send_when_its_binding_is_no_longer_usable(reason):
    first, second = _Socket([[_completed()]]), _Socket([[_completed()]])
    now = [0.0]
    connector = _Connector(first, second)
    transport = ResponsesWebSocketTransport(connector, clock=lambda: now[0])
    req = _request()
    try:
        list(transport.frames(req))
        if reason == "expired":
            now[0] = 55 * 60
        elif reason == "server_closed":
            first.state = State.CLOSED
        elif reason == "credential":
            req = replace(req, api_key="new-token")
        else:
            req = replace(req, payload={**req.payload, "model": "another-model"})
        list(transport.frames(req))
        assert len(connector.requests) == 2
        assert len(first.sent) == len(second.sent) == 1
        assert first.close_threads
    finally:
        transport.close()


def test_cancelled_receive_returns_promptly_and_closes_on_worker_thread():
    receiving = threading.Event()

    class WaitingSocket(_Socket):
        def recv(self, timeout):
            receiving.set()
            time.sleep(min(timeout, 0.01))
            raise TimeoutError()

    socket = WaitingSocket([[]])
    transport = ResponsesWebSocketTransport(_Connector(socket))
    control = LLMStreamControl()
    errors = []

    def consume():
        try:
            list(transport.frames(_request(stream_control=control)))
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=consume)
    try:
        worker.start()
        assert receiving.wait(2)
        control.cancel("test interrupt")
        worker.join(2)
        assert not worker.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], LLMStreamCancelledError)
        assert socket.close_threads == [worker.ident]
    finally:
        control.cancel()
        worker.join(2)
        transport.close()


def test_cancelled_request_never_connects_or_submits():
    connector = _Connector()
    transport = ResponsesWebSocketTransport(connector)
    control = LLMStreamControl()
    control.cancel("already interrupted")
    with pytest.raises(LLMStreamCancelledError):
        list(transport.frames(_request(stream_control=control)))
    assert not connector.requests and not transport._connections


def test_idle_response_timeout_retires_socket_without_replaying():
    class WaitingSocket(_Socket):
        def recv(self, timeout):
            time.sleep(min(timeout, 0.002))
            raise TimeoutError()

    socket = WaitingSocket([[]])
    transport = ResponsesWebSocketTransport(_Connector(socket))
    with pytest.raises(LLMProviderStartedError):
        list(transport.frames(_request(timeout_seconds=0.02)))
    assert len(socket.sent) == len(socket.close_threads) == 1
    assert not transport._connections


@pytest.mark.parametrize("payload", [[], {}, {"type": None}])
def test_malformed_event_retires_socket(payload):
    socket = _Socket([[payload]])
    transport = ResponsesWebSocketTransport(_Connector(socket))
    with pytest.raises(LLMProviderStartedError):
        list(transport.frames(_request()))
    assert socket.close_threads and not transport._connections


@pytest.mark.parametrize("phase", ["send", "receive"])
@pytest.mark.parametrize("streaming", [False, True])
def test_ambiguous_failure_is_not_automatically_replayed_by_runtime(phase, streaming):
    from tests.test_llm_fallback_switch import _runtime, _SwitchSettingsRepository

    socket = _Socket([[OSError("connection lost")]],
                     send_error=OSError("write failed") if phase == "send" else None)
    connector = _Connector(socket)
    ws = ResponsesWebSocketTransport(connector)
    runtime = _runtime(_SwitchSettingsRepository(True), active="chatgpt")
    runtime.endpoint_resolver.endpoints = (endpoint(),)
    runtime.endpoint_invoker = ShapeEndpointInvoker(transport=DirectSDKTransport(
        lambda ep: "token", websocket_transport=ws))
    try:
        if streaming:
            result = list(runtime._iter_stream_updates(request()))[-1].response
        else:
            result = runtime.generate(request()).response
        assert result.message.metadata["chatgpt_failure"]["code"] == "request_outcome_unknown"
        assert len(socket.sent) == len(connector.requests) == 1
    finally:
        ws.close()


def test_pre_send_connect_failure_remains_retryable_and_unsubmitted():
    def failing_connector(req):
        raise OSError("connection refused")

    control = LLMStreamControl()
    submitted = []
    ws = ResponsesWebSocketTransport(failing_connector)
    req = EncodedTransportRequest("test", WireShape.OPENAI_RESPONSE, 30, {}, True,
        stream_control=control, on_submitted=lambda: submitted.append(True))
    transport = DirectSDKTransport(lambda ep: "token", websocket_transport=ws)
    with pytest.raises(ChatGPTError) as failure:
        list(transport.frames(endpoint(), req))
    assert failure.value.code == "network_error" and failure.value.retryable
    assert not submitted and not control.provider_started
    assert not ws._connections


@pytest.mark.parametrize("url,shape", [
    ("https://example.test/v1", WireShape.OPENAI_RESPONSE),
    (API_URL, WireShape.OPENAI_COMPLETION),
])
def test_subscription_transport_rejects_invalid_destination_before_credentials_leave(url, shape):
    connector = _Connector()
    ws = ResponsesWebSocketTransport(connector)
    ep = endpoint()
    ep.base_url, ep.wire_shape = url, shape.value
    with pytest.raises(LLMEndpointSpecError):
        list(DirectSDKTransport(lambda ep: "token", websocket_transport=ws).frames(
            ep, EncodedTransportRequest("test", shape, 30, {}, True)))
    assert not connector.requests


def test_handshake_quota_preserves_structured_failure_and_pauses_credentials(monkeypatch):
    store = InMemorySecretStore()
    seed(store)

    def reject(*args, **kwargs):
        raise InvalidStatus(Response(429, "Too Many Requests", Headers({"x-request-id": "test-request"}),
            json.dumps({"error": {"code": QUOTA_CODE, "param": "model"}}).encode()))

    monkeypatch.setattr("pal.llm.websocket_transport.connect", reject)
    transport = DirectSDKTransport(LLMCredentialResolver(store).resolve_api_key)
    invoker = ShapeEndpointInvoker(transport=transport)
    with pytest.raises(ChatGPTError) as failure:
        invoker.invoke(endpoint(), request())
    assert failure.value.code == QUOTA_CODE
    assert failure.value.status == 429 and failure.value.request_id == "test-request"
    assert failure.value.param == "model"
    assert json.loads(store.get_secret(REF))["usage_paused"]


def test_credential_refresh_retires_both_transport_pools():
    class Resolver:
        refreshed = 0

        def resolve(self, ep):
            return "token"

        def refresh(self):
            self.refreshed += 1

    class API:
        closed = 0

        def close(self):
            self.closed += 1

    socket = _Socket([[_completed()]])
    ws = ResponsesWebSocketTransport(_Connector(socket))
    owner, api = Resolver(), API()
    transport = DirectSDKTransport(owner.resolve, api, ws)
    list(transport.frames(endpoint(), EncodedTransportRequest(
        "test", WireShape.OPENAI_RESPONSE, 30, {"model": "account-model"}, True)))
    assert transport.refresh_credentials()
    assert owner.refreshed == api.closed == len(socket.close_threads) == 1


def test_uncertain_close_quarantines_socket_and_never_reuses_it():
    socket = _Socket([[_completed()]], close_error=OSError("close failed"))
    transport = ResponsesWebSocketTransport(_Connector(socket))
    list(transport.frames(_request()))
    entry = next(iter(transport._connections.values()))[0]
    transport.close()
    assert entry.owner.quarantined
    assert entry.owner._resource.connection is socket
    assert not transport._connections
    assert len(socket.close_threads) == 1


@contextmanager
def _server(handler):
    with serve(handler, "127.0.0.1", 0) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"ws://127.0.0.1:{server.socket.getsockname()[1]}"
        finally:
            server.shutdown()
            worker.join(3)


def test_real_websocket_reuse_through_codec_preserves_usage_and_tool_namespace():
    connections, messages = [], []

    def handler(socket):
        connections.append(socket.request.headers["Authorization"])
        try:
            for raw in socket:
                messages.append(json.loads(raw))
                socket.send(json.dumps({"type": "response.created", "response": {"id": "test"}}))
                socket.send(json.dumps(_completed(2800 if len(messages) > 1 else 0)))
        except ConnectionClosed:
            pass

    with _server(handler) as url:
        ws = ResponsesWebSocketTransport(lambda req: connect(url,
            additional_headers={"Authorization": "Bearer " + req.api_key}, close_timeout=1))
        invoker = ShapeEndpointInvoker(transport=DirectSDKTransport(
            lambda ep: "test-token", websocket_transport=ws))
        try:
            one, _ = invoker.invoke(endpoint(), request(), stream=False)
            two, _ = invoker.invoke(endpoint(), request(), stream=False)
            assert one.message.text == two.message.text == "OK"
            assert one.usage.cached_input_tokens == 0
            assert two.usage.cached_input_tokens == 2800
            assert connections == ["Bearer test-token"]
            assert len(messages) == 2
            assert messages[0] == messages[1]
            assert messages[0]["tools"][0]["type"] == "namespace"
            assert messages[0]["tools"][0]["name"] == "pal"
            assert messages[0]["input"][0]["role"] == "developer"
        finally:
            ws.close()
