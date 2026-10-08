from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from pal.llm.chatgpt import (
    API_URL, ISSUER, PROFILE, QUOTA_CODE, SCOPES, ChatGPTAuthService, ChatGPTError,
)
from pal.llm.credentials import LLMCredentialResolver
from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.endpoint_spec import LLMEndpointSpec, LLMEndpointSpecError
from pal.llm.ir import (
    GenerationPolicyIR, LLMMessageIR, LLMRequestIR, MessageRole, TextPartIR, WireShape,
)
from pal.llm.secret_store import EncryptedFileSecretStore, InMemorySecretStore, SecretRef
from pal.llm.shapes.base import ShapeContext, _JSONFrame
from pal.llm.shapes.openai_response import OpenAIResponseCodec
from pal.llm.transport import DirectSDKTransport, EncodedTransportRequest
from pal.shared.tool_protocol import ToolCallIR, ToolDefinitionIR

REF = SecretRef("pal.chatgpt", "oaiapp_test")


def seed(store, *, expiry=None):
    record = dict(auth_provider=PROFILE, client_id=REF.account, issuer=ISSUER, subject="user-1",
                  access_token="access-old", refresh_token="refresh-old", id_token="id-old",
                  expires_at=expiry or time.time() + 3600, scopes=SCOPES.split(), usage_paused=False)
    store.set_secret(REF, json.dumps(record))
    return record


def endpoint():
    return SimpleNamespace(
        endpoint_id="chatgpt", model_id="account-model", provider="openai", display_name="ChatGPT",
        base_url=API_URL, wire_shape="openai_response", auth_kind="oauth",
        credential_ref="pal.chatgpt:oaiapp_test", capabilities_blob={"access_profile": PROFILE},
        thinking_levels_blob=["off"], default_thinking_level="off", supports_tools=True,
        supports_streaming=True, supports_vision=False, max_output_tokens=4096, context_window=32768,
        input_modalities_blob=["text"], output_modalities_blob=["text"], priority=0, enabled=True, notes=None,
    )


def request():
    return LLMRequestIR(
        messages=(LLMMessageIR(MessageRole.SYSTEM, (TextPartIR("System instructions"),)),
                  LLMMessageIR(MessageRole.USER, (TextPartIR("hello"),))),
        tools=(ToolDefinitionIR("probe", "probe", {"type": "object", "properties": {}}),),
        policy=GenerationPolicyIR(max_output_tokens=100, temperature=0.4),
    )


def context():
    return ShapeContext(WireShape.OPENAI_RESPONSE, "chatgpt", "account-model", capabilities={"access_profile": PROFILE})


def test_atomic_store_preserves_other_writers_and_permissions(tmp_path):
    path = tmp_path / "secrets.json"
    one, two = EncryptedFileSecretStore(path), EncryptedFileSecretStore(path)
    one.set_secret(SecretRef("one"), "first")
    two.set_secret(SecretRef("two"), "second")
    assert one.get_secret(SecretRef("two")) == "second"
    assert two.get_secret(SecretRef("one")) == "first"
    assert path.stat().st_mode & 0o777 == 0o600
    assert "first" not in path.read_text()


def test_corrupt_store_is_not_overwritten(tmp_path):
    path = tmp_path / "secrets.json"
    path.write_text("broken")
    with pytest.raises(ValueError):
        EncryptedFileSecretStore(path)
    assert path.read_text() == "broken"


def test_concurrent_refresh_rotates_once_across_store_instances(tmp_path):
    path = tmp_path / "secrets.json"
    seed(EncryptedFileSecretStore(path), expiry=time.time() - 1)
    calls = []

    def respond(req):
        calls.append(parse_qs(req.content.decode()))
        return httpx.Response(200, json={"access_token": "access-new", "refresh_token": "refresh-new", "expires_in": 3600})

    services = [ChatGPTAuthService(EncryptedFileSecretStore(path), client=httpx.Client(transport=httpx.MockTransport(respond))) for _ in range(4)]
    with ThreadPoolExecutor(4) as pool:
        values = list(pool.map(lambda service: service.access_token(REF), services))
    assert values == ["access-new"] * 4
    assert len(calls) == 1
    assert calls[0]["client_id"] == [REF.account]
    assert "scope" not in calls[0]
    assert json.loads(EncryptedFileSecretStore(path).get_secret(REF))["refresh_token"] == "refresh-new"


