from dataclasses import dataclass

# FreeCAD parameter group for this addon's settings. Follows the FreeCAD
# convention (BaseApp/Preferences/Mod/<Name>).
PARAM_PATH = "User parameter:BaseApp/Preferences/Mod/LLMCopilot"

# Providers the settings UI knows about. Order is the dropdown order.
PROVIDERS = ("anthropic", "openai", "openrouter", "ollama")
RENDER_STRATEGIES = ("global", "changed", "global_and_changed")

# Human-readable labels for the provider dropdown.
PROVIDER_LABELS = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "ollama": "Ollama (local)",
}

# Ollama is local and keyless; it needs a host URL instead of an API key.
OLLAMA_DEFAULT_BASE = "http://localhost:11434/v1"

# Curated fallback model lists, shown before/instead of a live fetch (no key
# yet, offline, or fetch failed). The live fetch (list_models) replaces these
# whenever it succeeds, so these only need to cover the common case.
#
# Anthropic: the full current catalog of active, generally-available models
# (from the Anthropic model documentation). OpenAI/OpenRouter change frequently
# and expose hundreds via their /models endpoints, so a hardcoded list there is
# only a small starting point — the live fetch is the real source. Ollama has no
# universal list; models are whatever the user has pulled locally (via /api/tags).
CURATED_MODELS = {
    "anthropic": [
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-opus-4-5",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
        "claude-haiku-4-5",
    ],
    "openai": ["gpt-5.4", "gpt-5-mini"],
    "openrouter": ["anthropic/claude-opus-4-8", "openai/gpt-5.4"],
    "ollama": ["llama3", "qwen2.5-coder"],
}


@dataclass
class Settings:
    """Resolved settings the LLM client consumes.

    `model` is the fully-prefixed string (e.g. "anthropic/claude-opus-4-8") the
    client selects a provider from; `api_key`/`api_base` are already resolved for
    the active provider. The provider-aware storage below is flattened into this
    shape by load_settings so the client and agent are unaffected by the UI.
    """
    model: str
    api_key: str
    api_base: str
    confirm_before_running: bool
    auto_approve_loop: bool
    max_auto_approved_steps: int
    self_correction_attempts: int
    reasoning_effort: str = "off"   # off | low | medium | high (provider-dependent)
    enhanced_validation: bool = True
    structured_diff: bool = True
    mandatory_verification: bool = True
    read_only_inspection: bool = True
    rendered_views: bool = False
    rollback_on_validation_failure: bool = True
    rich_snapshot: bool = True
    render_strategy: str = "global_and_changed"
    max_isolated_images: int = 4
    persist_chat_history: bool = True
    structured_cad_planning: bool = False
    parametric_feature_preference: bool = False
    sketch_constraint_verification: bool = False
    stage_order_guidance: bool = False
    design_ledger_context: bool = False
    final_design_review: bool = False
    technical_edge_overlay: bool = False
    color_separate_objects: bool = False
    depth_enhanced_shading: bool = False
    freecad_api_lookup: bool = True
    assumption_ledger: bool = False
    fidelity_target: str = "unspecified"
    mark_inferred_features: bool = False


# Family tiers we prefer to surface first, per provider, when the model id
# contains one of these tokens. Earlier = higher in the list. Anything without a
# known tier sorts after these, by natural version order.
_FAMILY_ORDER = {
    "anthropic": ["opus", "sonnet", "haiku"],
    # openrouter ids are "<vendor>/<model>"; still catch anthropic tiers within.
    "openrouter": ["opus", "sonnet", "haiku"],
}


def _natural_key(text: str):
    """Split a string into text/number chunks so numeric parts compare as
    numbers (so "4-10" > "4-8"). Text chunks compare case-insensitively."""
    import re
    parts = re.split(r"(\d+)", text)
    key = []
    for p in parts:
        if p.isdigit():
            key.append((1, int(p)))       # numbers after text within a chunk run
        elif p:
            key.append((0, p.lower()))
    return key


def _family_rank(model: str, provider: str) -> int:
    order = _FAMILY_ORDER.get(provider, [])
    low = model.lower()
    for i, fam in enumerate(order):
        if fam in low:
            return i
    return len(order)  # unknown families sort after the known tiers


def sort_models(models, provider: str) -> list:
    """Order models newest/flagship-first for the dropdown.

    Primary key: family tier (opus before sonnet before haiku, where known).
    Secondary key: natural version order, descending, so claude-opus-4-8 is above
    -4-7 and -4-10 is above -4-8. De-duplicates while preserving this order.
    """
    unique = list(dict.fromkeys(models))  # dedupe, keep first occurrence
    return sorted(
        unique,
        key=lambda m: (_family_rank(m, provider), _NaturalDesc(_natural_key(m))),
    )


