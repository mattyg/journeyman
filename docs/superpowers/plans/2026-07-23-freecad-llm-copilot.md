# FreeCAD LLM Copilot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a provider-agnostic LLM copilot for FreeCAD that inspects the active document, proposes and executes FreeCAD Python, and lets the user review changes by visual outcome (keep/undo), delivered as an Addon-Manager-installable workbench with a dockable chat panel.

**Architecture:** A modern namespaced FreeCAD addon (`freecad/llm_copilot/`). A pure-Python core (`agent`, `llm_client`) runs the inspect→propose→execute→read-back loop and is unit-tested without FreeCAD. Two thin adapters (`document_inspector`, `script_executor`) touch the FreeCAD API and are tested headless via `freecadcmd`. One Qt dock widget (`chat_panel`) is the only UI. LiteLLM provides provider abstraction.

**Tech Stack:** Python 3, FreeCAD 1.0+ Python API, PySide (Qt), LiteLLM, pytest (core), FreeCAD `__unit_test__`/`unittest` (integration).

## Global Constraints

- Namespaced addon layout: all runtime code under `freecad/llm_copilot/`.
- Only `chat_panel.py` may import PySide/Qt. Only `document_inspector.py` and `script_executor.py` may import `FreeCAD`/`FreeCADGui`. `agent.py` and `llm_client.py` must import neither.
- Every executed script wraps in exactly one FreeCAD transaction (`openTransaction`/`commitTransaction`; `abortTransaction` on error) so one undo reverts it.
- Settings live in FreeCAD's parameter system under `User parameter:BaseApp/Preferences/LLMCopilot`.
- Settings + defaults (verbatim): Model (none); API key (none); API base URL (none); `ConfirmBeforeRunning` = true; `AutoApproveLoop` = false; `MaxAutoApprovedSteps` = 5; `SelfCorrectionAttempts` = 3.
- Review is outcome-based: users see plain-language intent + visual result, never code as the primary surface.
- LLM tool exposed to the model: `run_freecad_script(script: str, intent: str)`.
- Provider abstraction is LiteLLM; model strings like `anthropic/claude-opus-4-8`, `openai/gpt-5.4`, `ollama/llama3`.
- Core unit tests run under plain `pytest` with LiteLLM/FreeCAD mocked. Adapter tests run under `freecadcmd`.

---

## File Structure

```
freecad-llm-plugin/
├─ freecad/
│  └─ llm_copilot/
│     ├─ __init__.py            # namespace init (empty/minimal)
│     ├─ init_gui.py            # workbench + command registration, panel toggle
│     ├─ settings.py            # read/write params, dataclass Settings
│     ├─ llm_client.py          # LiteLLM wrapper (pure python)
│     ├─ agent.py               # loop orchestration (pure python)
│     ├─ document_inspector.py  # reads active doc -> snapshot (FreeCAD API)
│     ├─ script_executor.py     # runs script in a transaction (FreeCAD API)
│     └─ chat_panel.py          # QDockWidget UI (Qt)
├─ tests/
│  ├─ test_settings.py          # pytest (uses a fake ParamGet)
│  ├─ test_llm_client.py        # pytest (mocks litellm)
│  ├─ test_agent.py             # pytest (fakes for client/inspector/executor)
│  └─ integration/
│     └─ test_freecad_adapters.py  # freecadcmd/unittest
├─ package.xml
├─ pyproject.toml
├─ requirements.txt             # litellm
├─ README.md
└─ LICENSE
```

**Interface contracts shared across tasks (defined once, referenced by all):**

```python
# settings.py
@dataclass
class Settings:
    model: str
    api_key: str
    api_base: str
    confirm_before_running: bool
    auto_approve_loop: bool
    max_auto_approved_steps: int
    self_correction_attempts: int

def load_settings(param_get) -> Settings: ...
def save_settings(param_get, settings: Settings) -> None: ...

# document_inspector.py  (Inspector protocol)
def snapshot(app) -> str: ...          # text description of App.ActiveDocument, or "NO_ACTIVE_DOCUMENT"

# script_executor.py  (Executor protocol)
@dataclass
class ExecResult:
    ok: bool
    output: str        # captured stdout
    error: str         # traceback text if not ok, else ""
def run(app, script: str) -> ExecResult: ...   # wraps one transaction, recomputes, abort on error
def undo(app) -> None: ...

# llm_client.py  (LLMClient protocol)
@dataclass
class LLMProposal:
    intent: str          # plain-language, from tool call args
    script: str          # python, from tool call args
    text: str            # any assistant prose (may be "")
    is_tool_call: bool   # True if the model called run_freecad_script
def complete(messages: list[dict], settings: Settings) -> LLMProposal: ...

# agent.py
class Agent:
    def __init__(self, client, inspector, executor, app, settings): ...
    def send(self, user_message: str, on_intent, on_result) -> str:
        # returns final assistant summary; calls on_intent(intent)->bool gate,
        # on_result(ExecResult, snapshot) for UI updates
        ...
```

