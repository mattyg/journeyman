"""Zero-dependency LLM client for the FreeCAD copilot.

Provider-agnostic over anything that speaks the OpenAI chat-completions format
(OpenAI, Ollama, OpenRouter, most gateways) plus Anthropic's native Messages
API. Uses only the Python standard library (urllib + json) so it runs inside
FreeCAD's bundled interpreter with no pip install and no vendored packages.

Provider is selected from the model string prefix:
  - "anthropic/<model>"  -> Anthropic native /v1/messages
  - anything else        -> OpenAI-compatible /v1/chat/completions
The provider prefix is stripped before the model name is sent on the wire.
"""

import json
import urllib.request
import urllib.error
from dataclasses import dataclass

from .settings import Settings

SYSTEM_PROMPT = (
    "You are a CAD copilot operating inside FreeCAD. You receive a text snapshot "
    "of the active document before each turn. To change the model, call the "
    "run_freecad_script tool with (1) `intent`: one plain-language sentence a "
    "non-programmer designer will read, and (2) `script`: FreeCAD Python using the "
    "App/Part/PartDesign APIs against App.ActiveDocument. Prefer editing existing "
    "objects and referencing existing geometry over rebuilding. Do not call "
    "recompute or manage transactions yourself; the host handles that. When the "
    "task is complete or you need the user to look, reply with plain text instead "
    "of a tool call."
)

# Provider-neutral description of the one tool the model may call. The two
# adapters translate this into each provider's own tool-definition shape.
_TOOL_NAME = "run_freecad_script"
_TOOL_DESCRIPTION = "Execute FreeCAD Python against the active document."
_TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "intent": {"type": "string",
                   "description": "One plain-language sentence describing the change."},
        "script": {"type": "string",
                   "description": "FreeCAD Python to execute."},
    },
    "required": ["intent", "script"],
}

# OpenAI-format tool schema.
TOOL_SCHEMA = [{
    "type": "function",
    "function": {
        "name": _TOOL_NAME,
        "description": _TOOL_DESCRIPTION,
        "parameters": _TOOL_PARAMETERS,
    },
}]

# Anthropic-format tool schema (input_schema instead of parameters).
ANTHROPIC_TOOL_SCHEMA = [{
    "name": _TOOL_NAME,
    "description": _TOOL_DESCRIPTION,
    "input_schema": _TOOL_PARAMETERS,
}]

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096

# Base URLs used when settings.api_base is not set.
_OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"
_OPENROUTER_DEFAULT_BASE = "https://openrouter.ai/api/v1"
_ANTHROPIC_DEFAULT_BASE = "https://api.anthropic.com/v1"


@dataclass
class LLMProposal:
    intent: str
    script: str
    text: str
    is_tool_call: bool


class LLMError(Exception):
    """Raised when the provider request fails or returns an unusable response."""


def _http_post_json(url: str, headers: dict, payload: dict) -> dict:
    """POST a JSON body and parse a JSON response, using only the stdlib.

    Isolated so tests can monkeypatch a single seam instead of the network.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")
        except Exception:
            pass
        raise LLMError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"Could not reach {url}: {exc.reason}") from exc
    return json.loads(body)


def _split_model(model: str):
    """Return (provider, wire_model) from a possibly prefixed model string."""
    if "/" in model:
        provider, rest = model.split("/", 1)
        return provider.lower(), rest
    return "", model


def _base_url(settings: Settings, provider: str) -> str:
    if settings.api_base:
        return settings.api_base.rstrip("/")
    if provider == "openrouter":
        return _OPENROUTER_DEFAULT_BASE
    if provider == "anthropic":
        return _ANTHROPIC_DEFAULT_BASE
    return _OPENAI_DEFAULT_BASE


def _complete_openai(wire_model: str, provider: str, messages: list,
                     settings: Settings) -> "LLMProposal":
    url = _base_url(settings, provider) + "/chat/completions"
    headers = {}
    if settings.api_key:
        headers["Authorization"] = "Bearer " + settings.api_key
    payload = {
        "model": wire_model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
        "tools": TOOL_SCHEMA,
    }
    resp = _http_post_json(url, headers, payload)
    try:
        message = resp["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"Unexpected response shape: {resp!r}") from exc
    tool_calls = message.get("tool_calls")
    if tool_calls:
        args = json.loads(tool_calls[0]["function"]["arguments"])
        return LLMProposal(intent=args.get("intent", ""),
                           script=args.get("script", ""),
                           text=message.get("content") or "",
                           is_tool_call=True)
    return LLMProposal(intent="", script="",
                       text=message.get("content") or "",
                       is_tool_call=False)


def _complete_anthropic(wire_model: str, messages: list,
                        settings: Settings) -> "LLMProposal":
    url = _base_url(settings, "anthropic") + "/messages"
    headers = {"anthropic-version": ANTHROPIC_VERSION}
    if settings.api_key:
        headers["x-api-key"] = settings.api_key
    payload = {
        "model": wire_model,
        "max_tokens": DEFAULT_MAX_TOKENS,
        "system": SYSTEM_PROMPT,          # system is top-level, not a message
        "messages": messages,
        "tools": ANTHROPIC_TOOL_SCHEMA,
    }
    resp = _http_post_json(url, headers, payload)
    content = resp.get("content")
    if not isinstance(content, list):
        raise LLMError(f"Unexpected response shape: {resp!r}")
    text_parts = []
    for block in content:
        if block.get("type") == "tool_use" and block.get("name") == _TOOL_NAME:
            args = block.get("input") or {}
            return LLMProposal(intent=args.get("intent", ""),
                               script=args.get("script", ""),
                               text="".join(text_parts),
                               is_tool_call=True)
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
    return LLMProposal(intent="", script="",
                       text="".join(text_parts),
                       is_tool_call=False)


def complete(messages: list, settings: Settings) -> "LLMProposal":
    provider, wire_model = _split_model(settings.model)
    if provider == "anthropic":
        return _complete_anthropic(wire_model, messages, settings)
    return _complete_openai(wire_model, provider, messages, settings)
