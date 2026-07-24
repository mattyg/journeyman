import json
import freecad.llm_copilot.llm_client as lc
from freecad.llm_copilot.settings import Settings


def _settings(model="openai/gpt-5.4", api_key="sk-x", api_base=""):
    return Settings(model, api_key, api_base, True, False, 5, 3)


def _patch_http(monkeypatch, response, captured):
    def fake_post(url, headers, payload):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        return response
    monkeypatch.setattr(lc, "_http_post_json", fake_post)


# ---- OpenAI-compatible adapter (OpenAI / Ollama / OpenRouter) ----

def _openai_response(tool_args=None, content=None):
    message = {"content": content}
    if tool_args is not None:
        message["tool_calls"] = [{
            "function": {"name": "run_freecad_script",
                         "arguments": json.dumps(tool_args)},
        }]
    return {"choices": [{"message": message}]}


def test_openai_parses_tool_call(monkeypatch):
    captured = {}
    _patch_http(monkeypatch,
                _openai_response({"script": "import Part", "intent": "make a box"}),
                captured)
    p = lc.complete([{"role": "user", "content": "box"}], _settings())
    assert p.is_tool_call is True
    assert p.script == "import Part"
    assert p.intent == "make a box"
    # provider prefix stripped from the wire model
    assert captured["payload"]["model"] == "gpt-5.4"
    assert captured["payload"]["tools"] == lc.TOOL_SCHEMA
    assert captured["payload"]["messages"][0]["role"] == "system"
    assert captured["headers"]["Authorization"] == "Bearer sk-x"
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"


def test_openai_plain_text_when_no_tool_call(monkeypatch):
    captured = {}
    _patch_http(monkeypatch, _openai_response(None, "Looks good!"), captured)
    p = lc.complete([{"role": "user", "content": "hi"}], _settings())
    assert p.is_tool_call is False
    assert p.text == "Looks good!"
    assert p.script == ""


def test_ollama_uses_api_base_and_no_auth_header(monkeypatch):
    captured = {}
    _patch_http(monkeypatch, _openai_response(None, "hi"), captured)
    s = _settings(model="ollama/llama3", api_key="", api_base="http://localhost:11434/v1")
    lc.complete([{"role": "user", "content": "hi"}], s)
    assert captured["url"] == "http://localhost:11434/v1/chat/completions"
    assert "Authorization" not in captured["headers"]
    assert captured["payload"]["model"] == "llama3"


def test_openrouter_default_base(monkeypatch):
    captured = {}
    _patch_http(monkeypatch, _openai_response(None, "hi"), captured)
    lc.complete([{"role": "user", "content": "hi"}],
                _settings(model="openrouter/anthropic/claude-opus-4-8"))
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    # only the first path segment is the provider; the rest is the wire model
    assert captured["payload"]["model"] == "anthropic/claude-opus-4-8"


# ---- Anthropic native adapter ----

def _anthropic_response(blocks):
    return {"content": blocks}


def test_anthropic_parses_tool_use(monkeypatch):
    captured = {}
    _patch_http(monkeypatch, _anthropic_response([
        {"type": "text", "text": "Adding a box. "},
        {"type": "tool_use", "name": "run_freecad_script",
         "input": {"intent": "make a box", "script": "import Part"}},
    ]), captured)
    p = lc.complete([{"role": "user", "content": "box"}],
                    _settings(model="anthropic/claude-opus-4-8"))
    assert p.is_tool_call is True
    assert p.intent == "make a box"
    assert p.script == "import Part"
    assert p.text == "Adding a box. "
    # Anthropic-specific wire shape
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "sk-x"
    assert captured["headers"]["anthropic-version"] == lc.ANTHROPIC_VERSION
    assert captured["payload"]["model"] == "claude-opus-4-8"
    assert captured["payload"]["system"] == lc.SYSTEM_PROMPT
    assert captured["payload"]["max_tokens"] == lc.DEFAULT_MAX_TOKENS
    assert captured["payload"]["tools"] == lc.ANTHROPIC_TOOL_SCHEMA
    # system is hoisted out of messages, not injected as a message
    assert all(m["role"] != "system" for m in captured["payload"]["messages"])


def test_anthropic_plain_text(monkeypatch):
    captured = {}
    _patch_http(monkeypatch, _anthropic_response([
        {"type": "text", "text": "Done — looks right?"},
    ]), captured)
    p = lc.complete([{"role": "user", "content": "hi"}],
                    _settings(model="anthropic/claude-opus-4-8"))
    assert p.is_tool_call is False
    assert p.text == "Done — looks right?"
    assert p.script == ""


def test_http_error_becomes_llmerror(monkeypatch):
    def boom(url, headers, payload):
        raise lc.LLMError("HTTP 401 from x: bad key")
    monkeypatch.setattr(lc, "_http_post_json", boom)
    try:
        lc.complete([{"role": "user", "content": "x"}], _settings())
    except lc.LLMError as e:
        assert "401" in str(e)
    else:
        raise AssertionError("expected LLMError")