---

### Task 1: Addon scaffold + settings

**Files:**
- Create: `freecad/llm_copilot/__init__.py`
- Create: `freecad/llm_copilot/settings.py`
- Create: `package.xml`
- Create: `pyproject.toml`
- Create: `requirements.txt`
- Create: `LICENSE` (LGPL-2.1-or-later text)
- Test: `tests/test_settings.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Settings` dataclass, `load_settings(param_get)`, `save_settings(param_get, settings)` — the exact signatures in the shared contract above.

`param_get` is a FreeCAD `ParamGet` group handle. Its API used here: `.GetString(name, default)`, `.SetString(name, value)`, `.GetBool(name, default)`, `.SetBool(name, value)`, `.GetInt(name, default)`, `.SetInt(name, value)`. Tests inject a fake implementing these; production passes `FreeCAD.ParamGet("User parameter:BaseApp/Preferences/LLMCopilot")`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_settings.py
from freecad.llm_copilot.settings import Settings, load_settings, save_settings

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
    assert s.model == ""
    assert s.confirm_before_running is True
    assert s.auto_approve_loop is False
    assert s.max_auto_approved_steps == 5
    assert s.self_correction_attempts == 3

def test_save_then_load_roundtrips():
    p = FakeParam()
    save_settings(p, Settings(
        model="anthropic/claude-opus-4-8", api_key="sk-x", api_base="",
        confirm_before_running=False, auto_approve_loop=True,
        max_auto_approved_steps=8, self_correction_attempts=2))
    s = load_settings(p)
    assert s.model == "anthropic/claude-opus-4-8"
    assert s.api_key == "sk-x"
    assert s.confirm_before_running is False
    assert s.auto_approve_loop is True
    assert s.max_auto_approved_steps == 8
    assert s.self_correction_attempts == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_settings.py -v`
Expected: FAIL — `ModuleNotFoundError: freecad.llm_copilot.settings`

- [ ] **Step 3: Create the namespace init and implementation**

```python
# freecad/llm_copilot/__init__.py
# Namespace package for the LLM Copilot addon.
```

```python
# freecad/llm_copilot/settings.py
from dataclasses import dataclass

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
```

- [ ] **Step 4: Create packaging files**

```xml
<!-- package.xml -->
<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
<package format="1" xmlns="https://wiki.freecad.org/Package_Metadata">
  <name>LLM Copilot</name>
  <description>Provider-agnostic LLM copilot for creating and editing CAD models.</description>
  <version>0.1.0</version>
  <date>2026-07-23</date>
  <maintainer email="matt@buildyourweb.app">matt</maintainer>
  <license file="LICENSE">LGPL-2.1-or-later</license>
  <url type="repository" branch="main">https://github.com/matt/freecad-llm-plugin</url>
  <url type="readme">https://github.com/matt/freecad-llm-plugin/blob/main/README.md</url>
  <content>
    <workbench>
      <classname>LLMCopilotWorkbench</classname>
      <subdirectory>./freecad/llm_copilot/</subdirectory>
    </workbench>
  </content>
  <!-- litellm is not on the Addon Manager python allow-list; it is installed
       from requirements.txt (see README). Bundling strategy revisited in Task 8. -->
</package>
```

```toml
# pyproject.toml
[tool.black]
line-length = 120

[tool.pytest.ini_options]
pythonpath = ["."]
```

```
# requirements.txt
litellm>=1.80
```

Create `LICENSE` containing the full LGPL-2.1-or-later license text.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_settings.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add freecad/llm_copilot/__init__.py freecad/llm_copilot/settings.py package.xml pyproject.toml requirements.txt LICENSE tests/test_settings.py
git commit -m "feat: addon scaffold and settings"
```

---

### Task 2: LLM client (LiteLLM wrapper)

**Files:**
- Create: `freecad/llm_copilot/llm_client.py`
- Test: `tests/test_llm_client.py`

**Interfaces:**
- Consumes: `Settings` (Task 1).
- Produces: `LLMProposal` dataclass and `complete(messages, settings) -> LLMProposal` per the shared contract. Also `TOOL_SCHEMA` (list) and `SYSTEM_PROMPT` (str) module constants.

The tool schema is OpenAI/LiteLLM function-calling format for `run_freecad_script(script, intent)`. `complete` calls `litellm.completion(model=..., messages=[system]+messages, tools=TOOL_SCHEMA, api_key=..., api_base=...)`, then parses the first tool call. `litellm` is imported lazily inside `complete` so tests can inject a fake via the module attribute.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_llm_client.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_llm_client.py -v`
Expected: FAIL — module/attribute not found.

- [ ] **Step 3: Write the implementation**

```python
# freecad/llm_copilot/llm_client.py
import json
from dataclasses import dataclass
from .settings import Settings