def _refresh_in_process(path, start, results):
    def respond(req):
        results.put(("refresh", parse_qs(req.content.decode())["refresh_token"][0]))
        time.sleep(0.05)
        return httpx.Response(200, json={"access_token": "access-new", "refresh_token": "refresh-new", "expires_in": 3600})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        service = ChatGPTAuthService(EncryptedFileSecretStore(path), client=client)
        start.wait(10)
        results.put(("token", service.access_token(REF)))


def test_runtime_and_wizard_processes_share_refresh_lock(tmp_path):
    import multiprocessing

    context = multiprocessing.get_context("fork")
    path = tmp_path / "secrets.json"
    seed(EncryptedFileSecretStore(path), expiry=time.time() - 1)
    start, results = context.Event(), context.Queue()
    processes = [context.Process(target=_refresh_in_process, args=(path, start, results)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        start.set()
        messages = [results.get(timeout=20) for _ in range(3)]
        for process in processes:
            process.join(20)
            assert process.exitcode == 0
        assert messages.count(("refresh", "refresh-old")) == 1
        assert messages.count(("token", "access-new")) == 2
        assert json.loads(EncryptedFileSecretStore(path).get_secret(REF))["refresh_token"] == "refresh-new"
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        results.close()


@pytest.mark.parametrize("code,cleared", [("invalid_grant", True), ("refresh_token_reused", True), ("temporarily_unavailable", False)])
def test_refresh_failure_retains_only_usable_credentials(code, cleared):
    store = InMemorySecretStore()
    seed(store, expiry=time.time() - 1)
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(400 if cleared else 503, json={"error": code})))
    with pytest.raises(ChatGPTError) as failure:
        ChatGPTAuthService(store, client=client).access_token(REF)
    record = json.loads(store.get_secret(REF))
    assert ("refresh_token" not in record) == cleared
    assert record["client_id"] == REF.account
    assert failure.value.code == ("reauthorization_required" if cleared else code)
    assert "refresh-old" not in str(failure.value)


def test_pause_survives_restart_shared_endpoints_and_refresh(tmp_path):
    path = tmp_path / "secrets.json"
    seed(EncryptedFileSecretStore(path))
    ChatGPTAuthService(EncryptedFileSecretStore(path)).pause(REF)
    resolver = LLMCredentialResolver(EncryptedFileSecretStore(path))
    for name in ["luna", "other-model"]:
        ep = endpoint()
        ep.endpoint_id = name
        resolver.refresh()
        with pytest.raises(ChatGPTError) as failure:
            resolver.resolve_api_key(ep)
        assert failure.value.code == QUOTA_CODE
    DirectSDKTransport(resolver.resolve_api_key).resume_subscription(ep)
    assert resolver.resolve_api_key(ep) == "access-old"


def test_codec_subscription_profile_and_api_unchanged():
    codec = OpenAIResponseCodec()
    payload = codec.encode(request(), context()).payload
    assert payload["store"] is False
    assert "max_output_tokens" not in payload and "temperature" not in payload
    assert payload["input"][0]["role"] == "developer"
    assert payload["tools"][0]["type"] == "namespace"
    assert payload["tools"][0]["name"] == "pal"
    api = codec.encode(request(), replace(context(), capabilities={})).payload
    assert api["max_output_tokens"] == 100
    assert api["temperature"] == 0.4
    assert api["tools"][0]["type"] == "function"
    assert api["input"][0]["role"] == "system"


def test_endpoint_rejects_subscription_token_to_other_host():
    ep = endpoint()
    ep.base_url = "https://example.test/v1"
    with pytest.raises(LLMEndpointSpecError):
        LLMEndpointSpec.from_value(ep)
    store = InMemorySecretStore()
    seed(store)
    with pytest.raises(ChatGPTError):
        LLMCredentialResolver(store).resolve_api_key(ep)


