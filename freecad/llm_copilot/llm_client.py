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
import copy
import socket
import urllib.request
import urllib.error
from dataclasses import dataclass

from .settings import Settings

SYSTEM_PROMPT = (
    "You are a CAD copilot operating inside FreeCAD. You receive a text snapshot "
    "of the active document before each turn.\n"
    "\n"
    "You have five tools and MUST call one on every turn (you cannot reply "
    "with free text):\n"
    "- run_freecad_script(intent, script): do work. `intent` is one plain-language "
    "sentence a non-programmer will read; `script` is FreeCAD Python run against "
    "App.ActiveDocument. ALL code — including diagnostics — goes here.\n"
    "- inspect_document(query): read detailed document state without changing it.\n"
    "- ask_user(question, options): pause and let the user choose one or more "
    "structured options when a consequential ambiguity cannot be resolved from "
    "the document. Do not use it for choices you can safely make yourself.\n"
    "- lookup_freecad_api(query, module, symbol): inspect the installed FreeCAD "
    "version and bundled API field guide. Use it instead of guessing classes, "
    "methods, properties, enums, or attachment workflows.\n"
    "- finish(summary, verified, evidence): call this ONLY when the whole task is complete. "
    "the user a question. `summary` is the message shown to the user.\n"
    "\n"
    "If a previous script errored, FIX IT and call run_freecad_script again — do "
    "NOT call finish to describe the fix. Keep calling run_freecad_script until the "
    "geometry is actually done, then call finish once.\n"
    "\n"
    "To inspect the model, call run_freecad_script with a script that uses print(); its "
    "stdout is returned to you as [script output] on the next turn, so you can "
    "check values (vertices, isClosed, validity) and then act on what you see. "
    "Python stderr and execution-scoped FreeCAD console warnings/errors are "
    "also returned; investigate console errors even if execution otherwise "
    "reports success. "
    "Prefer inspect_document for read-only checks. Diagnostic scripts that need "
    "custom FreeCAD calculations go through the execution tool too.\n"
    "\n"
    "FreeCAD API rules (follow exactly):\n"
    "- The host requires an active document before starting a conversation. "
    "Never create a document merely because App.ActiveDocument is missing.\n"
    "- If a document is already active, use App.ActiveDocument. Do NOT create "
    "another document unless the user explicitly asks for a separate document.\n"
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
    "- Never delete or modify Origin, origin planes, or origin axes to resolve "
    "an inspection or validation problem; they are structural FreeCAD objects.\n"
    "- Do not call recompute for transaction/undo purposes or manage "
    "transactions; the host wraps each script in one undoable transaction. You "
    "MAY call doc.recompute() when a feature needs recomputing to proceed."
)

_WORKFLOW_PROMPT = (
    "\n\nCAD design workflow:\n"
    "- Work like a careful human CAD designer: analyze references, make a "
    "feature-level plan, build stable sketches/base forms, add material, remove "
    "material, apply finishing features last, then measure and review.\n"
    "- Choose the appropriate strategy. Use Part Design for sketch-driven "
    "parametric components, Part primitives/booleans for genuinely simple "
    "construction, and preserve an existing feature tree when modifying it.\n"
    "- Give meaningful names to bodies, sketches, parameters, and features. "
    "Prefer datum/origin references over fragile generated-face references.\n"
)

_PARAMETRIC_PROMPT = (
    "- Prefer editable dimensions, constraints, expressions, sketches, and "
    "native features over replacing the result with an opaque Part::Feature. "
    "Do not force a sketch when a primitive or repair is the better strategy.\n")

_SKETCH_PROMPT = (
    "- After editing a sketch, inspect its support, solver state, and remaining "
    "degrees of freedom. Fully constrain intentional design geometry whenever "
    "practical; explain any deliberately free construction geometry.\n")

_STAGE_PROMPT = (
    "- Normally progress analyze → sketch → additive → subtractive → finish → "
    "verify; skip stages that do not apply, and return to an earlier stage when "
    "a correction requires it. Fillets/chamfers generally come last.\n")

_ASSUMPTION_PROMPT = (
    "- On the first script call, list every numeric value not supplied by the "
    "user in assumptions. Use stable ids, numeric values and units; sort rows "
    "by consequence high to low. Low-confidence, high-consequence assumptions "
    "must be resolved with ask_user before execution. Use at most three "
    "single-question calls with numeric choices in option labels, then resubmit "
    "the script with the same ids and user_confirmed status/evidence.\n")