SYSTEM_PROMPT = (
    "You are a CAD copilot operating inside FreeCAD. You receive a text snapshot "
    "of the active document before each turn. To change the model, call the "
    "run_freecad_script tool with (1) `intent`: one plain-language sentence a "
    "non-programmer designer will read, and (2) `script`: FreeCAD Python using the "
    "App/Part/PartDesign APIs against App.ActiveDocument. Prefer editing existing "
    "objects and referencing existing geometry over rebuilding. Do not call "
    "recompute or manage transactions yourself; the host handles that. When the "
    "task is complete or you need the user to look, reply with plain text instead "
    "of a tool call."
)

TOOL_SCHEMA = [{
    "type": "function",
    "function": {
        "name": "run_freecad_script",
        "description": "Execute FreeCAD Python against the active document.",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {"type": "string",
                           "description": "One plain-language sentence describing the change."},
                "script": {"type": "string",
                           "description": "FreeCAD Python to execute."},
            },
            "required": ["intent", "script"],
        },
    },
}]

@dataclass
class LLMProposal:
    intent: str
    script: str
    text: str
    is_tool_call: bool

def _litellm_completion(**kwargs):
    import litellm
    return litellm.completion(**kwargs)

def complete(messages: list, settings: Settings) -> "LLMProposal":
    kwargs = dict(
        model=settings.model,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}] + messages,
        tools=TOOL_SCHEMA,
    )
    if settings.api_key:
        kwargs["api_key"] = settings.api_key
    if settings.api_base:
        kwargs["api_base"] = settings.api_base
    resp = _litellm_completion(**kwargs)
    msg = resp.choices[0].message
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        args = json.loads(tool_calls[0].function.arguments)
        return LLMProposal(intent=args.get("intent", ""),
                           script=args.get("script", ""),
                           text=getattr(msg, "content", "") or "",
                           is_tool_call=True)
    return LLMProposal(intent="", script="",
                       text=getattr(msg, "content", "") or "",
                       is_tool_call=False)
```

Note: the test monkeypatches `_litellm_completion`, so `api_key`/`api_base` presence is asserted via the `test_parses_tool_call` captured kwargs — extend that test to also assert `captured["api_key"] == "sk-x"` (already included above).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_llm_client.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add freecad/llm_copilot/llm_client.py tests/test_llm_client.py
git commit -m "feat: LiteLLM client wrapper with run_freecad_script tool"
```

---

### Task 3: Agent loop

**Files:**
- Create: `freecad/llm_copilot/agent.py`
- Test: `tests/test_agent.py`

**Interfaces:**
- Consumes: `complete`/`LLMProposal` (Task 2), `Settings` (Task 1), and the Inspector/Executor protocols (`snapshot(app)`, `run(app, script)->ExecResult`, `undo(app)`) defined in the shared contract. `ExecResult` is imported from `script_executor` (Task 5) — but to keep Task 3 testable before Task 5, define `ExecResult` in a tiny shared module.
- Produces: `Agent` class with `send(user_message, on_intent, on_result) -> str`.

**Design decision:** put `ExecResult` in `script_executor.py` and have `agent.py` import it. Since importing `script_executor` imports FreeCAD, that would break pure-python tests. **Resolution:** define `ExecResult` in its own import-safe module `freecad/llm_copilot/types.py` (no FreeCAD/Qt imports); both `agent.py` and `script_executor.py` import it from there.

The agent injects `client`, `inspector` (callable `app->str`), `executor` (object with `run(app, script)->ExecResult` and `undo(app)`), `app`, and `settings`. `on_intent(intent)->bool` is the approval gate (return True to proceed). `on_result(exec_result, snapshot)` notifies the UI. Loop rules:
- Prepend current snapshot to the user message each turn.
- If proposal is not a tool call → return `proposal.text` (done).
- If tool call → gate: if `confirm_before_running` and not `auto_approve_loop`, call `on_intent`; if it returns False, stop and return a "cancelled" note.
- Execute; on error, feed the traceback back and let the model retry up to `self_correction_attempts` times.
- Count each executed step; stop after `max_auto_approved_steps` executed steps in one `send`, returning a "paused" summary.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_agent.py
from freecad.llm_copilot.agent import Agent
from freecad.llm_copilot.types import ExecResult
from freecad.llm_copilot.llm_client import LLMProposal
from freecad.llm_copilot.settings import Settings

class FakeClient:
    def __init__(self, proposals): self.proposals, self.calls = proposals, []
    def complete(self, messages, settings):
        self.calls.append(messages)
        return self.proposals.pop(0)

class FakeExecutor:
    def __init__(self, results): self.results, self.undos = results, 0
    def run(self, app, script): return self.results.pop(0)
    def undo(self, app): self.undos += 1

def _settings(**kw):
    base = dict(model="m", api_key="", api_base="", confirm_before_running=True,
                auto_approve_loop=False, max_auto_approved_steps=5,
                self_correction_attempts=3)
    base.update(kw); return Settings(**base)

