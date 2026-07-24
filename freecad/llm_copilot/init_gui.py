# freecad/llm_copilot/init_gui.py
import FreeCAD
import FreeCADGui as Gui

class _TogglePanelCommand:
    def GetResources(self):
        return {"MenuText": "LLM Copilot",
                "ToolTip": "Show/hide the LLM Copilot chat panel"}
    def IsActive(self):
        return True
    def Activated(self):
        from freecad.llm_copilot.chat_panel import toggle_panel
        toggle_panel()

class LLMCopilotWorkbench(Gui.Workbench):
    MenuText = "LLM Copilot"
    ToolTip = "AI copilot for creating and editing CAD models"

    def Initialize(self):
        from freecad.llm_copilot.deps import ensure_litellm
        ensure_litellm()
        Gui.addCommand("LLMCopilot_TogglePanel", _TogglePanelCommand())
        self.appendToolbar("LLM Copilot", ["LLMCopilot_TogglePanel"])
        self.appendMenu("LLM Copilot", ["LLMCopilot_TogglePanel"])

    def Activated(self):
        from freecad.llm_copilot.chat_panel import toggle_panel
        toggle_panel()

    def GetClassName(self):
        return "Gui::PythonWorkbench"

Gui.addWorkbench(LLMCopilotWorkbench())