_FIDELITY_MEANINGS = {
    "replica": "reproduce the reference faithfully",
    "stylised": (
        "keep the gesture, discard surface detail, and ignore extra reference "
        "detail"),
    "functional_analogue": "match function rather than appearance",
}

_INFERRED_PROMPT = (
    "- Build features required by the description but not visible in the "
    "reference, and name or comment every such feature INFERRED. Never invent "
    "a feature silently.\n")

_ASSUMPTION_ITEM = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "value": {"type": "number"},
        "unit": {"type": "string"},
        "source": {"type": "string"},
        "confidence": {"type": "string",
                       "enum": ["high", "medium", "low"]},
        "consequence": {"type": "string",
                        "enum": ["high", "medium", "low"]},
        "if_wrong": {"type": "string"},
        "status": {"type": "string",
                   "enum": ["unverified", "user_confirmed", "measured"]},
        "evidence": {"type": "string"},
    },
    "required": [
        "id", "name", "value", "unit", "source", "confidence",
        "consequence", "if_wrong", "status", "evidence"],
}

# The model always calls exactly one available tool (tool_choice is forced), so it
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
        "strategy": {
            "type": "string",
            "enum": ["part_design", "part", "surface", "modify_existing",
                     "inspection"],
            "description": "The CAD construction strategy chosen for this task."},
        "stage": {
            "type": "string",
            "enum": ["analyze", "sketch", "additive", "subtractive",
                     "finish", "verify"],
            "description": "The current stage of the design workflow."},
        "plan": {
            "type": "array", "items": {"type": "string"},
            "description": "Short ordered feature-level plan; repeat it when it changes."},
        "plan_step": {
            "type": "integer", "minimum": 1,
            "description": "One-based plan step this script advances."},
        "success_criteria": {
            "type": "array", "items": {"type": "string"},
            "description": "Measurable checks that define a correct result."},
    },
    "required": ["intent", "script"],
}

_FINISH_NAME = "finish"
_FINISH_DESCRIPTION = (
    "Call this ONLY when the whole task is complete. Use ask_user for a "
    "clarifying question. Include verification evidence after making changes. "
    "Its `summary` "
    "is shown to the user as your reply. Do NOT put any "
    "code here — all code goes to run_freecad_script.")
_FINISH_PARAMETERS = {
    "type": "object",
    "properties": {
        "summary": {"type": "string",
                    "description": "Plain-language message to the user."},
        "verified": {"type": "boolean",
                     "description": "True only after reviewing post-change evidence."},
        "evidence": {"type": "array", "items": {"type": "string"},
                     "description": "Concrete checks supporting completion."},
        "reviewed_plan": {
            "type": "boolean",
            "description": "True only after comparing the result with every plan step and success criterion."},
    },
    "required": ["summary", "verified", "evidence"],
}

_INSPECT_NAME = "inspect_document"
_INSPECT_DESCRIPTION = "Read detailed FreeCAD state without modifying the document."
_INSPECT_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {"type": "string",
                  "description": "What properties, geometry, or relationships to inspect."},
    },
    "required": ["query"],
}

_QUESTION_NAME = "ask_user"
_QUESTION_DESCRIPTION = (
    "Ask a consequential multiple-choice question and wait for the user's "
    "selection before continuing the same task. Provide 2–5 concise options.")
_QUESTION_PARAMETERS = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "A concise question explaining the decision needed."},
        "options": {
            "type": "array", "minItems": 2, "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string",
                           "description": "Short stable option identifier."},
                    "label": {"type": "string",
                              "description": "Short button label."},
                    "description": {
                        "type": "string",
                        "description": "One sentence explaining impact or tradeoff."},
                },
                "required": ["id", "label", "description"],
            },
        },
        "recommended_option": {
            "type": "string",
            "description": "The id of the recommended option, or empty string."},
        "allow_multiple": {
            "type": "boolean",
            "description": "Whether the user may select more than one option."},
    },
    "required": [
        "question", "options", "recommended_option", "allow_multiple"],
}

_API_NAME = "lookup_freecad_api"
_API_DESCRIPTION = (
    "Look up a FreeCAD API symbol using safe runtime introspection from the "
    "installed version plus a compact bundled field guide.")