def _agent(client, executor, settings, snapshot="DOC"):
    return Agent(client=client, inspector=lambda app: snapshot,
                 executor=executor, app=object(), settings=settings)

def test_single_step_then_done():
    client = FakeClient([
        LLMProposal("add a box", "import Part", "", True),
        LLMProposal("", "", "Done — added a box.", False),
    ])
    ex = FakeExecutor([ExecResult(True, "", "")])
    intents = []
    out = _agent(client, ex, _settings()).send(
        "make a box", on_intent=lambda i: intents.append(i) or True,
        on_result=lambda r, s: None)
    assert intents == ["add a box"]
    assert "Done" in out

def test_gate_rejection_stops_without_executing():
    client = FakeClient([LLMProposal("add a box", "import Part", "", True)])
    ex = FakeExecutor([])
    out = _agent(client, ex, _settings()).send(
        "make a box", on_intent=lambda i: False, on_result=lambda r, s: None)
    assert "cancel" in out.lower()

def test_self_correction_on_error_then_success():
    client = FakeClient([
        LLMProposal("add a box", "bad", "", True),
        LLMProposal("fix it", "good", "", True),
        LLMProposal("", "", "Fixed.", False),
    ])
    ex = FakeExecutor([ExecResult(False, "", "Traceback: NameError"),
                       ExecResult(True, "", "")])
    out = _agent(client, ex, _settings(auto_approve_loop=True)).send(
        "box", on_intent=lambda i: True, on_result=lambda r, s: None)
    assert "Fixed" in out
    # the retry message must carry the traceback back to the model
    assert any("NameError" in str(m) for m in client.calls[-1])

def test_gives_up_after_self_correction_attempts():
    props = [LLMProposal("try", "bad", "", True) for _ in range(3)]
    client = FakeClient(props)
    ex = FakeExecutor([ExecResult(False, "", "err")] * 3)
    out = _agent(client, ex, _settings(self_correction_attempts=3,
                                       auto_approve_loop=True)).send(
        "box", on_intent=lambda i: True, on_result=lambda r, s: None)
    assert "couldn't" in out.lower() or "could not" in out.lower()

def test_stops_at_max_auto_approved_steps():
    # every proposal is a successful tool call -> would loop forever without cap
    props = [LLMProposal(f"s{i}", "ok", "", True) for i in range(10)]
    client = FakeClient(props)
    ex = FakeExecutor([ExecResult(True, "", "")] * 10)
    out = _agent(client, ex, _settings(auto_approve_loop=True,
                                       max_auto_approved_steps=2)).send(
        "box", on_intent=lambda i: True, on_result=lambda r, s: None)
    assert "pause" in out.lower() or "continue" in out.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_agent.py -v`
Expected: FAIL — `freecad.llm_copilot.types` / `agent` missing.

- [ ] **Step 3: Write the shared types module**

```python
# freecad/llm_copilot/types.py
from dataclasses import dataclass

@dataclass
class ExecResult:
    ok: bool
    output: str
    error: str
```

- [ ] **Step 4: Write the agent**

```python
# freecad/llm_copilot/agent.py
class Agent:
    def __init__(self, client, inspector, executor, app, settings):
        self.client = client
        self.inspector = inspector
        self.executor = executor
        self.app = app
        self.settings = settings
        self.messages = []

    def send(self, user_message, on_intent, on_result) -> str:
        snap = self.inspector(self.app)
        self.messages.append({
            "role": "user",
            "content": f"[document snapshot]\n{snap}\n\n[request]\n{user_message}",
        })
        executed_steps = 0
        retries = 0
        while True:
            proposal = self.client.complete(self.messages, self.settings)
            if not proposal.is_tool_call:
                self.messages.append({"role": "assistant", "content": proposal.text})
                return proposal.text

            # record the assistant's tool intent in history
            self.messages.append(
                {"role": "assistant",
                 "content": f"(intent) {proposal.intent}\n(script)\n{proposal.script}"})

            gate_needed = (self.settings.confirm_before_running
                           and not self.settings.auto_approve_loop)
            if gate_needed and not on_intent(proposal.intent):
                note = "Cancelled before running."
                self.messages.append({"role": "user", "content": note})
                return note

            result = self.executor.run(self.app, proposal.script)
            new_snap = self.inspector(self.app)
            on_result(result, new_snap)

            if result.ok:
                executed_steps += 1
                retries = 0
                self.messages.append({
                    "role": "user",
                    "content": f"[executed OK]\n[new snapshot]\n{new_snap}",
                })
                if executed_steps >= self.settings.max_auto_approved_steps:
                    summary = ("Paused after reaching the step limit "
                               f"({self.settings.max_auto_approved_steps}). "
                               "Tell me to continue if this looks right.")
                    self.messages.append({"role": "assistant", "content": summary})
                    return summary
                continue

            # error path
            retries += 1
            if retries >= self.settings.self_correction_attempts:
                summary = ("I couldn't complete this after "
                           f"{retries} attempts. Last error:\n{result.error}")
                self.messages.append({"role": "assistant", "content": summary})
                return summary
            self.messages.append({
                "role": "user",
                "content": f"[script failed]\n{result.error}\nPlease fix and try again.",
            })
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_agent.py -v`
Expected: PASS (5 passed)

- [ ] **Step 6: Commit**

```bash
git add freecad/llm_copilot/types.py freecad/llm_copilot/agent.py tests/test_agent.py
git commit -m "feat: agent inspect/act/read-back loop with gate, retries, step cap"
```

---

### Task 4: Document inspector (FreeCAD adapter)

**Files:**
- Create: `freecad/llm_copilot/document_inspector.py`
- Test: `tests/integration/test_freecad_adapters.py` (inspector cases)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `snapshot(app) -> str`. Returns `"NO_ACTIVE_DOCUMENT"` when `app.ActiveDocument is None`. Otherwise a text block listing document name and, per object: `Name`, `TypeId`, `Label`, and — when present — `Shape` bounding box (`obj.Shape.BoundBox`) and the current GUI selection.

`app` is the `FreeCAD` module (injected so tests pass the real module under `freecadcmd`). Selection is read via `FreeCADGui.Selection.getSelection()` guarded by try/except (GUI may be absent headless).

- [ ] **Step 1: Write the failing integration test**

```python
# tests/integration/test_freecad_adapters.py  (run under freecadcmd)
import unittest
import FreeCAD as App
from freecad.llm_copilot import document_inspector as di