@pytest.mark.parametrize("terminal,expected", [(None, "stream_interrupted"), ("response.failed", QUOTA_CODE), ("response.incomplete", "response_incomplete")])
def test_tool_item_without_successful_terminal_is_failure(terminal, expected):
    frames = [_JSONFrame(0, {"type": "response.output_item.done", "output_index": 0, "item": {
        "id": "tool1", "type": "function_call", "call_id": "c1", "namespace": "pal", "name": "probe", "arguments": "{}",
    }})]
    if terminal:
        frames.append(_JSONFrame(1, {"type": terminal, "response": {"error": {"code": QUOTA_CODE}}}))
    with pytest.raises(ChatGPTError) as failure:
        list(OpenAIResponseCodec().decode(frames, context()))
    assert failure.value.code == expected


def test_tool_namespace_survives_replay():
    frames = [_JSONFrame(0, {"type": "response.completed", "response": {"status": "completed", "output": [{
        "type": "function_call", "call_id": "c1", "namespace": "pal", "name": "probe", "arguments": "{}",
    }]}})]
    updates = list(OpenAIResponseCodec().decode(frames, context()))
    message = updates[-1].response.message
    assert message.tool_calls[0].name == "probe"
    req = replace(request(), messages=(*request().messages, message))
    assert OpenAIResponseCodec().encode(req, context()).payload["input"][-1]["namespace"] == "pal"


def test_transport_forces_sse_and_pauses_on_stream_quota(tmp_path):
    store = EncryptedFileSecretStore(tmp_path / "secrets.json")
    seed(store)
    calls = []

    class SDK:
        def frames(self, req):
            calls.append(req)
            yield _JSONFrame(0, {"type": "response.failed", "response": {"error": {"code": QUOTA_CODE}}})

    invoker = ShapeEndpointInvoker(transport=DirectSDKTransport(LLMCredentialResolver(store).resolve_api_key, SDK()))
    with pytest.raises(ChatGPTError):
        invoker.invoke(endpoint(), request(), stream=False)
    assert calls[0].stream is True
    assert json.loads(store.get_secret(REF))["usage_paused"] is True
    with pytest.raises(ChatGPTError):
        invoker.invoke(endpoint(), request(), stream=False)
    assert len(calls) == 1


def test_no_fallback_even_with_global_and_request_override():
    from tests.test_llm_fallback_switch import _runtime, _SwitchSettingsRepository
    runtime = _runtime(_SwitchSettingsRepository(True), active="alpha")
    runtime.endpoint_resolver.endpoints[0].capabilities_blob = {"access_profile": PROFILE}
    assert [ep.endpoint_id for ep in runtime._enabled_endpoints_for_preference(endpoint_fallback_policy="enabled")] == ["alpha"]


@pytest.mark.parametrize("headless", [False, True])
def test_browser_login_validates_identity_and_persists_registration(headless):
    import socket

    store = InMemorySecretStore()
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    public["kid"] = "signing"
    authorize = {}
    callbacks = []

    def server(req):
        if req.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": ISSUER + "/jwks", "revocation_endpoint": ISSUER + "/revoke"})
        if req.url.path == "/jwks":
            return httpx.Response(200, json={"keys": [public]})
        form = parse_qs(req.content.decode())
        assert form["client_id"] == [REF.account]
        assert form["redirect_uri"] == authorize["redirect_uri"]
        token = jwt.encode({"iss": ISSUER, "aud": REF.account, "sub": "user-1", "exp": int(time.time()) + 300,
                            "nonce": authorize["nonce"][0], "email": "user@example.test"}, private, algorithm="RS256", headers={"kid": "signing"})
        return httpx.Response(200, json={"access_token": "access-new", "refresh_token": "refresh-new", "id_token": token,
                                        "scope": SCOPES, "expires_in": 3600})

    def browser(url):
        authorize.update(parse_qs(urlsplit(url).query))
        assert authorize["client_id"] == ["dynamic_agent_client"]
        assert authorize["agent_name_hint"] == ["Pal"]
        def callback():
            target = authorize["redirect_uri"][0] + "?" + urlencode({"state": authorize["state"][0], "code": "one-use", "client_id": REF.account})
            with urlopen(target, timeout=5) as response:
                callbacks.append(response.status)
        thread = threading.Thread(target=callback)
        thread.start()

    service = ChatGPTAuthService(store, client=httpx.Client(transport=httpx.MockTransport(server)))
    if headless:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        ready = []
        def show_url(url):
            assert ready == [port]
            assert parse_qs(urlsplit(url).query)["redirect_uri"] == [f"http://127.0.0.1:{port}/auth/callback"]
            browser(url)  # Simulate a browser reaching the listener through a tunnel.
        result = service.login(open_browser=None, show_url=show_url, timeout=5,
                               callback_port=port, on_listener_ready=ready.append)
    else:
        result = service.login(open_browser=browser, show_url=lambda _: None, timeout=5)
    assert result["client_id"] == REF.account
    assert result["subject"] == "user-1"
    assert "access_token" not in result
    assert service.access_token(REF) == "access-new"
    assert service.accounts()[0]["email"] == "user@example.test"
    assert service.host_id() == service.host_id()


