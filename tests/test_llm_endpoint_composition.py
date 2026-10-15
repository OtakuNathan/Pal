from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from unittest.mock import patch

import pytest

from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.credentials import LLMCredentialResolver
from pal.llm.endpoint_auth import ChatGPTEndpointAuth, EndpointAuth
from pal.llm.endpoint_spec import LLMEndpointSpec, LLMEndpointSpecError, endpoint_spec_fingerprint
from pal.llm.ir import WireShape
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import _JSONFrame
from pal.llm.secret_store import InMemorySecretStore, SecretRef
from pal.llm.transport import DirectSDKTransport, EncodedTransportRequest, LLMEndpointSpecStaleError
from pal.llm.websocket_transport import _websocket_url
from tests.test_chatgpt_subscription import endpoint, request
from tests.test_chatgpt_websocket import _completed


class _Port:
    def __init__(self):
        self.requests = []
        self.closed = 0

    def frames(self, req):
        self.requests.append(req)
        yield _JSONFrame(0, {"type": "response.created"})
        yield _JSONFrame(1, _completed())

    def activate_endpoint(self, endpoint_id):
        pass

    def close(self):
        self.closed += 1


def _encoded():
    return EncodedTransportRequest("request", WireShape.OPENAI_RESPONSE, 30, {}, False)


@pytest.mark.parametrize("subscription", [False, True])
@pytest.mark.parametrize("transport_kind", ["http", "websocket"])
def test_authentication_and_transport_are_independently_composable(subscription, transport_kind):
    ep = endpoint()
    ep.transport = transport_kind
    if not subscription:
        ep.auth_kind, ep.capabilities_blob = "api_key_ref", {}
    http, ws = _Port(), _Port()
    direct = DirectSDKTransport(lambda ep: "credential", http, ws)
    try:
        bound = direct.bind(ep)
        assert bound is direct.bind(ep)
        assert type(bound.auth) is (ChatGPTEndpointAuth if subscription else EndpointAuth)
        assert bound.transport is (ws if transport_kind == "websocket" else http)
        assert bound.context.wire_shape == WireShape.OPENAI_RESPONSE
        with patch("pal.llm.shapes.openai_response.is_chatgpt", side_effect=AssertionError("profile was not bound")):
            response, _ = ShapeEndpointInvoker(transport=direct).invoke(ep, request())
        assert response.text == "OK"
        selected, unselected = (ws, http) if transport_kind == "websocket" else (http, ws)
        assert len(selected.requests) == 1 and not unselected.requests
        assert selected.requests[0].api_key == "credential"
    finally:
        direct.close()


def test_bound_endpoint_keeps_one_codec_and_renews_credentials_without_reassembly():
    http, ws = _Port(), _Port()
    credentials = ["first-token"]
    direct = DirectSDKTransport(lambda ep: credentials[0], http, ws)
    ep = endpoint()
    with patch("pal.llm.endpoint_components.codec_for_shape", wraps=codec_for_shape) as compile_codec:
        direct.prepare_endpoints((ep,))
        bound = direct.bind(ep)
        list(bound.frames(_encoded()))
        credentials[0] = "renewed-token"
        list(direct.frames(ep, _encoded()))
        assert direct.bind(ep) is bound
        assert compile_codec.call_count == 1
    assert [req.api_key for req in ws.requests] == ["first-token", "renewed-token"]
    with pytest.raises(FrozenInstanceError):
        bound.transport = http
    with pytest.raises(TypeError):
        bound.context.capabilities["access_profile"] = "changed"
    direct.close()


def test_new_credential_reference_does_not_reuse_the_previous_compositions_cached_key():
    store = InMemorySecretStore()
    store.set_secret(SecretRef("first"), "first-key")
    store.set_secret(SecretRef("second"), "second-key")
    ep = endpoint()
    ep.auth_kind, ep.capabilities_blob, ep.transport = "api_key_ref", {}, "http"
    ep.credential_ref = "first"
    port = _Port()
    direct = DirectSDKTransport(LLMCredentialResolver(store).resolve_api_key, port)
    list(direct.bind(ep).frames(_encoded()))
    ep.credential_ref = "second"
    list(direct.bind(ep).frames(_encoded()))
    assert [req.api_key for req in port.requests] == ["first-key", "second-key"]
    direct.close()


def test_configuration_replacement_retires_old_composition_after_its_inflight_request():
    http, ws = _Port(), _Port()
    direct = DirectSDKTransport(lambda ep: "credential", http, ws)
    ep = endpoint()
    old = direct.bind(ep)
    pending = old.frames(_encoded())
    next(pending)
    ep.transport = "http"
    new = direct.bind(ep)
    assert new is not old and new.transport is http
    assert old.spec.transport == "websocket"
    assert ws.closed == 0
    with pytest.raises(LLMEndpointSpecStaleError):
        list(old.frames(_encoded()))
    assert len(ws.requests) == 1
    assert list(pending)[-1].payload["type"] == "response.completed"
    assert ws.closed == 1
    list(new.frames(_encoded()))
    direct.close()


def test_close_waits_for_admitted_requests_even_before_socket_acquisition():
    ws = _Port()
    holder = {}

    def credential(ep):
        # Admission has happened, but no transport call has started yet.
        holder['direct'].close()
        assert ws.closed == 0
        return "credential"

    direct = DirectSDKTransport(credential, websocket_transport=ws)
    holder['direct'] = direct
    bound = direct.bind(endpoint())
    assert list(bound.frames(_encoded()))[-1].payload["type"] == "response.completed"
    assert ws.closed == 1
    with pytest.raises(LLMEndpointSpecStaleError):
        list(bound.frames(_encoded()))


def test_transport_participates_in_endpoint_snapshot_and_unsupported_combinations_fail_early():
    ep = LLMEndpointSpec.from_value(endpoint())
    http = replace(ep, transport="http")
    assert ep.to_payload()["transport"] == "websocket"
    assert endpoint_spec_fingerprint(ep) != endpoint_spec_fingerprint(http)
    with pytest.raises(LLMEndpointSpecError, match="requires openai_response"):
        LLMEndpointSpec.from_value(replace(ep, wire_shape="anthropic_messages"))
    with pytest.raises(LLMEndpointSpecError, match="invalid transport"):
        LLMEndpointSpec.from_value(replace(ep, transport="auto"))


@pytest.mark.parametrize("base,expected", [
    ("https://api.openai.com/v1", "wss://api.openai.com/v1/responses"),
    ("https://compatible.example/custom", "wss://compatible.example/custom/responses"),
    ("http://127.0.0.1:8000/v1/", "ws://127.0.0.1:8000/v1/responses"),
])
def test_websocket_destination_comes_from_endpoint_configuration(base, expected):
    assert _websocket_url(base) == expected
