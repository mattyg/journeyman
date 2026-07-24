import json
import freecad.llm_copilot.llm_client as lc
from freecad.llm_copilot.settings import Settings


def _settings(model="openai/gpt-5.4", api_key="sk-x", api_base="",
              reasoning_effort="off"):
    return Settings(model, api_key, api_base, True, False, 5, 3, reasoning_effort)


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


def test_openai_forces_tool_choice(monkeypatch):
    captured = {}
    _patch_http(monkeypatch,
                _openai_response({"intent": "x", "script": "pass"}), captured)
    lc.complete([{"role": "user", "content": "box"}], _settings())
    assert captured["payload"]["tool_choice"] == "required"


def test_openai_parses_finish_tool(monkeypatch):
    captured = {}
    resp = {"choices": [{"message": {"content": None, "tool_calls": [{
        "function": {"name": "finish",
                     "arguments": json.dumps({"summary": "All done."})}}]}}]}
    _patch_http(monkeypatch, resp, captured)
    p = lc.complete([{"role": "user", "content": "x"}], _settings())
    assert p.is_tool_call is False
    assert p.kind == "finish"
    assert p.text == "All done."


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


def test_openai_captures_reasoning_and_sends_effort(monkeypatch):
    captured = {}
    resp = _openai_response(None, "hi")
    resp["choices"][0]["message"]["reasoning"] = "step one, step two"
    _patch_http(monkeypatch, resp, captured)
    p = lc.complete([{"role": "user", "content": "hi"}],
                    _settings(reasoning_effort="high"))
    assert p.reasoning == "step one, step two"
    assert captured["payload"]["reasoning_effort"] == "high"


def test_openai_no_effort_field_when_off(monkeypatch):
    captured = {}
    _patch_http(monkeypatch, _openai_response(None, "hi"), captured)
    p = lc.complete([{"role": "user", "content": "hi"}], _settings())
    assert "reasoning_effort" not in captured["payload"]
    assert p.reasoning == ""


def test_openrouter_reasoning_details(monkeypatch):
    captured = {}
    resp = _openai_response(None, "hi")
    resp["choices"][0]["message"]["reasoning_details"] = [
        {"text": "a"}, {"summary": "b"}]
    _patch_http(monkeypatch, resp, captured)
    p = lc.complete([{"role": "user", "content": "hi"}],
                    _settings(model="openrouter/x"))
    assert p.reasoning == "a\nb"


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


def test_anthropic_captures_thinking_and_sends_config(monkeypatch):
    captured = {}
    _patch_http(monkeypatch, _anthropic_response([
        {"type": "thinking", "thinking": "considering the plate shape"},
        {"type": "tool_use", "name": "run_freecad_script",
         "input": {"intent": "make plate", "script": "pass"}},
    ]), captured)
    p = lc.complete([{"role": "user", "content": "x"}],
                    _settings(model="anthropic/claude-opus-4-8",
                              reasoning_effort="medium"))
    assert p.is_tool_call is True
    assert p.reasoning == "considering the plate shape"
    assert captured["payload"]["thinking"]["type"] == "adaptive"
    assert captured["payload"]["output_config"]["effort"] == "medium"


def test_anthropic_forces_tool_when_no_thinking(monkeypatch):
    captured = {}
    _patch_http(monkeypatch, _anthropic_response([
        {"type": "tool_use", "name": "run_freecad_script",
         "input": {"intent": "x", "script": "pass"}}]), captured)
    lc.complete([{"role": "user", "content": "x"}],
                _settings(model="anthropic/claude-opus-4-8"))  # effort off
    assert captured["payload"]["tool_choice"] == {"type": "any"}


def test_anthropic_no_forced_tool_with_thinking(monkeypatch):
    captured = {}
    _patch_http(monkeypatch, _anthropic_response([
        {"type": "tool_use", "name": "run_freecad_script",
         "input": {"intent": "x", "script": "pass"}}]), captured)
    lc.complete([{"role": "user", "content": "x"}],
                _settings(model="anthropic/claude-opus-4-8",
                          reasoning_effort="high"))
    # thinking + forced tool_choice are incompatible on Anthropic
    assert "tool_choice" not in captured["payload"]


def test_anthropic_parses_finish_tool(monkeypatch):
    captured = {}
    _patch_http(monkeypatch, _anthropic_response([
        {"type": "tool_use", "name": "finish",
         "input": {"summary": "Finished."}}]), captured)
    p = lc.complete([{"role": "user", "content": "x"}],
                    _settings(model="anthropic/claude-opus-4-8"))
    assert p.is_tool_call is False
    assert p.kind == "finish"
    assert p.text == "Finished."


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


# ---- list_models ----

def _patch_get(monkeypatch, response, captured):
    def fake_get(url, headers):
        captured["url"] = url
        captured["headers"] = headers
        return response
    monkeypatch.setattr(lc, "_http_get_json", fake_get)


def test_list_models_openai(monkeypatch):
    captured = {}
    _patch_get(monkeypatch, {"data": [{"id": "gpt-5.4"}, {"id": "gpt-5-mini"}]},
               captured)
    models = lc.list_models("openai", _settings(model="openai/x"))
    assert models == ["gpt-5.4", "gpt-5-mini"]
    assert captured["url"] == "https://api.openai.com/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer sk-x"


def test_list_models_anthropic(monkeypatch):
    captured = {}
    _patch_get(monkeypatch, {"data": [{"id": "claude-opus-4-8"}]}, captured)
    models = lc.list_models("anthropic", _settings(model="anthropic/x"))
    assert models == ["claude-opus-4-8"]
    assert captured["url"] == "https://api.anthropic.com/v1/models"
    assert captured["headers"]["x-api-key"] == "sk-x"
    assert captured["headers"]["anthropic-version"] == lc.ANTHROPIC_VERSION


def test_list_models_ollama_uses_native_tags(monkeypatch):
    captured = {}
    _patch_get(monkeypatch, {"models": [{"name": "llama3"}, {"name": "qwen2.5"}]},
               captured)
    s = _settings(model="ollama/x", api_key="", api_base="http://localhost:11434/v1")
    models = lc.list_models("ollama", s)
    assert models == ["llama3", "qwen2.5"]
    # native tags endpoint at host root, not under /v1
    assert captured["url"] == "http://localhost:11434/api/tags"
    assert "Authorization" not in captured["headers"]


def test_list_models_raises_on_error(monkeypatch):
    def boom(url, headers):
        raise lc.LLMError("HTTP 401")
    monkeypatch.setattr(lc, "_http_get_json", boom)
    try:
        lc.list_models("openai", _settings())
    except lc.LLMError:
        pass
    else:
        raise AssertionError("expected LLMError")


def test_http_get_wraps_timeout_as_llmerror():
    # 203.0.113.0/24 (TEST-NET-3) is reserved and unroutable, so the connect
    # attempt hits our timeout. Verify _http_get_json turns that into an
    # LLMError with a clear timeout message rather than hanging or leaking a
    # raw socket error.
    import time
    start = time.monotonic()
    try:
        lc._http_get_json("http://203.0.113.1:81/models", {}, timeout=1)
    except lc.LLMError as e:
        assert "imed out" in str(e) or "reach" in str(e)
    else:
        raise AssertionError("expected LLMError")
    # must not have blocked far beyond the 1s timeout
    assert time.monotonic() - start < 10


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