@pytest.mark.parametrize("ssh_session", [True, False])
def test_headless_wizard_prints_forwarding_before_login(monkeypatch, capsys, ssh_session):
    from pal.wizard.prompts import _prompt_chatgpt_login

    monkeypatch.setattr("sys.platform", "linux")
    for name in ("SSH_CONNECTION", "SSH_TTY", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    if ssh_session:
        monkeypatch.setenv("SSH_CONNECTION", "192.0.2.10 54321 192.0.2.20 2222")
    monkeypatch.setattr("socket.gethostname", lambda: "pi-host")
    monkeypatch.setattr("getpass.getuser", lambda: "pi")
    monkeypatch.setattr("pal.wizard.prompts.ask", lambda prompt, default: default)
    class Service:
        def login(self, client_id, **kwargs):
            assert client_id == REF.account
            assert kwargs["open_browser"] is None
            assert kwargs["callback_port"] == 1455
            assert kwargs["timeout"] == 600
            kwargs["on_listener_ready"](1455)
            output = capsys.readouterr().out
            destination = "-p 2222 -- pi@192.0.2.20" if ssh_session else "-p 22 -- pi@pi-host"
            assert f"ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:1455:127.0.0.1:1455 {destination}" in output
            assert "browser computer" in output
            return {"client_id": client_id}
    assert _prompt_chatgpt_login(Service(), REF.account) == {"client_id": REF.account}


def test_headless_port_conflict_does_not_start_authorization():
    import socket

    service = ChatGPTAuthService(InMemorySecretStore())
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        with pytest.raises(OSError):
            service.login(open_browser=None, callback_port=occupied.getsockname()[1],
                          show_url=lambda _: pytest.fail("must bind before offering authorization"))
    assert service.accounts() == []


def test_headless_cancel_closes_listener():
    import socket

    ports = []
    def cancel(port):
        ports.append(port)
        raise KeyboardInterrupt
    service = ChatGPTAuthService(InMemorySecretStore())
    with pytest.raises(KeyboardInterrupt):
        service.login(open_browser=None, on_listener_ready=cancel,
                      show_url=lambda _: pytest.fail("cancelled"))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", ports[0]))
    assert service.accounts() == []


def test_logout_keeps_registration_and_reports_unconfirmed_revocation():
    store = InMemorySecretStore()
    seed(store)
    service = ChatGPTAuthService(store, client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503))))
    assert service.logout(REF.account) is False
    record = json.loads(store.get_secret(REF))
    assert record["subject"] == "user-1"
    assert "refresh_token" not in record


def test_http_quota_is_structured_and_pauses_credentials():
    store = InMemorySecretStore()
    seed(store)

    class SDK:
        def frames(self, req):
            import openai
            response = httpx.Response(429, request=httpx.Request("POST", API_URL + "/responses"),
                                      headers={"x-request-id": "request-123"})
            raise openai.RateLimitError("no usage", response=response, body={"code": QUOTA_CODE, "param": "model"})
            yield

    invoker = ShapeEndpointInvoker(transport=DirectSDKTransport(LLMCredentialResolver(store).resolve_api_key, SDK()))
    with pytest.raises(ChatGPTError) as result:
        invoker.invoke(endpoint(), request())
    details = result.value.to_dict()
    assert {key: details[key] for key in ("code", "status", "param", "request_id")} == {
        "code": QUOTA_CODE, "status": 429, "param": "model", "request_id": "request-123"}
    assert json.loads(details["diagnostic"])["error"] == {"code": QUOTA_CODE, "param": "model"}
    assert json.loads(store.get_secret(REF))["usage_paused"]


