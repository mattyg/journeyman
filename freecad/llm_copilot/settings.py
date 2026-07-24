from dataclasses import dataclass

# FreeCAD parameter group for this addon's settings. Follows the FreeCAD
# convention (BaseApp/Preferences/Mod/<Name>).
PARAM_PATH = "User parameter:BaseApp/Preferences/Mod/LLMCopilot"

# Providers the settings UI knows about. Order is the dropdown order.
PROVIDERS = ("anthropic", "openai", "openrouter", "ollama")

# Human-readable labels for the provider dropdown.
PROVIDER_LABELS = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "ollama": "Ollama (local)",
}

# Ollama is local and keyless; it needs a host URL instead of an API key.
OLLAMA_DEFAULT_BASE = "http://localhost:11434/v1"

# Curated fallback model shortlists, shown when no live list has been fetched
# (no key yet, offline, or fetch failed). The live fetch replaces these.
CURATED_MODELS = {
    "anthropic": ["claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5"],
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


def load_settings(param_get) -> "Settings":
    provider = get_provider(param_get)
    bare_model = get_model_for_provider(param_get, provider)
    return Settings(
        model=_resolve_model(provider, bare_model),
        api_key=get_api_key(param_get, provider),
        api_base=get_api_base(param_get, provider),
        confirm_before_running=param_get.GetBool("ConfirmBeforeRunning", True),
        auto_approve_loop=param_get.GetBool("AutoApproveLoop", False),
        max_auto_approved_steps=param_get.GetInt("MaxAutoApprovedSteps", 5),
        self_correction_attempts=param_get.GetInt("SelfCorrectionAttempts", 3),
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
    if "/" in settings.model:
        provider, bare = settings.model.split("/", 1)
        if provider in PROVIDERS:
            set_provider(param_get, provider)
            set_model_for_provider(param_get, provider, bare)