_API_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "What API behavior or workflow needs clarification."},
        "module": {
            "type": "string",
            "enum": ["FreeCAD", "Part", "PartDesign", "Sketcher"],
            "description": "Installed module to inspect."},
        "symbol": {
            "type": "string",
            "description": "Optional public dotted symbol path within the module."},
    },
    "required": ["query", "module", "symbol"],
}

# OpenAI-format tool schema.
TOOL_SCHEMA = [
    {"type": "function", "function": {
        "name": _TOOL_NAME, "description": _TOOL_DESCRIPTION,
        "parameters": _TOOL_PARAMETERS}},
    {"type": "function", "function": {
        "name": _FINISH_NAME, "description": _FINISH_DESCRIPTION,
        "parameters": _FINISH_PARAMETERS}},
    {"type": "function", "function": {
        "name": _INSPECT_NAME, "description": _INSPECT_DESCRIPTION,
        "parameters": _INSPECT_PARAMETERS}},
    {"type": "function", "function": {
        "name": _QUESTION_NAME, "description": _QUESTION_DESCRIPTION,
        "parameters": _QUESTION_PARAMETERS}},
    {"type": "function", "function": {
        "name": _API_NAME, "description": _API_DESCRIPTION,
        "parameters": _API_PARAMETERS}},
]

# Anthropic-format tool schema (input_schema instead of parameters).
ANTHROPIC_TOOL_SCHEMA = [
    {"name": _TOOL_NAME, "description": _TOOL_DESCRIPTION,
     "input_schema": _TOOL_PARAMETERS},
    {"name": _FINISH_NAME, "description": _FINISH_DESCRIPTION,
     "input_schema": _FINISH_PARAMETERS},
    {"name": _INSPECT_NAME, "description": _INSPECT_DESCRIPTION,
     "input_schema": _INSPECT_PARAMETERS},
    {"name": _QUESTION_NAME, "description": _QUESTION_DESCRIPTION,
     "input_schema": _QUESTION_PARAMETERS},
    {"name": _API_NAME, "description": _API_DESCRIPTION,
     "input_schema": _API_PARAMETERS},
]


def _system_prompt(settings):
    tool_count = (
        5 - int(not settings.freecad_api_lookup)
        - int(not settings.read_only_inspection))
    count_word = {3: "three", 4: "four", 5: "five"}[tool_count]
    prompt = SYSTEM_PROMPT.replace("five tools", count_word + " tools")
    if not settings.freecad_api_lookup:
        prompt = prompt.replace(
            "- lookup_freecad_api(query, module, symbol): inspect the installed "
            "FreeCAD version and bundled API field guide. Use it instead of "
            "guessing classes, methods, properties, enums, or attachment "
            "workflows.\n", "")
    if settings.structured_cad_planning:
        prompt += _WORKFLOW_PROMPT
    if settings.parametric_feature_preference:
        prompt += _PARAMETRIC_PROMPT
    if settings.sketch_constraint_verification:
        prompt += _SKETCH_PROMPT
    if settings.stage_order_guidance:
        prompt += _STAGE_PROMPT
    if settings.assumption_ledger:
        prompt += _ASSUMPTION_PROMPT
    if settings.fidelity_target != "unspecified":
        meaning = _FIDELITY_MEANINGS.get(settings.fidelity_target)
        if meaning:
            prompt += (
                f"- Fidelity target: {settings.fidelity_target}; {meaning}.\n")
    if settings.mark_inferred_features:
        prompt += _INFERRED_PROMPT
    if settings.final_design_review:
        prompt += (
            "- Before finish, compare the finished feature tree, measurements, "
            "validation, and rendered evidence with the plan and success criteria, "
            "then set reviewed_plan=true and cite concrete evidence.\n")
    if not settings.read_only_inspection:
        prompt = prompt.replace(
            "- inspect_document(query): read detailed document state without changing it.\n",
            "")
        prompt = prompt.replace(
            "Prefer inspect_document for read-only checks. ", "")
    if not settings.mandatory_verification:
        prompt = prompt.replace(", verified, evidence", "")
    return prompt


def system_prompt(settings):
    """Return the exact system message used for the configured feature flags."""
    return _system_prompt(settings)


