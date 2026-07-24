import types, json
import freecad.llm_copilot.llm_client as lc
from freecad.llm_copilot.settings import Settings

def _settings():
    return Settings("openai/gpt-5.4", "sk-x", "", True, False, 5, 3)

def _fake_response(tool_args=None, content=""):
    # mimics litellm/openai response object shape
    tc = []
    if tool_args is not None:
        fn = types.SimpleNamespace(name="run_freecad_script",
                                   arguments=json.dumps(tool_args))
        tc = [types.SimpleNamespace(function=fn)]
    msg = types.SimpleNamespace(content=content, tool_calls=tc or None)
    choice = types.SimpleNamespace(message=msg)
    return types.SimpleNamespace(choices=[choice])

def test_parses_tool_call(monkeypatch):
    captured = {}
    def fake_completion(**kwargs):
        captured.update(kwargs)
        return _fake_response({"script": "import Part", "intent": "make a box"})
    monkeypatch.setattr(lc, "_litellm_completion", fake_completion)
    p = lc.complete([{"role": "user", "content": "box"}], _settings())
    assert p.is_tool_call is True
    assert p.script == "import Part"
    assert p.intent == "make a box"
    assert captured["model"] == "openai/gpt-5.4"
    assert captured["api_key"] == "sk-x"
    assert captured["tools"] == lc.TOOL_SCHEMA
    assert captured["messages"][0]["role"] == "system"

def test_plain_text_when_no_tool_call(monkeypatch):
    monkeypatch.setattr(lc, "_litellm_completion",
                        lambda **k: _fake_response(None, "Looks good!"))
    p = lc.complete([{"role": "user", "content": "hi"}], _settings())
    assert p.is_tool_call is False
    assert p.text == "Looks good!"
    assert p.script == ""