class InspectorTests(unittest.TestCase):
    def tearDown(self):
        for d in list(App.listDocuments()):
            App.closeDocument(d)

    def test_no_active_document(self):
        self.assertEqual(di.snapshot(App), "NO_ACTIVE_DOCUMENT")

    def test_lists_objects_with_bbox(self):
        doc = App.newDocument("T")
        box = doc.addObject("Part::Box", "Box")
        doc.recompute()
        snap = di.snapshot(App)
        self.assertIn("Box", snap)
        self.assertIn("Part::Box", snap)
        self.assertIn("BoundBox", snap)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `freecadcmd -c "import unittest; unittest.main(module=None, argv=['x','discover','-s','tests/integration'])"`
(Or the repo's documented headless runner — see Task 7.)
Expected: FAIL — `document_inspector` missing.

- [ ] **Step 3: Write the implementation**

```python
# freecad/llm_copilot/document_inspector.py
def _selection_names():
    try:
        import FreeCADGui as Gui
        return [o.Name for o in Gui.Selection.getSelection()]
    except Exception:
        return []

def snapshot(app) -> str:
    doc = getattr(app, "ActiveDocument", None)
    if doc is None:
        return "NO_ACTIVE_DOCUMENT"
    lines = [f"Document: {doc.Name}"]
    for obj in doc.Objects:
        parts = [f"- {obj.Name} (TypeId={obj.TypeId}, Label={obj.Label!r})"]
        shape = getattr(obj, "Shape", None)
        bb = getattr(shape, "BoundBox", None) if shape is not None else None
        if bb is not None:
            parts.append(
                f"    BoundBox: ({bb.XMin:.2f},{bb.YMin:.2f},{bb.ZMin:.2f})"
                f"->({bb.XMax:.2f},{bb.YMax:.2f},{bb.ZMax:.2f})")
        lines.append("\n".join(parts))
    sel = _selection_names()
    if sel:
        lines.append(f"Selected: {', '.join(sel)}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run the headless suite (Task 7 runner). Expected: inspector cases PASS.

- [ ] **Step 5: Commit**

```bash
git add freecad/llm_copilot/document_inspector.py tests/integration/test_freecad_adapters.py
git commit -m "feat: document inspector snapshot"
```

---

### Task 5: Script executor (FreeCAD adapter)

**Files:**
- Create: `freecad/llm_copilot/script_executor.py`
- Modify: `tests/integration/test_freecad_adapters.py` (add executor cases)

**Interfaces:**
- Consumes: `ExecResult` from `freecad/llm_copilot/types.py` (Task 3).
- Produces: `run(app, script) -> ExecResult` and `undo(app) -> None`. `run` opens one transaction, execs the script with `App.ActiveDocument` reachable, recomputes, commits; on any exception it aborts the transaction and returns `ok=False` with the traceback. Stdout is captured.

The script executes with globals providing `App`, `FreeCAD`, and (if available) `Part`. `undo(app)` calls `app.ActiveDocument.undo()`.

- [ ] **Step 1: Write the failing integration tests**

```python
# add to tests/integration/test_freecad_adapters.py
import FreeCAD as App
from freecad.llm_copilot import script_executor as se

class ExecutorTests(unittest.TestCase):
    def setUp(self): self.doc = App.newDocument("E")
    def tearDown(self):
        for d in list(App.listDocuments()): App.closeDocument(d)

    def test_run_creates_object_in_one_transaction(self):
        r = se.run(App, "App.ActiveDocument.addObject('Part::Box','B')")
        self.assertTrue(r.ok, r.error)
        self.assertIsNotNone(self.doc.getObject("B"))
        se.undo(App)  # single undo removes it
        self.assertIsNone(self.doc.getObject("B"))

    def test_error_aborts_and_reports(self):
        before = len(self.doc.Objects)
        r = se.run(App, "raise ValueError('boom')")
        self.assertFalse(r.ok)
        self.assertIn("boom", r.error)
        self.assertEqual(len(self.doc.Objects), before)  # nothing half-applied

    def test_stdout_captured(self):
        r = se.run(App, "print('hello-from-script')")
        self.assertTrue(r.ok)
        self.assertIn("hello-from-script", r.output)
```

- [ ] **Step 2: Run to verify failure** — executor missing. FAIL.

- [ ] **Step 3: Write the implementation**

```python
# freecad/llm_copilot/script_executor.py
import io
import traceback
import contextlib
from .types import ExecResult

def run(app, script: str) -> "ExecResult":
    doc = app.ActiveDocument
    if doc is None:
        return ExecResult(False, "", "NO_ACTIVE_DOCUMENT")
    g = {"App": app, "FreeCAD": app}
    try:
        import Part
        g["Part"] = Part
    except Exception:
        pass
    doc.openTransaction("LLM Copilot")
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(script, "<llm_script>", "exec"), g)
        doc.recompute()
        doc.commitTransaction()
        return ExecResult(True, buf.getvalue(), "")
    except Exception:
        doc.abortTransaction()
        return ExecResult(False, buf.getvalue(), traceback.format_exc())