def test_refresh_that_loses_permission_does_not_infer():
    store = InMemorySecretStore()
    seed(store, expiry=time.time() - 1)
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        "access_token": "new", "refresh_token": "replacement", "expires_in": 3600, "scope": "openid profile email",
    })))
    with pytest.raises(ChatGPTError) as result:
        ChatGPTAuthService(store, client=client).access_token(REF)
    assert result.value.code == "plan_permission_missing"
    assert json.loads(store.get_secret(REF))["refresh_token"] == "replacement"


def test_model_catalog_uses_account_order_and_filters_visibility():
    store = InMemorySecretStore()
    seed(store)
    def respond(req):
        assert req.headers["authorization"] == "Bearer access-old"
        return httpx.Response(200, json={"models": [
            {"slug": "second", "visibility": "list", "display_name": "Second"},
            {"slug": "hidden", "visibility": "hidden"},
            {"slug": "first", "visibility": "list", "display_name": "First"},
        ]})
    service = ChatGPTAuthService(store, client=httpx.Client(transport=httpx.MockTransport(respond)))
    assert [m["slug"] for m in service.models(REF.account)] == ["second", "first"]


@pytest.mark.parametrize("bad_claim", ["nonce", "aud", "iss", "exp", "signature"])
def test_identity_validation_rejects_invalid_claims(bad_claim):
    store = InMemorySecretStore()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "key"
    def respond(req):
        if req.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": ISSUER + "/keys", "revocation_endpoint": ISSUER + "/revoke"})
        return httpx.Response(200, json={"keys": [jwk]})
    service = ChatGPTAuthService(store, client=httpx.Client(transport=httpx.MockTransport(respond)))
    claims = dict(iss=ISSUER, aud=REF.account, exp=time.time() + 300, nonce="expected", sub="subject")
    if bad_claim == "signature":
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    else:
        claims[bad_claim] = 1 if bad_claim == "exp" else "wrong"
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "key"})
    with pytest.raises(ChatGPTError) as error:
        service._verify_identity(token, REF.account, "expected")
    assert error.value.code == "invalid_identity"


@pytest.mark.parametrize("state", ["wrong", "错误状态"])
def test_invalid_state_does_not_exchange_code(state):
    from urllib.error import HTTPError
    calls = []
    statuses = []
    threads = []
    def respond(req):
        calls.append(req)
        return httpx.Response(500)
    def browser(url):
        params = parse_qs(urlsplit(url).query)
        def send():
            try:
                with urlopen(params["redirect_uri"][0] + "?" + urlencode({'state': state, 'code': 'stolen'}), timeout=2):
                    pass
            except HTTPError as exc:
                statuses.append(exc.code)
        thread = threading.Thread(target=send)
        threads.append(thread)
        thread.start()
    service = ChatGPTAuthService(InMemorySecretStore(), client=httpx.Client(transport=httpx.MockTransport(respond)))
    with pytest.raises(ChatGPTError) as error:
        service.login(open_browser=browser, show_url=lambda _: None, timeout=0.3)
    assert error.value.code == "authorization_timeout"
    for thread in threads:
        thread.join(2)
    assert statuses == [400]
    assert not calls


def test_sse_error_preserves_error_code():
    frames = [_JSONFrame(0, {"type": "error", "code": QUOTA_CODE})]
    with pytest.raises(ChatGPTError) as error:
        list(OpenAIResponseCodec().decode(frames, context()))
    assert error.value.code == QUOTA_CODE


