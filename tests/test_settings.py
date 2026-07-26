from freecad.journeyman import settings as st
from freecad.journeyman.settings import (
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
    assert s.enhanced_validation is True
    assert s.structured_diff is True
    assert s.mandatory_verification is True
    assert s.read_only_inspection is True
    assert s.rendered_views is False
    assert s.rollback_on_validation_failure is True
    assert s.rich_snapshot is True
    assert s.render_strategy == "global_and_changed"
    assert s.max_isolated_images == 4
    assert s.persist_chat_history is True
    assert s.keep_script_history is False
    assert s.feature_retry_cap == 2
    assert s.one_feature_per_step is False
    assert s.keep_partial_on_error is True
    assert s.structured_cad_planning is True
    assert s.parametric_feature_preference is True
    assert s.sketch_constraint_verification is True
    assert s.stage_order_guidance is True
    assert s.design_ledger_context is True
    assert s.final_design_review is True
    assert s.technical_edge_overlay is True
    assert s.color_separate_objects is True
    assert s.depth_enhanced_shading is True
    assert s.freecad_api_lookup is True
    assert s.assumption_ledger is False
    assert s.fidelity_target == "unspecified"
    assert s.mark_inferred_features is False


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


def test_sort_models_newest_and_flagship_first():
    from freecad.journeyman.settings import sort_models
    # family tier (opus>sonnet>haiku) then natural version, descending
    out = sort_models(
        ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-7",
         "claude-opus-4-8"], "anthropic")
    assert out == ["claude-opus-4-8", "claude-opus-4-7",
                   "claude-sonnet-5", "claude-haiku-4-5"]


def test_sort_models_numeric_aware_and_dedup():
    from freecad.journeyman.settings import sort_models
    out = sort_models(
        ["claude-opus-4-8", "claude-opus-4-10", "claude-opus-4-8",
         "claude-opus-4-9"], "anthropic")
    # 4-10 sorts above 4-8 (numbers, not strings); duplicate removed
    assert out == ["claude-opus-4-10", "claude-opus-4-9", "claude-opus-4-8"]


def test_sort_models_unknown_provider_natural_descending():
    from freecad.journeyman.settings import sort_models
    out = sort_models(["gpt-4o", "gpt-5.4", "gpt-5-mini"], "openai")
    assert out[0] == "gpt-5.4"
    assert out[-1] == "gpt-4o"


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


def test_harness_feature_flags_roundtrip():
    p = FakeParam()
    settings = load_settings(p)
    settings.enhanced_validation = False
    settings.structured_diff = False
    settings.mandatory_verification = False
    settings.read_only_inspection = False
    settings.rendered_views = True
    settings.rollback_on_validation_failure = False
    settings.rich_snapshot = False
    settings.render_strategy = "changed"
    settings.max_isolated_images = 2
    settings.persist_chat_history = False
    settings.keep_script_history = True
    settings.feature_retry_cap = 4
    settings.one_feature_per_step = True
    settings.keep_partial_on_error = False
    settings.structured_cad_planning = False
    settings.parametric_feature_preference = False
    settings.sketch_constraint_verification = False
    settings.stage_order_guidance = False
    settings.design_ledger_context = False
    settings.final_design_review = False
    settings.technical_edge_overlay = False
    settings.color_separate_objects = False
    settings.depth_enhanced_shading = False
    settings.freecad_api_lookup = False
    settings.assumption_ledger = True
    settings.fidelity_target = "stylised"
    settings.mark_inferred_features = True
    save_settings(p, settings)
    loaded = load_settings(p)
    assert loaded.enhanced_validation is False
    assert loaded.structured_diff is False
    assert loaded.mandatory_verification is False
    assert loaded.read_only_inspection is False
    assert loaded.rendered_views is True
    assert loaded.rollback_on_validation_failure is False
    assert loaded.rich_snapshot is False
    assert loaded.render_strategy == "changed"
    assert loaded.max_isolated_images == 2
    assert loaded.persist_chat_history is False
    assert loaded.keep_script_history is True
    assert loaded.feature_retry_cap == 4
    assert loaded.one_feature_per_step is True
    assert loaded.keep_partial_on_error is False
    assert loaded.structured_cad_planning is False
    assert loaded.parametric_feature_preference is False
    assert loaded.sketch_constraint_verification is False
    assert loaded.stage_order_guidance is False
    assert loaded.design_ledger_context is False
    assert loaded.final_design_review is False
    assert loaded.technical_edge_overlay is False
    assert loaded.color_separate_objects is False
    assert loaded.depth_enhanced_shading is False
    assert loaded.freecad_api_lookup is False
    assert loaded.assumption_ledger is True
    assert loaded.fidelity_target == "stylised"
    assert loaded.mark_inferred_features is True
