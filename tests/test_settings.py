from freecad.llm_copilot import settings as st
from freecad.llm_copilot.settings import (
    Settings, load_settings, save_settings,
    get_provider, set_provider, get_api_key, set_api_key,
    get_model_for_provider, set_model_for_provider,
    get_cached_models, set_cached_models, get_api_base, set_ollama_base,
)


class FakeParam:
    def __init__(self): self.s, self.b, self.i = {}, {}, {}
    def GetString(self, k, d=""): return self.s.get(k, d)
    def SetString(self, k, v): self.s[k] = v
    def GetBool(self, k, d=False): return self.b.get(k, d)
    def SetBool(self, k, v): self.b[k] = v
    def GetInt(self, k, d=0): return self.i.get(k, d)
    def SetInt(self, k, v): self.i[k] = v


def test_defaults_when_unset():
    s = load_settings(FakeParam())
    # Default provider is the first in PROVIDERS; model resolves to that
    # provider's curated default, prefixed.
    assert s.model == "anthropic/claude-opus-4-8"
    assert s.api_key == ""
    assert s.confirm_before_running is True
    assert s.auto_approve_loop is False
    assert s.max_auto_approved_steps == 5
    assert s.self_correction_attempts == 3


def test_per_provider_keys_are_isolated():
    p = FakeParam()
    set_api_key(p, "anthropic", "sk-ant")
    set_api_key(p, "openai", "sk-oai")
    assert get_api_key(p, "anthropic") == "sk-ant"
    assert get_api_key(p, "openai") == "sk-oai"
    # ollama never stores/returns a key
    set_api_key(p, "ollama", "ignored")
    assert get_api_key(p, "ollama") == ""


def test_load_resolves_selected_provider_and_key():
    p = FakeParam()
    set_provider(p, "openai")
    set_model_for_provider(p, "openai", "gpt-5.4")
    set_api_key(p, "openai", "sk-oai")
    set_api_key(p, "anthropic", "sk-ant")  # must not leak into openai
    s = load_settings(p)
    assert s.model == "openai/gpt-5.4"
    assert s.api_key == "sk-oai"


def test_ollama_uses_host_not_key():
    p = FakeParam()
    set_provider(p, "ollama")
    set_ollama_base(p, "http://box:11434/v1")
    set_model_for_provider(p, "ollama", "llama3")
    s = load_settings(p)
    assert s.model == "ollama/llama3"
    assert s.api_key == ""
    assert s.api_base == "http://box:11434/v1"


def test_cached_models_roundtrip_and_fallback():
    p = FakeParam()
    # Fallback to curated list when nothing cached
    assert get_cached_models(p, "anthropic") == st.CURATED_MODELS["anthropic"]
    set_cached_models(p, "anthropic", ["claude-x", "claude-y"])
    assert get_cached_models(p, "anthropic") == ["claude-x", "claude-y"]


def test_autonomy_settings_roundtrip():
    p = FakeParam()
    save_settings(p, Settings(
        model="openrouter/anthropic/claude-opus-4-8", api_key="", api_base="",
        confirm_before_running=False, auto_approve_loop=True,
        max_auto_approved_steps=8, self_correction_attempts=2))
    s = load_settings(p)
    assert s.confirm_before_running is False
    assert s.auto_approve_loop is True
    assert s.max_auto_approved_steps == 8
    assert s.self_correction_attempts == 2
    # save_settings mirrored the resolved model back into provider slots
    assert get_provider(p) == "openrouter"
    assert s.model == "openrouter/anthropic/claude-opus-4-8"