def _openai_tools(settings):
    tools = copy.deepcopy(TOOL_SCHEMA)
    if not settings.freecad_api_lookup:
        tools = [t for t in tools if t["function"]["name"] != _API_NAME]
    if not settings.read_only_inspection:
        tools = [t for t in tools if t["function"]["name"] != _INSPECT_NAME]
    if not settings.mandatory_verification:
        finish = next(t["function"] for t in tools
                      if t["function"]["name"] == _FINISH_NAME)
        finish["parameters"]["properties"].pop("verified", None)
        finish["parameters"]["properties"].pop("evidence", None)
        finish["parameters"]["required"] = ["summary"]
    if settings.structured_cad_planning:
        script = next(t["function"] for t in tools
                      if t["function"]["name"] == _TOOL_NAME)
        script["parameters"]["required"] += [
            "strategy", "stage", "plan", "plan_step", "success_criteria"]
    if settings.assumption_ledger:
        script = next(t["function"] for t in tools
                      if t["function"]["name"] == _TOOL_NAME)
        script["parameters"]["properties"]["assumptions"] = {
            "type": "array", "items": copy.deepcopy(_ASSUMPTION_ITEM)}
        script["parameters"]["required"].append("assumptions")
    if settings.final_design_review:
        script = next(t["function"] for t in tools
                      if t["function"]["name"] == _TOOL_NAME)
        if "stage" not in script["parameters"]["required"]:
            script["parameters"]["required"].append("stage")
        finish = next(t["function"] for t in tools
                      if t["function"]["name"] == _FINISH_NAME)
        finish["parameters"]["required"].append("reviewed_plan")
    return tools


def _anthropic_tools(settings):
    openai_tools = _openai_tools(settings)
    return [{
        "name": t["function"]["name"],
        "description": t["function"]["description"],
        "input_schema": t["function"]["parameters"],
    } for t in openai_tools]

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
COMPLETION_TIMEOUT = 300


@dataclass
class LLMProposal:
    intent: str
    script: str
    text: str
    is_tool_call: bool          # True for a run_freecad_script call
    reasoning: str = ""         # model's thinking, if the provider exposed it
    kind: str = "script"        # "script" (run_freecad_script) | "finish"
    verified: bool = False
    evidence: tuple = ()
    query: str = ""
    strategy: str = ""
    stage: str = ""
    plan: tuple = ()
    plan_step: int = 0
    success_criteria: tuple = ()
    assumptions: object = None
    reviewed_plan: bool = False
    question: str = ""
    options: tuple = ()
    recommended_option: str = ""
    allow_multiple: bool = False
    api_query: str = ""
    api_module: str = ""
    api_symbol: str = ""

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


