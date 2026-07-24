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
import socket
import urllib.request
import urllib.error
from dataclasses import dataclass

from .settings import Settings

SYSTEM_PROMPT = (
    "You are a CAD copilot operating inside FreeCAD. You receive a text snapshot "
    "of the active document before each turn.\n"
    "\n"
    "You have exactly two tools and MUST call one on every turn (you cannot reply "
    "with free text):\n"
    "- run_freecad_script(intent, script): do work. `intent` is one plain-language "
    "sentence a non-programmer will read; `script` is FreeCAD Python run against "
    "App.ActiveDocument. ALL code — including diagnostics — goes here.\n"
    "- finish(summary): call this ONLY when the whole task is complete, or to ask "
    "the user a question. `summary` is the message shown to the user.\n"
    "\n"
    "If a previous script errored, FIX IT and call run_freecad_script again — do "
    "NOT call finish to describe the fix. Keep calling run_freecad_script until the "
    "geometry is actually done, then call finish once.\n"
    "\n"
    "To inspect the model, call run_freecad_script with a script that uses print(); its "
    "stdout is returned to you as [script output] on the next turn, so you can "
    "check values (vertices, isClosed, validity) and then act on what you see. "
    "Diagnostic scripts go through the tool too — never paste them as plain text.\n"
    "\n"
    "FreeCAD API rules (follow exactly):\n"
    "- If there is no active document, create one: doc = App.newDocument().\n"
    "- `doc` has NO `XY_Plane`, `Body`, or origin attributes. Those do not exist "
    "on the document.\n"
    "- For PartDesign, create a Body first: body = "
    "doc.addObject('PartDesign::Body','Body'); doc.recompute(). The origin planes "
    "are body.Origin.OriginFeatures (or reference them by their real Name shown "
    "in the snapshot). A sketch attaches via "
    "sketch.AttachmentSupport = [(plane, '')]; sketch.MapMode = 'FlatFace'.\n"
    "- For simple solids, prefer the Part module: e.g. "
    "box = doc.addObject('Part::Box','Box'); box.Length=10 ... — it needs no "
    "Body or sketch.\n"
    "- Refer to existing objects by the exact Name shown in the snapshot; do not "
    "guess attribute names. Prefer editing existing objects over rebuilding.\n"
    "- Do not call recompute for transaction/undo purposes or manage "
    "transactions; the host wraps each script in one undoable transaction. You "
    "MAY call doc.recompute() when a feature needs recomputing to proceed."
)

# The model always calls exactly one of two tools (tool_choice is forced), so it
# can never emit a free-text reply — which structurally prevents scripts from
# leaking into prose. run_freecad_script = do work; finish = the turn is done.
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

_FINISH_NAME = "finish"
_FINISH_DESCRIPTION = (
    "Call this ONLY when the whole task is complete (or to ask the user a "
    "question). Its `summary` is shown to the user as your reply. Do NOT put any "
    "code here — all code goes to run_freecad_script.")
_FINISH_PARAMETERS = {
    "type": "object",
    "properties": {
        "summary": {"type": "string",
                    "description": "Plain-language message to the user."},
    },
    "required": ["summary"],
}

# OpenAI-format tool schema (both tools).
TOOL_SCHEMA = [
    {"type": "function", "function": {
        "name": _TOOL_NAME, "description": _TOOL_DESCRIPTION,
        "parameters": _TOOL_PARAMETERS}},
    {"type": "function", "function": {
        "name": _FINISH_NAME, "description": _FINISH_DESCRIPTION,
        "parameters": _FINISH_PARAMETERS}},
]

# Anthropic-format tool schema (input_schema instead of parameters).
ANTHROPIC_TOOL_SCHEMA = [
    {"name": _TOOL_NAME, "description": _TOOL_DESCRIPTION,
     "input_schema": _TOOL_PARAMETERS},
    {"name": _FINISH_NAME, "description": _FINISH_DESCRIPTION,
     "input_schema": _FINISH_PARAMETERS},
]

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096

# Base URLs used when settings.api_base is not set.
_OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"
_OPENROUTER_DEFAULT_BASE = "https://openrouter.ai/api/v1"
_ANTHROPIC_DEFAULT_BASE = "https://api.anthropic.com/v1"