@pytest.mark.parametrize("keep_existing,save", [(True, True), (False, True), (False, False)])
def test_narrow_wizard_preserves_non_llm_configuration(tmp_path, monkeypatch, keep_existing, save):
    from pal.foundation import PalV2Database
    from pal.llm.models import LLMEndpointModel, PalRuntimeSettingModel
    from pal.llm.repository import LLMEndpointRepository, RuntimeSettingRepository
    from pal.wizard.cli import run_llm_setup_wizard
    from pal.wizard.prompts import WizardLLMEndpoint
    import sqlite3

    database = PalV2Database(tmp_path / "pal.sqlite3")
    database.initialize((LLMEndpointModel, PalRuntimeSettingModel))
    api = endpoint()
    api.endpoint_id = "existing-api"
    api.auth_kind = "api_key_ref"
    api.capabilities_blob = {}
    api.credential_ref = "EXISTING_API_KEY"
    LLMEndpointRepository().upsert(**vars(api))
    RuntimeSettingRepository().set_active_llm_endpoint_id("existing-api")
    RuntimeSettingRepository().set_think_level("existing-api", "off")
    database.close()
    seed(EncryptedFileSecretStore(tmp_path / "secrets.json"))
    original_secrets = (tmp_path / "secrets.json").read_bytes()
    config = tmp_path / "config.toml"
    config.write_text('[unchanged]\nvalue = "keep"\n')
    selected = WizardLLMEndpoint(
        endpoint_id="new-chatgpt", model_id="account-model", wire_shape="openai_response", base_url=API_URL,
        api_key=None, context_window=32768, max_output_tokens=4096, thinking_levels=["off"],
        default_thinking_level="off", supports_tools=True, supports_streaming=True, supports_vision=False,
        priority=1, provider="openai", auth_kind="oauth", credential_ref="pal.chatgpt:oaiapp_test",
        capabilities_blob={"access_profile": PROFILE},
    )
    def prompt(current, active, **kwargs):
        assert active == "existing-api"
        assert current[0].credential_ref == "EXISTING_API_KEY"
        return [*(current if keep_existing else []), selected], "new-chatgpt"
    monkeypatch.setattr("pal.wizard.prompts.prompt_llm_endpoints_with_current", prompt)
    monkeypatch.setattr("pal.wizard.cli.ask_yes_no", lambda *a: save)
    monkeypatch.setattr("pal.wizard.cli._prompt_service_setup", lambda *a: pytest.fail("must not set up services"))
    assert run_llm_setup_wizard(runtime_root=tmp_path) == (0 if save else 1)
    assert config.read_text() == '[unchanged]\nvalue = "keep"\n'
    assert (tmp_path / "secrets.json").read_bytes() == original_secrets
    with sqlite3.connect(tmp_path / "pal.sqlite3") as db:
        row = db.execute("SELECT credential_ref FROM llm_endpoints WHERE endpoint_id='existing-api'").fetchone()
        assert row == (("EXISTING_API_KEY",) if keep_existing or not save else None)
        if not save:
            assert db.execute("SELECT endpoint_id FROM llm_endpoints WHERE endpoint_id='new-chatgpt'").fetchone() is None


def test_wizard_aliases_accept_narrow_mode():
    from pal.main import _build_parser
    for command in ("wizard", "wizzard", "setup"):
        parsed = _build_parser().parse_args([command, "--llm", "--runtime-root", "/tmp/runtime"])
        assert parsed.command == "setup" and parsed.llm


@pytest.mark.parametrize("stream", [False, True])
def test_runtime_quota_never_executes_partial_tool_or_falls_back(stream):
    from tests.test_llm_fallback_switch import _runtime, _SwitchSettingsRepository, _fake_endpoint
    from pal.shared import LLMFinishReason
    store = InMemorySecretStore()
    seed(store)
    requests = []
    class SDK:
        def frames(self, req):
            requests.append(req.endpoint_id)
            yield _JSONFrame(0, {"type": "response.output_item.done", "output_index": 0, "item": {
                "type": "function_call", "name": "probe", "call_id": "call1", "arguments": "{}", "namespace": "pal",
            }})
            yield _JSONFrame(1, {"type": "response.failed", "response": {"error": {"code": QUOTA_CODE}}})
    runtime = _runtime(_SwitchSettingsRepository(True), active="chatgpt")
    runtime.endpoint_resolver.endpoints = (endpoint(), _fake_endpoint("paid-api"))
    runtime.endpoint_invoker = ShapeEndpointInvoker(transport=DirectSDKTransport(LLMCredentialResolver(store).resolve_api_key, SDK()))
    if stream:
        final = list(runtime._iter_stream_updates(request()))[-1].response
    else:
        final = runtime.generate(request()).response
    assert final.finish_reason == LLMFinishReason.ERROR
    assert not final.message.tool_calls
    assert final.message.metadata["chatgpt_failure"]["code"] == QUOTA_CODE
    assert requests == ["chatgpt"]
    runtime.generate(request())
    assert requests == ["chatgpt"]
    runtime.set_active_endpoint("chatgpt")  # internal activation is not consent to retry
    runtime.generate(request())
    assert requests == ["chatgpt"]
    runtime.resume_subscription("chatgpt")
    runtime.generate(request())
    assert requests == ["chatgpt", "chatgpt"]