class LLMTimeoutError(LLMError):
    """Raised when a completion exceeds the provider request timeout."""


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
        raise LLMTimeoutError(
            f"Timed out after {timeout}s contacting {url}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, socket.timeout):
            raise LLMTimeoutError(
                f"Timed out after {timeout}s contacting {url}") from exc
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
    reasoning = message.get("reasoning") or message.get("reasoning_content")
    if isinstance(reasoning, str):
        return reasoning
    # OpenRouter can return structured reasoning_details: [{text/summary}, ...]
    details = message.get("reasoning_details")
    if isinstance(details, list):
        parts = [
            detail.get("text") or detail.get("summary") or ""
            for detail in details if isinstance(detail, dict)]
        return "\n".join(part for part in parts if part)
    return ""


def _question_proposal(args, reasoning=""):
    options = []
    for option in (args.get("options") or [])[:5]:
        if not isinstance(option, dict):
            continue
        option_id = str(option.get("id", "")).strip()
        label = str(option.get("label", "")).strip()
        if option_id and label:
            options.append({
                "id": option_id,
                "label": label,
                "description": str(option.get("description", "")).strip(),
            })
    return LLMProposal(
        "", "", "", False, reasoning=reasoning, kind="question",
        question=str(args.get("question", "")).strip(),
        options=tuple(options),
        recommended_option=str(
            args.get("recommended_option", "")).strip(),
        allow_multiple=bool(args.get("allow_multiple")))


def _proposal_from_tool(name, args, reasoning="", text=""):
    """Build the LLMProposal for one tool call, provider-independent.

    Both the OpenAI-compatible and Anthropic adapters reduce their wire format
    to a ``(name, args)`` pair and hand off here, so the five proposal shapes
    are constructed in exactly one place. Returns ``None`` for an unrecognized
    tool name; the caller decides how to handle that (treat text as a finish).
    """
    args = args or {}
    if name == _TOOL_NAME:
        return LLMProposal(
            intent=args.get("intent", ""), script=args.get("script", ""),
            text=text, is_tool_call=True, reasoning=reasoning, kind="script",
            strategy=args.get("strategy", ""), stage=args.get("stage", ""),
            plan=tuple(args.get("plan") or ()),
            plan_step=int(args.get("plan_step") or 0),
            success_criteria=tuple(args.get("success_criteria") or ()),
            assumptions=tuple(
                dict(row) for row in (args.get("assumptions") or ())
                if isinstance(row, dict)))
    if name == _FINISH_NAME:
        return LLMProposal(
            intent="", script="", text=args.get("summary", "") or text,
            is_tool_call=False, reasoning=reasoning, kind="finish",
            verified=bool(args.get("verified")),
            evidence=tuple(args.get("evidence") or ()),
            reviewed_plan=bool(args.get("reviewed_plan")))
    if name == _INSPECT_NAME:
        return LLMProposal(
            "", "", "", False, reasoning=reasoning, kind="inspect",
            query=args.get("query", ""))
    if name == _QUESTION_NAME:
        return _question_proposal(args, reasoning)
    if name == _API_NAME:
        return LLMProposal(
            "", "", "", False, reasoning=reasoning, kind="api_lookup",
            api_query=str(args.get("query", "")),
            api_module=str(args.get("module", "FreeCAD")),
            api_symbol=str(args.get("symbol", "")))
    return None


def _complete_openai(wire_model: str, provider: str, messages: list,
                     settings: Settings) -> "LLMProposal":
    url = _base_url(settings, provider) + "/chat/completions"
    headers = {}
    if settings.api_key:
        headers["Authorization"] = "Bearer " + settings.api_key
    payload = {
        "model": wire_model,
        "messages": [{"role": "system", "content": _system_prompt(settings)}] + messages,
        "tools": _openai_tools(settings),
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
        proposal = _proposal_from_tool(name, args, reasoning, content)
        if proposal is not None:
            _debug("openai: " + name)
            return proposal
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
    anthropic_messages = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            blocks = []
            for block in content:
                if block.get("type") == "image_url":
                    data_url = (block.get("image_url") or {}).get("url", "")
                    prefix = "data:image/png;base64,"
                    if data_url.startswith(prefix):
                        blocks.append({
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/png",
                                       "data": data_url[len(prefix):]},
                        })
                else:
                    blocks.append(block)
            anthropic_messages.append(
                {"role": message["role"], "content": blocks})
        else:
            anthropic_messages.append(message)
    payload = {
        "model": wire_model,
        "max_tokens": DEFAULT_MAX_TOKENS,
        "system": _system_prompt(settings),  # system is top-level, not a message
        "messages": anthropic_messages,
        "tools": _anthropic_tools(settings),
    }
    effort = getattr(settings, "reasoning_effort", "off")
    thinking_on = bool(effort and effort != "off")
    if thinking_on:
        payload["thinking"] = {"type": "adaptive", "display": "summarized"}
        payload["output_config"] = {"effort": effort}
        # Anthropic rejects forced tool_choice together with thinking; thinking
        # models follow the two-tool contract from the prompt anyway.
    else:
        payload["tool_choice"] = {"type": "any"}  # force one available tool
    resp = _http_post_json(url, headers, payload)
    content = resp.get("content")
    if not isinstance(content, list):
        raise LLMError(f"Unexpected response shape: {resp!r}")
    text_parts = []
    thinking_parts = []
    tool_blocks = {}
    for block in content:
        btype = block.get("type")
        if btype == "tool_use":
            tool_blocks[block.get("name")] = block
        elif btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "thinking":
            thinking_parts.append(block.get("thinking", "") or "")
    reasoning = "\n".join(t for t in thinking_parts if t)
    text = "".join(text_parts)
    # Prefer a work/finish tool over the read-only tools if several appear.
    for name in (_TOOL_NAME, _FINISH_NAME, _INSPECT_NAME, _QUESTION_NAME,
                 _API_NAME):
        if name in tool_blocks:
            return _proposal_from_tool(
                name, tool_blocks[name].get("input"), reasoning, text)
    # Thinking-on path may return plain text; treat it as a finish.
    return LLMProposal(intent="", script="", text=text,
                       is_tool_call=False, reasoning=reasoning, kind="finish")


def complete(messages: list, settings: Settings) -> "LLMProposal":
    provider, wire_model = _split_model(settings.model)
    if provider == "anthropic":
        return _complete_anthropic(wire_model, messages, settings)
    return _complete_openai(wire_model, provider, messages, settings)