# Network timeouts (seconds). Without these, urllib blocks forever on a slow or
# unreachable endpoint — which would hang the model-list fetch and the chat
# call. Listing models is a quick metadata call; completions can take longer.
MODELS_TIMEOUT = 15
COMPLETION_TIMEOUT = 120


@dataclass
class LLMProposal:
    intent: str
    script: str
    text: str
    is_tool_call: bool          # True for a run_freecad_script call
    reasoning: str = ""         # model's thinking, if the provider exposed it
    kind: str = "script"        # "script" (run_freecad_script) | "finish"

    @property
    def is_finish(self) -> bool:
        return self.kind == "finish"


# Which providers expose a reasoning-effort knob (drives the Preferences UI).
PROVIDERS_WITH_REASONING = ("anthropic", "openai", "openrouter")

# Abstract effort levels stored in settings; translated per provider at request
# time. "off" means don't request reasoning at all.
REASONING_LEVELS = ("off", "low", "medium", "high")


class LLMError(Exception):
    """Raised when the provider request fails or returns an unusable response."""


# Optional diagnostic sink. The GUI sets this to route trace lines to the
# FreeCAD console; kept as a plain callable so this module needs no FreeCAD
# import. No-op by default.
DEBUG_LOG = None


def _debug(msg: str) -> None:
    cb = DEBUG_LOG
    if cb is not None:
        try:
            cb(msg)
        except Exception:
            pass