def test_subscription_preflight_checks_tool_round_trip():
    from pal.llm.ir import LLMResponseIR
    from pal.shared import LLMFinishReason
    from pal.wizard.prompts import WizardLLMEndpoint, run_llm_endpoint_preflight
    ep = endpoint()
    wizard = WizardLLMEndpoint(
        endpoint_id=ep.endpoint_id, model_id=ep.model_id, wire_shape=ep.wire_shape, base_url=ep.base_url,
        api_key=None, context_window=ep.context_window, max_output_tokens=ep.max_output_tokens,
        thinking_levels=["low", "medium", "high"], default_thinking_level="high", supports_tools=True, supports_streaming=True,
        supports_vision=False, priority=0, auth_kind="oauth", credential_ref=ep.credential_ref,
        capabilities_blob=ep.capabilities_blob,
    )
    class Invoker:
        calls = []
        def invoke(self, ep, req, **kwargs):
            self.calls.append(req)
            if len(self.calls) == 2:
                return LLMResponseIR(LLMMessageIR(MessageRole.ASSISTANT, (ToolCallIR("c1", "pal_preflight_probe", {"ok": True}),)), LLMFinishReason.TOOL_CALLS)
            return LLMResponseIR(LLMMessageIR(MessageRole.ASSISTANT, (TextPartIR("PAL_PREFLIGHT_OK"),)), LLMFinishReason.STOP)
    invoker = Invoker()
    result = run_llm_endpoint_preflight(wizard, invoker=invoker)
    assert result.status == "ok"
    assert len(invoker.calls) == 3
    assert all(call.policy.thinking_level.value == "high" for call in invoker.calls)
    assert invoker.calls[2].messages[:2] == invoker.calls[1].messages
    assert invoker.calls[2].messages[-1].role == MessageRole.TOOL
    assert invoker.calls[2].messages[-1].parts[0].call_id == "c1"


def test_api_http_errors_keep_existing_retry_classification():
    import openai
    from pal.llm.runtime import _classify_retry_error
    response = httpx.Response(429, request=httpx.Request("POST", "https://api.example.test"))
    exc = openai.RateLimitError("Error code: 429", response=response, body={"code": "rate_limit_exceeded"})
    assert _classify_retry_error(exc) == "rate_limit"


def test_bunshin_proxy_preserves_subscription_failure(tmp_path):
    import asyncio
    from tests.test_bunshin_llm_transport import _manager
    from pal.bunshin.manager import BunshinRunState
    from pal.bunshin.ipc import start_manager_server, cleanup_manager_endpoint
    from pal.bunshin.llm_transport import ManagerProxyTransport
    from pal.llm.repository import LLMEndpointRepository
    from pal.shared import BunshinInvocationPack

    async def scenario():
        database, manager = _manager(tmp_path)
        ep = LLMEndpointRepository().upsert(**vars(endpoint()))
        manager.runs["run-1"] = BunshinRunState(
            bunshin_id="bunshin-1", run_id="run-1", pack=BunshinInvocationPack(invocation_id="bunshin-1"),
        )
        class Transport:
            def frames(self, ep, req):
                raise ChatGPTError(QUOTA_CODE, status=429, request_id="request-id")
                yield
        manager._llm_json_transport = Transport()
        server, _ = await start_manager_server(tmp_path, manager._handle_client)
        try:
            proxy = ShapeEndpointInvoker(transport=ManagerProxyTransport(tmp_path, "run-1", request_timeout_seconds=2))
            with pytest.raises(ChatGPTError) as error:
                await asyncio.to_thread(proxy.invoke, ep, request())
            assert error.value.code == QUOTA_CODE
            assert error.value.status == 429
            assert error.value.request_id == "request-id"
        finally:
            server.close()
            await server.wait_closed()
            await cleanup_manager_endpoint(tmp_path)
            database.close()
    asyncio.run(scenario())


