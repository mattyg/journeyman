from dataclasses import dataclass

# FreeCAD parameter group for this addon's settings. Follows the FreeCAD
# convention (BaseApp/Preferences/Mod/<Name>) so that a Gui::Pref* preferences
# page and the code read/write the same store. The trailing group name here
# ("Mod/LLMCopilot") must match the prefPath/prefEntry bindings in the
# preferences .ui file.
PARAM_PATH = "User parameter:BaseApp/Preferences/Mod/LLMCopilot"

@dataclass
class Settings:
    model: str
    api_key: str
    api_base: str
    confirm_before_running: bool
    auto_approve_loop: bool
    max_auto_approved_steps: int
    self_correction_attempts: int

def load_settings(param_get) -> "Settings":
    return Settings(
        model=param_get.GetString("Model", ""),
        api_key=param_get.GetString("ApiKey", ""),
        api_base=param_get.GetString("ApiBase", ""),
        confirm_before_running=param_get.GetBool("ConfirmBeforeRunning", True),
        auto_approve_loop=param_get.GetBool("AutoApproveLoop", False),
        max_auto_approved_steps=param_get.GetInt("MaxAutoApprovedSteps", 5),
        self_correction_attempts=param_get.GetInt("SelfCorrectionAttempts", 3),
    )

def save_settings(param_get, settings: "Settings") -> None:
    param_get.SetString("Model", settings.model)
    param_get.SetString("ApiKey", settings.api_key)
    param_get.SetString("ApiBase", settings.api_base)
    param_get.SetBool("ConfirmBeforeRunning", settings.confirm_before_running)
    param_get.SetBool("AutoApproveLoop", settings.auto_approve_loop)
    param_get.SetInt("MaxAutoApprovedSteps", settings.max_auto_approved_steps)
    param_get.SetInt("SelfCorrectionAttempts", settings.self_correction_attempts)