def undo(app) -> None:
    doc = app.ActiveDocument
    if doc is not None:
        doc.undo()
```

- [ ] **Step 4: Run headless suite.** Expected: executor cases PASS.

- [ ] **Step 5: Commit**

```bash
git add freecad/llm_copilot/script_executor.py tests/integration/test_freecad_adapters.py
git commit -m "feat: transactional script executor with undo and stdout capture"
```

---

### Task 6: Chat panel + workbench wiring

**Files:**
- Create: `freecad/llm_copilot/chat_panel.py`
- Create: `freecad/llm_copilot/init_gui.py`
- Test: manual smoke (documented steps below) — no automated test; this layer is thin and Qt/GUI-bound.

**Interfaces:**
- Consumes: `Agent` (Task 3), `complete` (Task 2, wrapped into a small client object), `snapshot` (Task 4), `script_executor` module (Task 5), `load_settings`/`save_settings` (Task 1).
- Produces: `LLMCopilotWorkbench` (registered via `Gui.addWorkbench`) and a `CopilotDockWidget(QDockWidget)` added to the main window.

The panel builds an `Agent` with: a client adapter exposing `.complete(messages, settings)` that calls `llm_client.complete`; `inspector=document_inspector.snapshot`; an executor object = the `script_executor` module (it already has `run`/`undo`); `app=FreeCAD`; `settings=load_settings(...)`. Sending runs `agent.send` on a worker thread so the UI stays responsive; `on_intent` shows a modal-free inline approve/reject that blocks the worker via a thread-safe event; `on_result` posts the new snapshot/intent to the log and offers an Undo button that calls `script_executor.undo`.

- [ ] **Step 1: Write the chat panel**

```python
# freecad/llm_copilot/chat_panel.py
import threading
from PySide import QtGui, QtCore
import FreeCAD
import FreeCADGui as Gui

from . import document_inspector, script_executor, llm_client
from .agent import Agent
from .settings import load_settings

PARAM_PATH = "User parameter:BaseApp/Preferences/LLMCopilot"

class _Client:
    def complete(self, messages, settings):
        return llm_client.complete(messages, settings)

