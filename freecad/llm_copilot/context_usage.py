"""Provider-independent context size estimates for the chat UI."""
import json
import math


def estimate_tokens(messages, system_prompt="", tools=None):
    """Return a rough token count without adding a tokenizer dependency.

    Four characters per token is a conventional display-only approximation.
    JSON serialization accounts for roles and tool schema overhead as well as
    message content. Providers may tokenize the same request differently.
    """
    payload = {
        "system": system_prompt,
        "messages": messages,
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