class _NaturalDesc:
    """Wraps a natural-sort key so a plain ascending sort orders it descending
    (newest first) without needing reverse=True to fight the family-rank key."""
    __slots__ = ("key",)

    def __init__(self, key):
        self.key = key

    def __lt__(self, other):
        # invert: we are "less than" (sort earlier) when our natural key is
        # greater, giving descending order for this sub-key only.
        return self.key > other.key

    def __eq__(self, other):
        return self.key == other.key


def _key_entry(provider: str) -> str:
    return "ApiKey_" + provider


def _model_entry(provider: str) -> str:
    return "Model_" + provider


def _cache_entry(provider: str) -> str:
    return "ModelCache_" + provider


def get_provider(param_get) -> str:
    provider = param_get.GetString("Provider", PROVIDERS[0])
    return provider if provider in PROVIDERS else PROVIDERS[0]


def get_api_key(param_get, provider: str) -> str:
    """Per-provider API key (Ollama has none)."""
    if provider == "ollama":
        return ""
    return param_get.GetString(_key_entry(provider), "")


def get_api_base(param_get, provider: str) -> str:
    """Resolved base URL. Only Ollama exposes a user-editable host; the hosted
    providers use their built-in defaults (resolved in the client)."""
    if provider == "ollama":
        return param_get.GetString("OllamaBase", OLLAMA_DEFAULT_BASE)
    return ""


def get_model_for_provider(param_get, provider: str) -> str:
    """Bare (unprefixed) model id chosen for a provider."""
    default = CURATED_MODELS.get(provider, [""])[0]
    return param_get.GetString(_model_entry(provider), default)


def get_cached_models(param_get, provider: str) -> list:
    """Last fetched model list for a provider, or the curated fallback."""
    raw = param_get.GetString(_cache_entry(provider), "")
    models = [m for m in raw.split("\n") if m]
    return models or list(CURATED_MODELS.get(provider, []))


def set_cached_models(param_get, provider: str, models) -> None:
    param_get.SetString(_cache_entry(provider), "\n".join(models))


def set_provider(param_get, provider: str) -> None:
    param_get.SetString("Provider", provider)


def set_api_key(param_get, provider: str, key: str) -> None:
    if provider != "ollama":
        param_get.SetString(_key_entry(provider), key)


def set_ollama_base(param_get, base: str) -> None:
    param_get.SetString("OllamaBase", base or OLLAMA_DEFAULT_BASE)


def set_model_for_provider(param_get, provider: str, model: str) -> None:
    param_get.SetString(_model_entry(provider), model)


def _resolve_model(provider: str, bare_model: str) -> str:
    """Build the client's prefixed model string from provider + bare id."""
    if not bare_model:
        return ""
    return provider + "/" + bare_model


def model_display_name(model: str) -> str:
    """A short, readable label for a model id, for use in the chat transcript.

    Strips the provider prefix and any vendor path, then title-cases the bare id:
      "openrouter/moonshotai/kimi-k3" -> "Kimi K3"
      "anthropic/claude-opus-4-8"     -> "Claude Opus 4 8"
      "ollama/llama3"                 -> "Llama3"
    Falls back to "Copilot" when no model is set.
    """
    if not model:
        return "Copilot"
    bare = model.rsplit("/", 1)[-1]          # last path segment
    words = bare.replace("_", "-").split("-")
    return " ".join(w[:1].upper() + w[1:] if w else w for w in words) or "Copilot"


