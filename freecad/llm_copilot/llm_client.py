import json
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

TOOL_SCHEMA = [{
    "type": "function",
    "function": {
        "name": "run_freecad_script",
        "description": "Execute FreeCAD Python against the active document.",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {"type": "string",
                           "description": "One plain-language sentence describing the change."},
                "script": {"type": "string",
                           "description": "FreeCAD Python to execute."},
            },
            "required": ["intent", "script"],
        },
    },
}]

@dataclass
class LLMProposal:
    intent: str
    script: str
    text: str
    is_tool_call: bool

def _litellm_completion(**kwargs):
    import litellm
    return litellm.completion(**kwargs)

def complete(messages: list, settings: Settings) -> "LLMProposal":
    kwargs = dict(
        model=settings.model,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}] + messages,
        tools=TOOL_SCHEMA,
    )
    if settings.api_key:
        kwargs["api_key"] = settings.api_key
    if settings.api_base:
        kwargs["api_base"] = settings.api_base
    resp = _litellm_completion(**kwargs)
    msg = resp.choices[0].message
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        args = json.loads(tool_calls[0].function.arguments)
        return LLMProposal(intent=args.get("intent", ""),
                           script=args.get("script", ""),
                           text=getattr(msg, "content", "") or "",
                           is_tool_call=True)
    return LLMProposal(intent="", script="",
                       text=getattr(msg, "content", "") or "",
                       is_tool_call=False)
