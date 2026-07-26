"""Provider-independent context size estimates for the chat UI."""
import json
import math


def estimate_tokens(messages, system_prompt="", tools=None):
    """Return a rough token count without adding a tokenizer dependency.

    Four characters per token is a conventional display-only approximation.
    JSON serialization accounts for roles and tool schema overhead as well as
    message content. Providers may tokenize the same request differently.

    Measures the *model-facing projection* of the transcript, not the durable
    transcript itself. The two diverge once an inspection is superseded, and
    reporting the stored size would overstate every later request.
    """
    from .agent import _model_history
    payload = {
        "system": system_prompt,
        "messages": _model_history(messages),
        "tools": tools or [],
    }
    characters = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return max(1, math.ceil(characters / 4))


def format_usage(messages, system_prompt="", tools=None):
    tokens = estimate_tokens(messages, system_prompt, tools)
    if tokens >= 1000:
        token_text = f"{tokens / 1000:.1f}k"
    else:
        token_text = str(tokens)
    count = len(messages)
    noun = "message" if count == 1 else "messages"
    return f"Context: ~{token_text} tokens · {count} {noun}"