def load_settings(param_get) -> "Settings":
    provider = get_provider(param_get)
    bare_model = get_model_for_provider(param_get, provider)
    render_strategy = param_get.GetString(
        "RenderStrategy", "global_and_changed")
    if render_strategy not in RENDER_STRATEGIES:
        render_strategy = "global_and_changed"
    fidelity_target = param_get.GetString("FidelityTarget", "unspecified")
    if fidelity_target not in (
            "unspecified", "replica", "stylised", "functional_analogue"):
        fidelity_target = "unspecified"
    return Settings(
        model=_resolve_model(provider, bare_model),
        api_key=get_api_key(param_get, provider),
        api_base=get_api_base(param_get, provider),
        confirm_before_running=param_get.GetBool("ConfirmBeforeRunning", True),
        auto_approve_loop=param_get.GetBool("AutoApproveLoop", False),
        max_auto_approved_steps=param_get.GetInt("MaxAutoApprovedSteps", 5),
        self_correction_attempts=param_get.GetInt("SelfCorrectionAttempts", 3),
        reasoning_effort=param_get.GetString("ReasoningEffort", "off"),
        enhanced_validation=param_get.GetBool("EnhancedValidation", True),
        structured_diff=param_get.GetBool("StructuredDiff", True),
        mandatory_verification=param_get.GetBool("MandatoryVerification", True),
        read_only_inspection=param_get.GetBool("ReadOnlyInspection", True),
        rendered_views=param_get.GetBool("RenderedViews", False),
        rollback_on_validation_failure=param_get.GetBool(
            "RollbackOnValidationFailure", True),
        rich_snapshot=param_get.GetBool("RichSnapshot", True),
        render_strategy=render_strategy,
        max_isolated_images=max(
            0, min(20, param_get.GetInt("MaxIsolatedImages", 4))),
        persist_chat_history=param_get.GetBool("PersistChatHistory", True),
        structured_cad_planning=param_get.GetBool(
            "StructuredCADPlanning", True),
        parametric_feature_preference=param_get.GetBool(
            "ParametricFeaturePreference", True),
        sketch_constraint_verification=param_get.GetBool(
            "SketchConstraintVerification", True),
        stage_order_guidance=param_get.GetBool("StageOrderGuidance", True),
        design_ledger_context=param_get.GetBool("DesignLedgerContext", True),
        final_design_review=param_get.GetBool("FinalDesignReview", True),
        technical_edge_overlay=param_get.GetBool(
            "TechnicalEdgeOverlay", True),
        color_separate_objects=param_get.GetBool(
            "ColorSeparateObjects", True),
        depth_enhanced_shading=param_get.GetBool(
            "DepthEnhancedShading", True),
        freecad_api_lookup=param_get.GetBool("FreeCADAPILookup", True),
        assumption_ledger=param_get.GetBool("AssumptionLedger", False),
        fidelity_target=fidelity_target,
        mark_inferred_features=param_get.GetBool(
            "MarkInferredFeatures", False),
    )


def save_settings(param_get, settings: "Settings") -> None:
    """Persist the autonomy settings. Provider/model/key are written through the
    per-provider setters above (from the settings UI); this saves the parts of
    Settings that are provider-independent, plus mirrors the resolved model back
    to its provider slot so a round-trip is faithful."""
    param_get.SetBool("ConfirmBeforeRunning", settings.confirm_before_running)
    param_get.SetBool("AutoApproveLoop", settings.auto_approve_loop)
    param_get.SetInt("MaxAutoApprovedSteps", settings.max_auto_approved_steps)
    param_get.SetInt("SelfCorrectionAttempts", settings.self_correction_attempts)
    param_get.SetBool("EnhancedValidation", settings.enhanced_validation)
    param_get.SetBool("StructuredDiff", settings.structured_diff)
    param_get.SetBool("MandatoryVerification", settings.mandatory_verification)
    param_get.SetBool("ReadOnlyInspection", settings.read_only_inspection)
    param_get.SetBool("RenderedViews", settings.rendered_views)
    param_get.SetBool("RollbackOnValidationFailure",
                      settings.rollback_on_validation_failure)
    param_get.SetBool("RichSnapshot", settings.rich_snapshot)
    param_get.SetString("RenderStrategy", settings.render_strategy)
    param_get.SetInt("MaxIsolatedImages", settings.max_isolated_images)
    param_get.SetBool("PersistChatHistory", settings.persist_chat_history)
    param_get.SetBool("StructuredCADPlanning", settings.structured_cad_planning)
    param_get.SetBool(
        "ParametricFeaturePreference", settings.parametric_feature_preference)
    param_get.SetBool(
        "SketchConstraintVerification", settings.sketch_constraint_verification)
    param_get.SetBool("StageOrderGuidance", settings.stage_order_guidance)
    param_get.SetBool("DesignLedgerContext", settings.design_ledger_context)
    param_get.SetBool("FinalDesignReview", settings.final_design_review)
    param_get.SetBool("TechnicalEdgeOverlay", settings.technical_edge_overlay)
    param_get.SetBool(
        "ColorSeparateObjects", settings.color_separate_objects)
    param_get.SetBool(
        "DepthEnhancedShading", settings.depth_enhanced_shading)
    param_get.SetBool("FreeCADAPILookup", settings.freecad_api_lookup)
    param_get.SetBool("AssumptionLedger", settings.assumption_ledger)
    param_get.SetString("FidelityTarget", settings.fidelity_target)
    param_get.SetBool("MarkInferredFeatures", settings.mark_inferred_features)
    if "/" in settings.model:
        provider, bare = settings.model.split("/", 1)
        if provider in PROVIDERS:
            set_provider(param_get, provider)
            set_model_for_provider(param_get, provider, bare)
