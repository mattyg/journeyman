# FreeCAD LLM Copilot

A provider-agnostic AI copilot for FreeCAD. It lets you describe CAD edits in
plain language, then follows an **inspect -> act -> review** loop: it looks
at the current document, proposes a small script to make the change, runs
it, and shows you the outcome.

Review is **outcome-based**, not code-based: you don't see the generated
Python. Instead, for every step you see a plain-language statement of intent
(what the copilot is about to do) and, after it runs, a visual result with a
**Keep** / **Undo last change** choice. The underlying code is intentionally
hidden from the chat surface.

## Provider-agnostic via LiteLLM

The copilot talks to models through [LiteLLM](https://github.com/BerriAI/litellm),
so it works with any provider LiteLLM supports by changing a single model
string in settings, e.g.:

- `anthropic/claude-opus-4-8`
- `openai/gpt-5.4`
- `ollama/llama3`

No provider-specific code lives in the plugin itself.

## Installation

You can install this addon two ways:

1. **FreeCAD Addon Manager** — add/update the workbench itself via the Addon
   Manager as usual.
2. **Install the `litellm` dependency separately.** `litellm` is **not** on
   the Addon Manager's Python package allow-list (and pulls in a fairly
   large dependency tree), so the Addon Manager cannot install it for you.
   You must install it into FreeCAD's own Python environment yourself:

   ```
   <freecad-python> -m pip install -r requirements.txt
   ```

   Then restart FreeCAD. If `litellm` is missing, the workbench will still
   load, but it prints guidance (see `freecad/llm_copilot/deps.py`) telling
   you to run the command above.

## Configuration

Settings live under the parameter path:

```
User parameter:BaseApp/Preferences/LLMCopilot
```

Open them via **Tools -> Edit parameters** in FreeCAD and navigate to that
group. Available settings and their defaults:

| Setting                | Default | Meaning                                                              |
|-------------------------|---------|------------------------------------------------------------------------|
| `Model`                 | (empty) | LiteLLM model string, e.g. `anthropic/claude-opus-4-8`                |
| `ApiKey`                 | (empty) | API key for the selected provider                                     |
| `ApiBase`                | (empty) | Optional custom API base URL (e.g. for a local/self-hosted endpoint)  |
| `ConfirmBeforeRunning`   | `true`  | Require explicit confirmation of intent before each step runs         |
| `AutoApproveLoop`        | `false` | Allow the copilot to run multiple steps in a row without confirmation |
| `MaxAutoApprovedSteps`   | `5`     | Cap on consecutive auto-approved steps when `AutoApproveLoop` is on    |
| `SelfCorrectionAttempts` | `3`     | How many times the copilot retries a failed step before giving up     |

## Manual smoke test

1. Symlink this repository into FreeCAD's `Mod` directory (e.g.
   `~/.local/share/FreeCAD/Mod/freecad-llm-plugin`).
2. Launch FreeCAD.
3. Switch to the **LLM Copilot** workbench from the workbench selector.
4. In the chat panel, set your model and API key (see Configuration above).
5. Click the toolbar button to open/focus the chat panel.
6. Type: `make a 10mm cube`.
7. Confirm the proposed intent.
8. Verify a cube appears in the 3D view.
9. Click **Undo last change** and verify the cube is removed.

## Running the tests

There are two test tiers.

**Pure-Python tests** (agent/client/settings/deps logic, no FreeCAD needed):

```
nix develop -c python3 -m pytest tests/test_*.py -v
```

**Integration tests** (run inside headless FreeCAD, exercise the
FreeCAD-dependent inspector/executor code):

```
nix develop -c freecadcmd tests/integration/run_headless.py
```

## Safety note

The copilot executes LLM-generated Python directly against your active
FreeCAD document. Each proposed step runs as a single transaction, so every
change can be undone with one **Undo last change** click (or FreeCAD's
normal Undo). Always review the stated intent before confirming a step,
especially when `AutoApproveLoop` is enabled.
