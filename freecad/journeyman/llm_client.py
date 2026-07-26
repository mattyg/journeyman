"""Zero-dependency LLM client for the FreeCAD journeyman.

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
import random
import socket
import time
import urllib.request
import urllib.error
from dataclasses import dataclass

from .config import Settings

# Spliced into SYSTEM_PROMPT and removed again when on_demand_render is off.
# Named so the two uses cannot drift apart the way a duplicated literal would.
_RENDER_PROMPT_LINE = (
    "- render_views(objects): see the geometry. Returns offscreen orthographic "
    "and isometric views — pass object names to isolate them, or omit for the "
    "whole document. Use it when orientation, placement, seating, or which "
    "side faces where matters, BEFORE you write the script that depends on "
    "it. Views are dropped once a script changes the document, so render "
    "again after an edit if you need to look.\n")

SYSTEM_PROMPT = (
    "You are a CAD journeyman operating inside FreeCAD. You receive a text snapshot "
    "of the active document before each turn.\n"
    "\n"
    "You have six tools and MUST call one on every turn (you cannot reply "
    "with free text):\n"
    "- run_freecad_script(intent, script): do work. `intent` is one plain-language "
    "sentence a non-programmer will read; `script` is FreeCAD Python run against "
    "App.ActiveDocument. ALL code — including diagnostics — goes here.\n"
    "- inspect_document(query): read detailed document state without changing it.\n"
    + _RENDER_PROMPT_LINE +
    "- ask_user(question, options): pause and let the user choose one or more "
    "structured options when a consequential ambiguity cannot be resolved from "
    "the document. Do not use it for choices you can safely make yourself.\n"
    "- lookup_freecad_api(query, module, symbol): inspect the installed FreeCAD "
    "version and bundled API field guide. Use it instead of guessing classes, "
    "methods, properties, enums, or attachment workflows.\n"
    "- finish(summary, verified, evidence): call this ONLY when the whole task "
    "is complete — never to describe work you still intend to do, and never to "
    "ask the user a question. `summary` is the message shown to the user.\n"
    "\n"
    "If a previous script errored, FIX IT and call run_freecad_script again — do "
    "NOT call finish to describe the fix. Keep calling run_freecad_script until the "
    "geometry is actually done, then call finish once.\n"
    "\n"
    "Prefer inspect_document for read-only checks. For custom calculations, "
    "call run_freecad_script with print(); its stdout returns as [script "
    "output] next turn, so you can check values (vertices, isClosed, validity) "
    "and act on them. stderr and FreeCAD console warnings/errors return too — "
    "investigate console errors even when execution reports success.\n"
    "\n"
    "FreeCAD API rules (follow exactly):\n"
    "- Always use App.ActiveDocument. The host guarantees one exists; never "
    "create a document, even if App.ActiveDocument looks missing, unless the "
    "user explicitly asks for a separate one.\n"
    "- `doc` has NO `XY_Plane`, `Body`, or origin attributes. Those do not exist "
    "on the document.\n"
    "- Build all geometry with native Part Design. Create a Body first: body = "
    "doc.addObject('PartDesign::Body','Body'); doc.recompute(). Origin planes "
    "are body.Origin.OriginFeatures (or use the real Name from the snapshot). "
    "Sketches attach via sketch.AttachmentSupport = [(plane, '')]; "
    "sketch.MapMode = 'FlatFace'. Constrain them, then use native features "
    "(Pad, Pocket, Hole, Revolution, AdditivePipe). Do not use Part "
    "primitives, Part::Feature, Shape assignment, or Part booleans.\n"
    "- Refer to existing objects by the exact Name shown in the snapshot; do not "
    "guess attribute names. Prefer editing existing objects over rebuilding.\n"
    "- Never delete or modify Origin, origin planes, or origin axes to resolve "
    "an inspection or validation problem; they are structural FreeCAD objects.\n"
    "- Do not call recompute for transaction/undo purposes or manage "
    "transactions; the host wraps each script in one undoable transaction. You "
    "MAY call doc.recompute() when a feature needs recomputing to proceed.\n"
    "- Two helpers are preloaded; prefer them over hand-rolled checks, which "
    "raise opaque OCC errors or pass silently on a null shape. "
    "assert_feature(obj, solids=None) raises a specific error unless obj built "
    "real geometry; assert_sketch_constrained(sk) raises unless a sketch is "
    "closed and fully constrained. Call them after creating a feature, so a "
    "failure names the step that caused it.\n"
    "- When a script raises part-way through, the work it completed first is "
    "kept, not undone. Check the document snapshot before repeating earlier "
    "steps: objects you already deleted are still gone, and features you "
    "already built are still there."
)

_WORKFLOW_PROMPT = (
    "\n\nCAD design workflow:\n"
    "- Work like a careful human CAD designer: analyze references, make a "
    "feature-level plan, build stable sketches/base forms, add material, remove "
    "material, apply finishing features last, then measure and review.\n"
    "- Preserve an existing native feature tree when modifying it.\n"
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
    "user in assumptions. Use stable ids, numeric values and units; row order "
    "does not matter (the host sorts most-severe first). Low-confidence, "
    "high-consequence assumptions "
    "must be resolved with ask_user before execution. Use at most three "
    "single-question calls with numeric choices in option labels, then resubmit "
    "the script with the same ids and user_confirmed status/evidence.\n"
    "- Carry the ledger forward on every later call: ids are permanent, and a "
    "row must never be dropped. You may reword a row's name freely as your "
    "understanding sharpens. Changing a row's value or status needs evidence, "
    "and rewording a row while changing its value or status reads as "
    "redefining it — keep those separate.\n")

_FIDELITY_MEANINGS = {
    "replica": "reproduce the reference faithfully",
    "stylised": (
        "keep the gesture, discard surface detail, and ignore extra reference "
        "detail"),
    "functional_analogue": "match function rather than appearance",
}

_FIDELITY_FEATURE_ITEM = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "description": {"type": "string"},
        "status": {
            "type": "string",
            "enum": [
                "planned", "implemented", "user_approved_omission", "blocked"]},
        "evidence": {"type": "string"},
    },
    "required": ["id", "description", "status", "evidence"],
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
            "enum": ["part_design"],
            "description": "The required native Part Design construction strategy."},
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
    "required": ["intent", "script", "strategy"],
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

_RENDER_NAME = "render_views"
_RENDER_DESCRIPTION = (
    "Render offscreen orthographic and isometric views of the document or of "
    "named objects, without changing the visible camera or the document.")
_RENDER_PARAMETERS = {
    "type": "object",
    "properties": {
        "objects": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Internal object names to render in isolation (for example "
                "['Body', 'Pad001']). Empty renders the whole document."),
        },
    },
    # Required-but-emptyable, matching _API_PARAMETERS' treatment of `symbol`:
    # providers that enforce strict schemas reject optional properties, and an
    # empty list already means "whole document".
    "required": ["objects"],
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
        "name": _RENDER_NAME, "description": _RENDER_DESCRIPTION,
        "parameters": _RENDER_PARAMETERS}},
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
    {"name": _RENDER_NAME, "description": _RENDER_DESCRIPTION,
     "input_schema": _RENDER_PARAMETERS},
    {"name": _QUESTION_NAME, "description": _QUESTION_DESCRIPTION,
     "input_schema": _QUESTION_PARAMETERS},
    {"name": _API_NAME, "description": _API_DESCRIPTION,
     "input_schema": _API_PARAMETERS},
]


def _system_prompt(settings):
    tool_count = (
        6 - int(not settings.freecad_api_lookup)
        - int(not settings.read_only_inspection)
        - int(not settings.on_demand_render))
    count_word = {
        3: "three", 4: "four", 5: "five", 6: "six"}[tool_count]
    prompt = SYSTEM_PROMPT.replace("six tools", count_word + " tools")
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
    if settings.fidelity_target == "replica":
        prompt += (
            "- Replica fidelity is enforced. Enumerate every visible or "
            "described feature in observed_features and keep stable ids on "
            "every script call. Implementation difficulty is not permission to "
            "flatten, straighten, merge, approximate, or omit a feature. Change "
            "construction strategy instead. If omission is genuinely necessary, "
            "ask_user first and record user_approved_omission with evidence. "
            "Do not finish while a feature is planned or blocked.\n")
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
    if not settings.on_demand_render:
        prompt = prompt.replace(_RENDER_PROMPT_LINE, "")
    if not settings.mandatory_verification:
        prompt = prompt.replace(", verified, evidence", "")
    return prompt


def system_prompt(settings):
    """Return the exact system message used for the configured feature flags."""
    return _system_prompt(settings)


def _openai_tools(settings):
    tools = [copy.deepcopy(spec.schema)
             for spec in TOOL_SPECS if spec.enabled(settings)]
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
    if settings.fidelity_target == "replica":
        script = next(t["function"] for t in tools
                      if t["function"]["name"] == _TOOL_NAME)
        script["parameters"]["properties"]["observed_features"] = {
            "type": "array", "minItems": 1,
            "items": copy.deepcopy(_FIDELITY_FEATURE_ITEM)}
        script["parameters"]["required"].append("observed_features")
        finish = next(t["function"] for t in tools
                      if t["function"]["name"] == _FINISH_NAME)
        finish["parameters"]["properties"]["fidelity_met"] = {
            "type": "boolean",
            "description": "True only when every tracked replica feature is resolved."}
        finish["parameters"]["properties"]["fidelity_omissions"] = {
            "type": "array", "items": {"type": "string"},
            "description": "Stable ids of user-approved omissions; empty otherwise."}
        finish["parameters"]["required"] += [
            "fidelity_met", "fidelity_omissions"]
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

# Transient HTTP failures worth retrying: rate limiting, and the gateway/
# overload family providers return under load. Anything else (401, 400) is a
# configuration or payload problem that retrying cannot fix.
RETRYABLE_STATUS = frozenset((429, 500, 502, 503, 504))
MAX_HTTP_ATTEMPTS = 3
# Bound on any single backoff wait, and on the total spent waiting across
# attempts. A long CAD run makes many sequential calls, so a rate limit must
# not turn into an unbounded stall.
MAX_RETRY_WAIT = 30.0
MAX_TOTAL_RETRY_WAIT = 60.0


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
    strategy: str = "part_design"
    stage: str = ""
    plan: tuple = ()
    plan_step: int = 0
    success_criteria: tuple = ()
    assumptions: object = None
    observed_features: object = None
    fidelity_met: bool = False
    fidelity_omissions: tuple = ()
    reviewed_plan: bool = False
    question: str = ""
    options: tuple = ()
    recommended_option: str = ""
    allow_multiple: bool = False
    api_query: str = ""
    api_module: str = ""
    api_symbol: str = ""
    render_objects: tuple = ()

    @property
    def is_finish(self) -> bool:
        return self.kind == "finish"


class ScriptProposal(LLMProposal):
    """A document-changing or diagnostic Python tool request."""


class FinishProposal(LLMProposal):
    """A request to finish the current agent turn."""


class InspectionProposal(LLMProposal):
    """A structured document inspection request."""


class RenderProposal(LLMProposal):
    """An offscreen document render request."""


class QuestionProposal(LLMProposal):
    """A structured clarification request."""


class ApiLookupProposal(LLMProposal):
    """An installed-FreeCAD API lookup request."""


@dataclass(frozen=True)
class ToolSpec:
    """One source of truth for schema, result type, and feature flag."""

    name: str
    schema: dict
    proposal_type: type
    setting: str = ""

    def enabled(self, settings):
        return not self.setting or bool(getattr(settings, self.setting))


_PROPOSAL_TYPES = {
    _TOOL_NAME: ScriptProposal,
    _FINISH_NAME: FinishProposal,
    _INSPECT_NAME: InspectionProposal,
    _RENDER_NAME: RenderProposal,
    _QUESTION_NAME: QuestionProposal,
    _API_NAME: ApiLookupProposal,
}

_TOOL_SETTINGS = {
    _INSPECT_NAME: "read_only_inspection",
    _RENDER_NAME: "on_demand_render",
    _API_NAME: "freecad_api_lookup",
}

TOOL_SPECS = tuple(
    ToolSpec(tool["function"]["name"], tool,
             _PROPOSAL_TYPES[tool["function"]["name"]],
             _TOOL_SETTINGS.get(tool["function"]["name"], ""))
    for tool in TOOL_SCHEMA)

TOOL_SPEC_BY_NAME = {spec.name: spec for spec in TOOL_SPECS}


# Which providers expose a reasoning-effort knob (drives the Preferences UI).
PROVIDERS_WITH_REASONING = ("anthropic", "openai", "openrouter")

# Abstract effort levels stored in settings; translated per provider at request
# time. "off" means don't request reasoning at all.
REASONING_LEVELS = ("off", "low", "medium", "high")


class LLMError(Exception):
    """Raised when the provider request fails or returns an unusable response."""


class LLMTimeoutError(LLMError):
    """Raised when a completion exceeds the provider request timeout."""


class LLMRateLimitError(LLMError):
    """Raised when the provider rate limited or was overloaded.

    Distinguished from a generic :class:`LLMError` so the agent can offer the
    same "retry?" path it offers on a timeout: waiting fixes this, whereas a
    bad key or a malformed payload never will.
    """

    def __init__(self, message, status=429, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


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


def _retry_after_seconds(exc):
    """Seconds the provider asked us to wait, if it said so.

    ``Retry-After`` is either a delay in seconds or an HTTP date; only the
    numeric form is honoured, since the date form needs clock agreement we
    cannot assume. Returns ``None`` when absent or unparsable.
    """
    headers = getattr(exc, "headers", None)
    raw = headers.get("Retry-After") if headers is not None else None
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return max(0.0, value) if value == value else None  # reject NaN


def _backoff_delay(attempt, retry_after=None):
    """How long to wait before ``attempt`` (1-based), clamped to a sane bound."""
    if retry_after is not None:
        return min(retry_after, MAX_RETRY_WAIT)
    # Exponential with jitter, so several documents retrying do not synchronise.
    base = min(2.0 ** (attempt - 1), MAX_RETRY_WAIT)
    return min(base + random.uniform(0, 0.5 * base), MAX_RETRY_WAIT)


def _sleep(seconds, cancel_event=None):
    """Wait, but stay responsive to cancellation.

    A urllib request already cannot be killed from another thread; sleeping
    through a backoff would extend that window, so the wait is sliced and
    checked. Returns False when cancelled mid-wait.
    """
    if cancel_event is not None and hasattr(cancel_event, "wait"):
        return not cancel_event.wait(seconds)
    remaining = seconds
    while remaining > 0:
        if cancel_event is not None and cancel_event.is_set():
            return False
        slice_ = min(0.25, remaining)
        time.sleep(slice_)
        remaining -= slice_
    return True


def _http_post_json(url: str, headers: dict, payload: dict,
                    timeout: float = COMPLETION_TIMEOUT,
                    cancel_event=None) -> dict:
    """POST a JSON body and parse a JSON response, using only the stdlib.

    Retries transient failures (rate limiting, gateway/overload errors) with
    backoff, honouring ``Retry-After`` when the provider sends it. A long CAD
    run makes many sequential calls, so a single 429 must not end the turn.

    Isolated so tests can monkeypatch a single seam instead of the network.
    """
    data = json.dumps(payload).encode("utf-8")
    waited = 0.0
    for attempt in range(1, MAX_HTTP_ATTEMPTS + 1):
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        for key, value in headers.items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")
            except Exception:
                pass
            if exc.code not in RETRYABLE_STATUS:
                raise LLMError(
                    f"HTTP {exc.code} from {url}: {detail}") from exc
            retry_after = _retry_after_seconds(exc)
            last = LLMRateLimitError(
                f"HTTP {exc.code} from {url}: {detail}",
                status=exc.code, retry_after=retry_after)
            if attempt == MAX_HTTP_ATTEMPTS:
                raise last from exc
            delay = _backoff_delay(attempt, retry_after)
            if waited + delay > MAX_TOTAL_RETRY_WAIT:
                raise last from exc
            _debug(
                f"HTTP {exc.code} from {url}; retrying in {delay:.1f}s "
                f"(attempt {attempt + 1} of {MAX_HTTP_ATTEMPTS})")
            if not _sleep(delay, cancel_event):
                raise last from exc
            waited += delay
        except socket.timeout as exc:
            raise LLMTimeoutError(
                f"Timed out after {timeout}s contacting {url}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, socket.timeout):
                raise LLMTimeoutError(
                    f"Timed out after {timeout}s contacting {url}") from exc
            raise LLMError(f"Could not reach {url}: {reason}") from exc
    raise LLMError(f"Could not reach {url}")  # unreachable; loop always exits


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


def _question_proposal(args, reasoning="", proposal_type=QuestionProposal):
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
    return proposal_type(
        "", "", "", False, reasoning=reasoning, kind="question",
        question=str(args.get("question", "")).strip(),
        options=tuple(options),
        recommended_option=str(
            args.get("recommended_option", "")).strip(),
        allow_multiple=bool(args.get("allow_multiple")))


def _proposal_from_tool(name, args, reasoning="", text=""):
    """Build the LLMProposal for one tool call, provider-independent.

    Both the OpenAI-compatible and Anthropic adapters reduce their wire format
    to a ``(name, args)`` pair and hand off here, so the six proposal shapes
    are constructed in exactly one place. Returns ``None`` for an unrecognized
    tool name; the caller decides how to handle that (treat text as a finish).
    """
    args = args or {}
    spec = TOOL_SPEC_BY_NAME.get(name)
    if spec is None:
        return None
    proposal_type = spec.proposal_type
    if name == _TOOL_NAME:
        return proposal_type(
            intent=args.get("intent", ""), script=args.get("script", ""),
            text=text, is_tool_call=True, reasoning=reasoning, kind="script",
            strategy=args.get("strategy", "part_design"),
            stage=args.get("stage", ""),
            plan=tuple(args.get("plan") or ()),
            plan_step=int(args.get("plan_step") or 0),
            success_criteria=tuple(args.get("success_criteria") or ()),
            assumptions=tuple(
                dict(row) for row in (args.get("assumptions") or ())
                if isinstance(row, dict)),
            observed_features=tuple(
                dict(row) for row in (args.get("observed_features") or ())
                if isinstance(row, dict))
            if "observed_features" in args else None)
    if name == _FINISH_NAME:
        return proposal_type(
            intent="", script="", text=args.get("summary", "") or text,
            is_tool_call=False, reasoning=reasoning, kind="finish",
            verified=bool(args.get("verified")),
            evidence=tuple(args.get("evidence") or ()),
            reviewed_plan=bool(args.get("reviewed_plan")),
            fidelity_met=bool(args.get("fidelity_met")),
            fidelity_omissions=tuple(args.get("fidelity_omissions") or ()))
    if name == _INSPECT_NAME:
        return proposal_type(
            "", "", "", False, reasoning=reasoning, kind="inspect",
            query=args.get("query", ""))
    if name == _RENDER_NAME:
        return proposal_type(
            "", "", "", False, reasoning=reasoning, kind="render",
            render_objects=tuple(
                str(item) for item in (args.get("objects") or ()) if item))
    if name == _QUESTION_NAME:
        return _question_proposal(args, reasoning, proposal_type)
    if name == _API_NAME:
        return proposal_type(
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
    return FinishProposal(intent="", script="", text=content,
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
    # The system prompt and tool schemas are identical on every call of a
    # conversation and together run to a couple of thousand tokens. Two cache
    # breakpoints make that prefix near-free from the second turn onward, which
    # is where the per-session cost was going.
    tools = _anthropic_tools(settings)
    if tools:
        tools = tools[:-1] + [
            dict(tools[-1], cache_control={"type": "ephemeral"})]
    payload = {
        "model": wire_model,
        "max_tokens": DEFAULT_MAX_TOKENS,
        # system is top-level, not a message; a block list so it can be cached.
        "system": [{"type": "text", "text": _system_prompt(settings),
                    "cache_control": {"type": "ephemeral"}}],
        "messages": anthropic_messages,
        "tools": tools,
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
                 _API_NAME, _RENDER_NAME):
        if name in tool_blocks:
            return _proposal_from_tool(
                name, tool_blocks[name].get("input"), reasoning, text)
    # Thinking-on path may return plain text; treat it as a finish.
    return FinishProposal(intent="", script="", text=text,
                          is_tool_call=False, reasoning=reasoning, kind="finish")


def complete(messages: list, settings: Settings) -> "LLMProposal":
    provider, wire_model = _split_model(settings.model)
    if provider == "anthropic":
        return _complete_anthropic(wire_model, messages, settings)
    return _complete_openai(wire_model, provider, messages, settings)
