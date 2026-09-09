"""Official Codex app-server adapter for ChatGPT subscription access.

The Codex executable owns OAuth credentials and refresh. Journeyman only speaks
its documented JSON-RPC protocol and never reads or persists tokens.
"""

import json
import subprocess
from dataclasses import dataclass


class CodexError(Exception):
    """Codex app-server is unavailable or returned unusable output."""


class CodexAuthError(CodexError):
    """A managed ChatGPT login did not complete."""


class CodexProtocolError(CodexError):
    """Codex app-server rejected a request."""


@dataclass(frozen=True)
class Account:
    auth_mode: str = ""
    plan_type: str = ""
    email: str = ""
    requires_auth: bool = False

    @property
    def signed_in(self):
        return self.auth_mode == "chatgpt"


class _StdioTransport:
    def __init__(self):
        try:
            self._process = subprocess.Popen(
                ["codex", "app-server"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1)
        except (FileNotFoundError, OSError) as exc:
            raise CodexError(
                "Codex CLI is required for ChatGPT subscription login. "
                "Install Codex, then try again.") from exc

    def send(self, message):
        try:
            self._process.stdin.write(json.dumps(message) + "\n")
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CodexError("Codex app-server stopped unexpectedly.") from exc

    def receive(self):
        line = self._process.stdout.readline()
        if not line:
            raise CodexError("Codex app-server stopped unexpectedly.")
        try:
            return json.loads(line)
        except (TypeError, ValueError) as exc:
            raise CodexProtocolError(
                "Codex app-server returned an invalid response.") from exc

    def close(self):
        process = self._process
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


class _Client:
    def __init__(self, transport_factory):
        self.transport = transport_factory()
        self._next_id = 0
        self._initialize()

    def close(self):
        self.transport.close()

    def _initialize(self):
        self.request("initialize", {
            "clientInfo": {
                "name": "freecad_journeyman",
                "title": "FreeCAD Journeyman",
                "version": "0.1.0",
            },
            "capabilities": {"experimentalApi": True},
        })
        self.transport.send({"method": "initialized", "params": {}})

    def send_request(self, method, params=None):
        request_id = self._next_id
        self._next_id += 1
        self.transport.send({
            "method": method, "id": request_id, "params": params or {}})
        return request_id

    def request(self, method, params=None):
        request_id = self.send_request(method, params)
        while True:
            message = self.transport.receive()
            if message.get("id") != request_id:
                continue
            if "error" in message:
                # Never forward server text: it may echo credentials.
                raise CodexProtocolError(
                    "Codex app-server rejected %s." % method)
            return message.get("result") or {}


def _with_client(transport_factory, operation):
    client = _Client(transport_factory)
    try:
        return operation(client)
    finally:
        client.close()


def _account(result):
    value = result.get("account") or {}
    return Account(
        str(value.get("type") or ""),
        str(value.get("planType") or ""),
        str(value.get("email") or ""),
        bool(result.get("requiresOpenaiAuth") and not value),
    )


def account_status(transport_factory=_StdioTransport):
    try:
        return _with_client(
            transport_factory,
            lambda client: _account(client.request(
                "account/read", {"refreshToken": True})))
    except CodexProtocolError as exc:
        raise CodexAuthError(
            "OpenAI session could not be refreshed. Sign in again.") from exc


def login(open_url, transport_factory=_StdioTransport):
    """Run Codex-managed browser OAuth and return the authenticated account.

    ``open_url`` is invoked with the authorization URL. GUI callers should
    implement it with a queued Qt signal so browser launch stays on the main
    thread.
    """
    def operation(client):
        request_id = client.send_request("account/login/start", {
            "type": "chatgpt",
            "useHostedLoginSuccessPage": True,
            "appBrand": "chatgpt",
        })
        login_id = ""
        opened = False
        while True:
            message = client.transport.receive()
            if message.get("id") == request_id:
                if "error" in message:
                    raise CodexAuthError("OpenAI sign-in could not start.")
                result = message.get("result") or {}
                login_id = str(result.get("loginId") or "")
                auth_url = str(result.get("authUrl") or "")
                if not auth_url:
                    raise CodexAuthError(
                        "Codex did not provide a sign-in URL.")
                open_url(auth_url)
                opened = True
                continue
            if message.get("method") != "account/login/completed":
                continue
            params = message.get("params") or {}
            if login_id and params.get("loginId") != login_id:
                continue
            if not params.get("success"):
                raise CodexAuthError(
                    "OpenAI sign-in failed or was cancelled.")
            if not opened:
                raise CodexAuthError("Sign-in completed without authorization.")
            return _account(client.request(
                "account/read", {"refreshToken": False}))

    return _with_client(transport_factory, operation)


def list_models(transport_factory=_StdioTransport):
    def operation(client):
        result = client.request(
            "model/list", {"limit": 100, "includeHidden": False})
        return [
            str(item.get("model") or item.get("id") or "")
            for item in (result.get("data") or [])
            if isinstance(item, dict) and (item.get("model") or item.get("id"))
        ]

    return _with_client(transport_factory, operation)


def logout(transport_factory=_StdioTransport):
    return _with_client(
        transport_factory,
        lambda client: client.request("account/logout"))


def _dynamic_tools(settings):
    from .llm_client import _openai_tools

    return [{
        "name": tool["function"]["name"],
        "description": tool["function"]["description"],
        "inputSchema": tool["function"]["parameters"],
    } for tool in _openai_tools(settings)]


def _turn_input(messages, system_prompt):
    text_parts = [system_prompt]
    images = []
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, list):
            blocks = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    blocks.append(str(block["text"]))
                elif block.get("type") == "image_url":
                    url = str((block.get("image_url") or {}).get("url") or "")
                    if url:
                        images.append({"type": "image", "url": url})
            text = "\n".join(blocks)
        else:
            text = str(content)
        if text:
            text_parts.append(
                "%s: %s" % (message.get("role", "user"), text))
    return [{"type": "text", "text": "\n\n".join(text_parts)}] + images

