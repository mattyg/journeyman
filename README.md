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

## Provider-agnostic, zero dependencies

The copilot talks to models over plain HTTPS using only the Python standard
library — there is **nothing to pip-install** and nothing vendored. You choose
a provider by setting a single model string in settings; the provider is taken
from the prefix. Four providers are supported:

| Model string example                       | Provider   | API used                          |
|--------------------------------------------|------------|-----------------------------------|
| `openai/gpt-5.4`                           | OpenAI     | OpenAI `/v1/chat/completions`     |
| `openrouter/anthropic/claude-opus-4-8`     | OpenRouter | OpenAI-compatible                 |
| `ollama/llama3`                            | Ollama     | OpenAI-compatible (set `ApiBase`) |
| `anthropic/claude-opus-4-8`                | Anthropic  | native `/v1/messages`             |

Anything without a recognized prefix is treated as an OpenAI-compatible
endpoint, so other gateways work too by pointing `ApiBase` at them.

For **Ollama** (local), set `ApiBase` to your server's OpenAI-compatible URL,
e.g. `http://localhost:11434/v1`, and leave `ApiKey` empty.

## Installation

Install the workbench through the **FreeCAD Addon Manager** as usual. That's
the whole install — because the LLM client is standard-library-only, there is
no separate dependency step, no `pip install`, and no compiled wheels. It works
on any FreeCAD build, including immutable/Nix installs.

## Configuration

Settings live under the parameter path:

```
User parameter:BaseApp/Preferences/LLMCopilot
```

Open them via **Tools -> Edit parameters** in FreeCAD and navigate to that
group. Available settings and their defaults:

| Setting                | Default | Meaning                                                              |
|-------------------------|---------|------------------------------------------------------------------------|
| `Model`                 | (empty) | Prefixed model string, e.g. `anthropic/claude-opus-4-8` (see providers above) |
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
