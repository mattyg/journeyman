import json

import pytest

from freecad.journeyman import codex_app_server as cas
from freecad.journeyman.config.settings import Settings


class FakeTransport:
    def __init__(self, incoming):
        self.incoming = iter(incoming)
        self.sent = []
        self.closed = False

    def send(self, message):
        self.sent.append(message)

    def receive(self):
        return next(self.incoming)

    def close(self):
        self.closed = True


def _response(request_id, result):
    return {"id": request_id, "result": result}


def test_browser_login_returns_authenticated_subscription_account():
    transport = FakeTransport([
        _response(0, {}),
        _response(1, {
            "type": "chatgpt", "loginId": "login-1",
            "authUrl": "https://chatgpt.com/auth",
        }),
        {"method": "account/login/completed", "params": {
            "loginId": "login-1", "success": True, "error": None,
        }},
        {"method": "account/updated", "params": {
            "authMode": "chatgpt", "planType": "plus",
        }},
        _response(2, {"account": {
            "type": "chatgpt", "email": "person@example.com",
            "planType": "plus",
        }, "requiresOpenaiAuth": True}),
    ])
    urls = []

    account = cas.login(urls.append, transport_factory=lambda: transport)

    assert urls == ["https://chatgpt.com/auth"]
    assert account == cas.Account("chatgpt", "plus", "person@example.com")
    assert transport.sent[2]["params"]["type"] == "chatgpt"
    assert transport.closed is True


def test_cancelled_login_surfaces_actionable_error():
    transport = FakeTransport([
        _response(0, {}),
        _response(1, {
            "type": "chatgpt", "loginId": "login-1",
            "authUrl": "https://chatgpt.com/auth",
        }),
        {"method": "account/login/completed", "params": {
            "loginId": "login-1", "success": False,
            "error": "Login cancelled",
        }},
    ])

    with pytest.raises(
            cas.CodexAuthError, match="failed or was cancelled"):
        cas.login(lambda _url: None, transport_factory=lambda: transport)

def test_account_status_refreshes_managed_tokens():
    transport = FakeTransport([
        _response(0, {}),
        _response(1, {"account": {
            "type": "chatgpt", "email": "person@example.com",
            "planType": "pro",
        }, "requiresOpenaiAuth": True}),
    ])

    account = cas.account_status(transport_factory=lambda: transport)

    assert account.signed_in is True
    assert transport.sent[2]["params"]["refreshToken"] is True


def test_expired_session_requires_actionable_reauthentication():
    transport = FakeTransport([
        _response(0, {}),
        {"id": 1, "error": {
            "code": 401, "message": "expired oauth-secret-token"}},
    ])

    with pytest.raises(cas.CodexAuthError, match="Sign in again") as caught:
        cas.account_status(transport_factory=lambda: transport)

    assert "oauth-secret-token" not in str(caught.value)


def test_revoked_session_is_reported_as_signed_out():
    transport = FakeTransport([
        _response(0, {}),
        _response(1, {"account": None, "requiresOpenaiAuth": True}),
    ])

    account = cas.account_status(transport_factory=lambda: transport)

    assert account.signed_in is False
    assert account.requires_auth is True



def test_logout_clears_codex_managed_credentials():
    transport = FakeTransport([_response(0, {}), _response(1, {})])

    cas.logout(transport_factory=lambda: transport)

    assert transport.sent[2]["method"] == "account/logout"
    assert transport.closed is True


def test_subscription_model_listing_uses_codex_catalog():
    transport = FakeTransport([
        _response(0, {}),
        _response(1, {"data": [
            {"id": "gpt-5.6-sol", "model": "gpt-5.6-sol"},
            {"id": "gpt-5.5-codex", "model": "gpt-5.5-codex"},
        ]}),
    ])

    assert cas.list_models(transport_factory=lambda: transport) == [
        "gpt-5.6-sol", "gpt-5.5-codex"]
    assert transport.sent[2]["method"] == "model/list"


def test_failed_subscription_turn_is_actionable():
    transport = FakeTransport([
        _response(0, {}),
        _response(1, {"thread": {"id": "thread-1"}}),
        _response(2, {"turn": {"id": "turn-1"}}),
        {"method": "turn/completed", "params": {"turn": {
            "status": "failed", "error": {"message": "secret detail"},
        }}},
    ])
    settings = Settings("openai/unsupported", "", "", True, False, 5, 3,
                        openai_auth_method="chatgpt")

    with pytest.raises(
            cas.CodexError, match="selected model supports") as caught:
        cas.complete([], settings, transport_factory=lambda: transport)

    assert "secret detail" not in str(caught.value)


def test_rejected_turn_never_exposes_server_error_text():
    secret = "authorization-code-secret"
    transport = FakeTransport([
        _response(0, {}),
        _response(1, {"thread": {"id": "thread-1"}}),
        {"id": 2, "error": {
            "code": 401, "message": "rejected " + secret}},
    ])
    settings = Settings("openai/gpt-5.6-sol", "", "", True, False, 5, 3,
                        openai_auth_method="chatgpt")

    with pytest.raises(cas.CodexProtocolError) as caught:
        cas.complete([], settings, transport_factory=lambda: transport)

    assert secret not in str(caught.value)


def test_completion_returns_journeyman_dynamic_tool_proposal():
    transport = FakeTransport([
        _response(0, {}),
        _response(1, {"thread": {"id": "thread-1"}}),
        _response(2, {"turn": {"id": "turn-1"}}),
        {"method": "item/tool/call", "id": 40, "params": {
            "tool": "run_freecad_script",
            "arguments": {
                "intent": "Create a box",
                "script": "print('box')",
                "strategy": "part_design",
            },
        }},
    ])
    settings = Settings("openai/gpt-5.4", "", "", True, False, 5, 3,
                        openai_auth_method="chatgpt")

    proposal = cas.complete(
        [{"role": "user", "content": "make a box"}], settings,
        transport_factory=lambda: transport)

    assert proposal.intent == "Create a box"
    thread_request = transport.sent[2]
    assert thread_request["method"] == "thread/start"
    assert thread_request["params"]["ephemeral"] is True
    assert any(tool["name"] == "run_freecad_script"
               for tool in thread_request["params"]["dynamicTools"])
    assert transport.closed is True


def test_protocol_errors_never_include_credentials():
    secret = "oauth-secret-token"
    transport = FakeTransport([
        _response(0, {}),
        {"id": 1, "error": {"code": 401, "message": "Unauthorized",
                              "data": {"token": secret}}},
    ])

    with pytest.raises(cas.CodexAuthError) as caught:
        cas.account_status(transport_factory=lambda: transport)

    assert secret not in str(caught.value)