def complete(messages, settings, transport_factory=_StdioTransport):
    """Request one Journeyman tool proposal through Codex app-server."""
    from . import llm_client

    def operation(client):
        _provider, model = llm_client._split_model(settings.model)
        thread = client.request("thread/start", {
            "model": model,
            "ephemeral": True,
            "sandbox": "read-only",
            "dynamicTools": _dynamic_tools(settings),
        }).get("thread") or {}
        thread_id = thread.get("id")
        if not thread_id:
            raise CodexProtocolError("Codex did not create a conversation.")
        turn_id = client.send_request("turn/start", {
            "threadId": thread_id,
            "input": _turn_input(messages, llm_client._system_prompt(settings)),
        })
        text = ""
        while True:
            message = client.transport.receive()
            if message.get("id") == turn_id and "error" in message:
                raise CodexProtocolError(
                    "Codex app-server rejected turn/start.")
            method = message.get("method")
            params = message.get("params") or {}
            if method == "item/tool/call":
                name = str(params.get("tool") or "")
                arguments = params.get("arguments") or {}
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError as exc:
                        raise CodexProtocolError(
                            "Codex returned invalid tool arguments.") from exc
                proposal = llm_client._proposal_from_tool(
                    name, arguments, text=text)
                if proposal is not None:
                    return proposal
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                if turn.get("status") == "failed":
                    raise CodexError(
                        "Codex could not complete the request. Check that the "
                        "selected model supports ChatGPT subscription access "
                        "and sign in again if needed.")
                return llm_client.FinishProposal(
                    "", "", text, False, kind="finish")
            elif method == "item/agentMessage/delta":
                text += str(params.get("delta") or "")

    return _with_client(transport_factory, operation)