def test_bunshin_quota_blocks_instead_of_worker_retry():
    import asyncio
    from pal.bunshin.runner_components.llm_rounds import LlmRounds
    from pal.core.turns import EffectResult
    from pal.llm.runtime import _failure_result
    from pal.shared import RuntimeStatus
    blocked = []
    # Quota must short-circuit before any completion heuristics or reporter.
    rounds = LlmRounds(
        completion=None, control=None, heartbeat=None, prompt_context=None, reporter=None,
        status=SimpleNamespace(block=blocked.append), text_deliverables=None, tool_session=None, pack=None,
    )
    failure = ChatGPTError(QUOTA_CODE)
    state = SimpleNamespace(llm_round_count=1)
    result = EffectResult(status=RuntimeStatus.OK, payload=_failure_result(failure.user_message, exc=failure))
    assert asyncio.run(rounds.postprocess_bunshin_llm_round(state, result)) is result
    assert blocked == [failure.user_message]
    assert state.llm_round_count == 0


def test_catalog_reasoning_replaces_legacy_off_and_reports_unsupported(monkeypatch, capsys):
    from pal.wizard.prompts import _prompt_chatgpt_thinking
    model = {'slug': 'gpt-6-astra', 'default_reasoning_level': 'medium',
             'supported_reasoning_levels': [{'effort': level} for level in ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']]}
    current = SimpleNamespace(model_id='gpt-6-astra', thinking_levels=['off'], default_thinking_level='off')
    answers = iter(['off', 'medium'])
    monkeypatch.setattr('pal.wizard.prompts.ask', lambda *args: next(answers))
    levels, default = _prompt_chatgpt_thinking(model, current)
    assert levels == ['low', 'medium', 'high', 'xhigh', 'max']
    assert default == 'medium'
    output = capsys.readouterr().out
    assert 'ultra' in output and 'not mapped' in output
    assert 'Choose one of' in output
    for level in levels:
        payload = OpenAIResponseCodec().encode(replace(request(), policy=GenerationPolicyIR(max_output_tokens=32, thinking_level=level)), context()).payload
        assert payload['reasoning']['effort'] == level


def test_catalog_reasoning_preserves_supported_user_default(monkeypatch):
    from pal.wizard.prompts import _prompt_chatgpt_thinking
    monkeypatch.setattr('pal.wizard.prompts.ask', lambda prompt, default: default)
    model = {'slug': 'model', 'default_reasoning_level': 'medium',
             'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'medium'}, {'effort': 'high'}]}
    current = SimpleNamespace(model_id='model', thinking_levels=['low', 'high'], default_thinking_level='high')
    assert _prompt_chatgpt_thinking(model, current) == (['low', 'medium', 'high'], 'high')
    model['slug'] = 'different-model'
    assert _prompt_chatgpt_thinking(model, current)[1] == 'medium'


def test_catalog_none_is_not_misrepresented_as_off(monkeypatch, capsys):
    from pal.wizard.prompts import _prompt_chatgpt_thinking
    monkeypatch.setattr('pal.wizard.prompts.ask', lambda prompt, default: default)
    model = {'slug': 'model', 'default_reasoning_level': 'medium',
             'supported_reasoning_levels': [{'effort': 'none'}, {'effort': 'medium'}]}
    assert _prompt_chatgpt_thinking(model, None) == (['medium'], 'medium')
    assert 'none (not mapped' in capsys.readouterr().out


def test_missing_catalog_reasoning_requires_explicit_configuration(monkeypatch):
    from pal.wizard.prompts import _prompt_chatgpt_thinking
    answers = iter(['low,medium,high', 'medium'])
    monkeypatch.setattr('pal.wizard.prompts.ask', lambda *args: next(answers))
    assert _prompt_chatgpt_thinking({'slug': 'unknown'}, None) == (['low', 'medium', 'high'], 'medium')