class CopilotDockWidget(QtGui.QDockWidget):
    resultReady = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__("LLM Copilot", parent)
        self.setObjectName("LLMCopilotDock")
        body = QtGui.QWidget()
        layout = QtGui.QVBoxLayout(body)
        self.log = QtGui.QTextEdit(); self.log.setReadOnly(True)
        self.input = QtGui.QLineEdit()
        self.send_btn = QtGui.QPushButton("Send")
        self.undo_btn = QtGui.QPushButton("Undo last change")
        layout.addWidget(self.log)
        layout.addWidget(self.input)
        layout.addWidget(self.send_btn)
        layout.addWidget(self.undo_btn)
        self.setWidget(body)
        self.send_btn.clicked.connect(self._on_send)
        self.undo_btn.clicked.connect(self._on_undo)
        self.resultReady.connect(self._append)
        self._build_agent()

    def _build_agent(self):
        settings = load_settings(FreeCAD.ParamGet(PARAM_PATH))
        self.agent = Agent(client=_Client(),
                           inspector=document_inspector.snapshot,
                           executor=script_executor,
                           app=FreeCAD, settings=settings)

    def _append(self, text):
        self.log.append(text)

    def _on_send(self):
        msg = self.input.text().strip()
        if not msg:
            return
        self.input.clear()
        self._append(f"<b>You:</b> {msg}")
        self._build_agent()  # pick up latest settings each send

        def on_intent(intent):
            # Ask on the GUI thread; block worker until answered.
            answer = {}
            done = threading.Event()
            def ask():
                res = QtGui.QMessageBox.question(
                    self, "Run this change?", intent,
                    QtGui.QMessageBox.Yes | QtGui.QMessageBox.No)
                answer["ok"] = (res == QtGui.QMessageBox.Yes)
                done.set()
            QtCore.QTimer.singleShot(0, ask)
            done.wait()
            return answer.get("ok", False)

        def on_result(result, snap):
            status = "OK" if result.ok else f"ERROR: {result.error.splitlines()[-1]}"
            self.resultReady.emit(f"<i>step: {status}</i>")

        def work():
            try:
                out = self.agent.send(msg, on_intent, on_result)
            except Exception as e:
                out = f"Error: {e}"
            self.resultReady.emit(f"<b>Copilot:</b> {out}")

        threading.Thread(target=work, daemon=True).start()

    def _on_undo(self):
        script_executor.undo(FreeCAD)
        self._append("<i>Undid last change.</i>")

_dock = None

def toggle_panel():
    global _dock
    mw = Gui.getMainWindow()
    if _dock is None:
        _dock = CopilotDockWidget(mw)
        mw.addDockWidget(QtCore.Qt.RightDockWidgetArea, _dock)
    else:
        _dock.setVisible(not _dock.isVisible())
```

- [ ] **Step 2: Write the workbench + command**

```python
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
        Gui.addCommand("LLMCopilot_TogglePanel", _TogglePanelCommand())
        self.appendToolbar("LLM Copilot", ["LLMCopilot_TogglePanel"])
        self.appendMenu("LLM Copilot", ["LLMCopilot_TogglePanel"])

    def Activated(self):
        from freecad.llm_copilot.chat_panel import toggle_panel
        toggle_panel()

    def GetClassName(self):
        return "Gui::PythonWorkbench"

Gui.addWorkbench(LLMCopilotWorkbench())
```

- [ ] **Step 3: Manual smoke test**

Symlink the repo into FreeCAD's `Mod` dir, launch FreeCAD, switch to the "LLM Copilot" workbench, set a model+key via the parameter editor (Tools → Edit parameters → `BaseApp/Preferences/LLMCopilot`), click the toolbar button, type "make a 10mm cube", confirm the intent dialog, and verify a box appears and "Undo last change" removes it.

Document this in README (Task 8).

- [ ] **Step 4: Commit**

```bash
git add freecad/llm_copilot/chat_panel.py freecad/llm_copilot/init_gui.py
git commit -m "feat: chat panel dock widget and workbench registration"
```

---

### Task 7: Headless test runner + CI wiring

**Files:**
- Create: `tests/integration/run_headless.py`
- Create: `tests/integration/__init__.py`
- Create: `tests/__init__.py`
- Modify: `README.md` (add test instructions — folded into Task 8 if not yet created)

**Interfaces:**
- Consumes: the integration test module (Tasks 4–5).
- Produces: a script runnable as `freecadcmd tests/integration/run_headless.py` that discovers and runs the integration suite and exits non-zero on failure.

- [ ] **Step 1: Write the runner**

```python
# tests/integration/run_headless.py
import os, sys, unittest

# Ensure repo root on path so `import freecad.llm_copilot...` works.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

loader = unittest.TestLoader()
suite = loader.discover(os.path.join(ROOT, "tests", "integration"),
                        pattern="test_*.py")
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
```

Create empty `tests/__init__.py` and `tests/integration/__init__.py`.

- [ ] **Step 2: Run both test tiers**

Run: `pytest tests/test_settings.py tests/test_llm_client.py tests/test_agent.py -v`
Expected: all PASS.

Run: `freecadcmd tests/integration/run_headless.py`
Expected: all integration tests PASS, exit 0. (If `freecadcmd` is unavailable in the environment, document that these run in CI/locally with FreeCAD installed and note it in the commit message.)

- [ ] **Step 3: Commit**

```bash
git add tests/__init__.py tests/integration/__init__.py tests/integration/run_headless.py
git commit -m "test: headless FreeCAD integration runner"
```

---

### Task 8: Dependency install strategy + README

**Files:**
- Create: `README.md`
- Create: `freecad/llm_copilot/deps.py`
- Modify: `freecad/llm_copilot/init_gui.py` (call dependency check on load)
- Test: `tests/test_deps.py` (pytest)

**Interfaces:**
- Consumes: nothing.
- Produces: `ensure_litellm() -> bool` returning True if `litellm` importable, else False (and logging guidance). Never auto-pip-installs silently; it reports the missing dependency so the panel can show a clear message.

**Rationale (from spec packaging note + Addon Academy):** `litellm` is not on the Addon Manager python allow-list and has a large dependency tree, so we do not declare it as an auto-installed `<depend>`. v1 strategy: detect-and-instruct. The README documents `pip install -r requirements.txt` into FreeCAD's Python. A future task may vendor a slimmer client.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deps.py
import builtins
import freecad.llm_copilot.deps as deps

def test_ensure_reports_true_when_importable(monkeypatch):
    monkeypatch.setattr(deps, "_can_import", lambda name: True)
    assert deps.ensure_litellm() is True

def test_ensure_reports_false_when_missing(monkeypatch):
    monkeypatch.setattr(deps, "_can_import", lambda name: False)
    assert deps.ensure_litellm() is False
```