def _http_post_json(url: str, headers: dict, payload: dict,
                    timeout: float = COMPLETION_TIMEOUT) -> dict:
    """POST a JSON body and parse a JSON response, using only the stdlib.

    Isolated so tests can monkeypatch a single seam instead of the network.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")
        except Exception:
            pass
        raise LLMError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except socket.timeout as exc:
        raise LLMError(f"Timed out after {timeout}s contacting {url}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, socket.timeout):
            raise LLMError(f"Timed out after {timeout}s contacting {url}") from exc
        raise LLMError(f"Could not reach {url}: {reason}") from exc
    return json.loads(body)


def _http_get_json(url: str, headers: dict,
                   timeout: float = MODELS_TIMEOUT) -> dict:
    """GET a JSON response, using only the stdlib. Separate seam from POST so
    tests can stub model-listing independently of completions."""
    req = urllib.request.Request(url, method="GET")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")
        except Exception:
            pass
        raise LLMError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except socket.timeout as exc:
        raise LLMError(f"Timed out after {timeout}s contacting {url}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, socket.timeout):
            raise LLMError(f"Timed out after {timeout}s contacting {url}") from exc
        raise LLMError(f"Could not reach {url}: {reason}") from exc
    return json.loads(body)


def list_models(provider: str, settings: Settings) -> list:
    """Fetch the available model ids for a provider.

    Returns a list of bare model-id strings (as the provider reports them).
    Raises LLMError on network/HTTP failure or an unexpected response shape so
    the caller can fall back to a cached/curated list.
    """
    provider = (provider or "").lower()
    if provider == "ollama":
        # Ollama's native tags endpoint lives at the host root, not under /v1.
        base = (settings.api_base or _base_url(settings, "ollama")).rstrip("/")
        root = base[:-3] if base.endswith("/v1") else base
        resp = _http_get_json(root + "/api/tags", {})
        models = resp.get("models") or []
        return [m.get("name", "") for m in models if m.get("name")]
    if provider == "anthropic":
        url = _base_url(settings, "anthropic") + "/models"
        headers = {"anthropic-version": ANTHROPIC_VERSION}
        if settings.api_key:
            headers["x-api-key"] = settings.api_key
    else:  # openai, openrouter, and any OpenAI-compatible endpoint
        url = _base_url(settings, provider) + "/models"
        headers = {}
        if settings.api_key:
            headers["Authorization"] = "Bearer " + settings.api_key
    resp = _http_get_json(url, headers)
    data = resp.get("data")
    if not isinstance(data, list):
        raise LLMError(f"Unexpected models response: {resp!r}")
    return [item.get("id", "") for item in data if item.get("id")]


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


def _openai_reasoning(message: dict) -> str:
    """Best-effort reasoning extraction from an OpenAI-style message. Different
    providers/models expose it under different keys (or not at all)."""
    r = message.get("reasoning") or message.get("reasoning_content")
    if isinstance(r, str):
        return r
    # OpenRouter can return structured reasoning_details: [{text/summary}, ...]
    details = message.get("reasoning_details")
    if isinstance(details, list):
        parts = []
        for d in details:
            if isinstance(d, dict):
                parts.append(d.get("text") or d.get("summary") or "")
        return "\n".join(p for p in parts if p)
    return ""


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
        # Force a tool call so the model can't reply in free text (no prose leaks).
        "tool_choice": "required",
    }
    effort = getattr(settings, "reasoning_effort", "off")
    if effort and effort != "off":
        # OpenAI-style reasoning control; models that don't support it ignore it.
        payload["reasoning_effort"] = effort
    resp = _http_post_json(url, headers, payload)
    try:
        message = resp["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"Unexpected response shape: {resp!r}") from exc
    reasoning = _openai_reasoning(message)
    content = message.get("content") or ""
    for call in (message.get("tool_calls") or []):
        fn = call.get("function") or {}
        name = fn.get("name")
        args = json.loads(fn.get("arguments") or "{}")
        if name == _FINISH_NAME:
            _debug("openai: finish")
            return LLMProposal(intent="", script="",
                               text=args.get("summary", "") or content,
                               is_tool_call=False, reasoning=reasoning,
                               kind="finish")
        if name == _TOOL_NAME:
            _debug("openai: run_freecad_script")
            return LLMProposal(intent=args.get("intent", ""),
                               script=args.get("script", ""),
                               text=content,
                               is_tool_call=True, reasoning=reasoning,
                               kind="script")
    # No recognized tool call (a model that ignored tool_choice): treat any text
    # as a finish so the turn still resolves rather than looping.
    _debug("openai: no tool call; text=%r" % (content[:120]))
    return LLMProposal(intent="", script="", text=content,
                       is_tool_call=False, reasoning=reasoning, kind="finish")


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
    effort = getattr(settings, "reasoning_effort", "off")
    thinking_on = bool(effort and effort != "off")
    if thinking_on:
        payload["thinking"] = {"type": "adaptive", "display": "summarized"}
        payload["output_config"] = {"effort": effort}
        # Anthropic rejects forced tool_choice together with thinking; thinking
        # models follow the two-tool contract from the prompt anyway.
    else:
        payload["tool_choice"] = {"type": "any"}  # force one of the two tools
    resp = _http_post_json(url, headers, payload)
    content = resp.get("content")
    if not isinstance(content, list):
        raise LLMError(f"Unexpected response shape: {resp!r}")
    text_parts = []
    thinking_parts = []
    script_block = None
    finish_block = None
    for block in content:
        btype = block.get("type")
        if btype == "tool_use" and block.get("name") == _TOOL_NAME:
            script_block = block
        elif btype == "tool_use" and block.get("name") == _FINISH_NAME:
            finish_block = block
        elif btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "thinking":
            thinking_parts.append(block.get("thinking", "") or "")
    reasoning = "\n".join(t for t in thinking_parts if t)
    if script_block is not None:
        args = script_block.get("input") or {}
        return LLMProposal(intent=args.get("intent", ""),
                           script=args.get("script", ""),
                           text="".join(text_parts),
                           is_tool_call=True, reasoning=reasoning, kind="script")
    if finish_block is not None:
        args = finish_block.get("input") or {}
        return LLMProposal(intent="", script="",
                           text=args.get("summary", "") or "".join(text_parts),
                           is_tool_call=False, reasoning=reasoning, kind="finish")
    # Thinking-on path may return plain text; treat it as a finish.
    return LLMProposal(intent="", script="",
                       text="".join(text_parts),
                       is_tool_call=False, reasoning=reasoning, kind="finish")


def complete(messages: list, settings: Settings) -> "LLMProposal":
    provider, wire_model = _split_model(settings.model)
    if provider == "anthropic":
        return _complete_anthropic(wire_model, messages, settings)
    return _complete_openai(wire_model, provider, messages, settings)