- [ ] **Step 2: Run to verify failure.** FAIL — module missing.

- [ ] **Step 3: Write the implementation**

```python
# freecad/llm_copilot/deps.py
import importlib

GUIDANCE = (
    "LLM Copilot requires the 'litellm' package. Install it into FreeCAD's "
    "Python environment, e.g.:\n"
    "    <freecad-python> -m pip install -r requirements.txt\n"
    "then restart FreeCAD."
)

def _can_import(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:
        return False

def ensure_litellm() -> bool:
    if _can_import("litellm"):
        return True
    try:
        import FreeCAD
        FreeCAD.Console.PrintWarning(GUIDANCE + "\n")
    except Exception:
        print(GUIDANCE)
    return False
```

- [ ] **Step 4: Wire into init_gui.py** — add near the top of `LLMCopilotWorkbench.Initialize`:

```python
        from freecad.llm_copilot.deps import ensure_litellm
        ensure_litellm()
```

- [ ] **Step 5: Write README.md**

Cover: what it is; provider-agnostic via LiteLLM with example model strings; install (Addon Manager + `pip install -r requirements.txt` into FreeCAD's Python); configuring model/key/base URL and the autonomy settings under `BaseApp/Preferences/LLMCopilot` with their defaults; the manual smoke-test flow from Task 6; how to run both test tiers from Task 7; the outcome-based review model (intent + visual keep/undo, code hidden). Include a safety note that the copilot executes generated Python against the active document and that every step is a single undo.

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_deps.py -v`
Expected: PASS (2 passed). Then run the full pytest set to confirm nothing regressed.

- [ ] **Step 7: Commit**

```bash
git add README.md freecad/llm_copilot/deps.py freecad/llm_copilot/init_gui.py tests/test_deps.py
git commit -m "feat: dependency check with guidance and project README"
```

---

## Self-Review

**Spec coverage:**
- Workbench + dockable chat panel → Tasks 1, 6. ✓
- Provider-agnostic via LiteLLM → Task 2. ✓
- inspect → propose → execute → read-back loop → Task 3 (orchestration), 4 (inspect), 5 (act). ✓
- Outcome-based review (intent + visual keep/undo, no code surface) → Task 6 (intent dialog, Undo button; no code shown). ✓
- Component boundaries (only panel=Qt, only inspector/executor=FreeCAD, agent/client pure) → enforced by file split + Global Constraints; verified by pure-pytest tests for agent/client/settings/deps. ✓
- Single transaction per step / clean undo → Task 5 (+ test `test_run_creates_object_in_one_transaction`). ✓
- Settings with exact defaults → Task 1 (+ tests). ConfirmBeforeRunning, AutoApproveLoop, MaxAutoApprovedSteps=5, SelfCorrectionAttempts=3. ✓
- Error handling: script error → abort + self-correct bounded (Task 3 `test_gives_up...`, Task 5 `test_error_aborts...`); no active document (Task 4 `test_no_active_document`, Task 5 guard); LLM/network failure surfaced (Task 6 `work()` try/except); missing config guidance (Task 8); runaway-loop cap as setting (Task 3 `test_stops_at_max...`). ✓
- Testing plan (pure pytest brain + headless FreeCAD adapters) → Tasks 1–5, 7. ✓
- Packaging note (LiteLLM bundling) → Task 8 (detect-and-instruct strategy, documented rationale). ✓

**Placeholder scan:** No TBD/TODO/"add error handling" placeholders; every code step has complete code. ✓

**Type consistency:** `Settings` fields consistent across Tasks 1/2/3/6. `ExecResult(ok, output, error)` defined in `types.py` (Task 3) and used identically in Tasks 3/5/6. `LLMProposal(intent, script, text, is_tool_call)` consistent Tasks 2/3. `snapshot(app)`, `run(app, script)`, `undo(app)` consistent Tasks 3/4/5/6. `Agent.send(user_message, on_intent, on_result)` consistent Tasks 3/6. ✓
