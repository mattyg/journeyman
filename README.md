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
| `ollama/llama3`                            | Ollama     | OpenAI-compatible (local host)    |
| `anthropic/claude-opus-4-8`                | Anthropic  | native `/v1/messages`             |

You never type a provider URL. You pick a **Provider** and a **Model** from
dropdowns (see Configuration); the endpoint is built in. For **Ollama** (local)
you set a **Host** instead of an API key.

## Installation

Install the addon through the **FreeCAD Addon Manager** as usual. That's
the whole install — because the LLM client is standard-library-only, there is
no separate dependency step, no `pip install`, and no compiled wheels. It works
on any FreeCAD build, including immutable/Nix installs.

## Configuration

Open **Edit -> Preferences -> LLM Copilot**:

- **Provider** — choose Anthropic, OpenAI, OpenRouter, or Ollama (local).
- **API key** — the key for that provider (kept separately per provider, so you
  can have keys for several at once). Hidden for Ollama.
- **Host** (Ollama only) — your Ollama server, default `http://localhost:11434/v1`.
- **Model** — an editable dropdown. It's filled from a short built-in list and,
  once a key/host is set, refreshed with the provider's live model list; click
  **Refresh** to re-pull. You can also type a model id directly.
- **Autonomy** — *Confirm intent before running* (default on), *Auto-approve
  consecutive steps* (default off), *Max auto-approved steps* (default 5),
  *Self-correction attempts* (default 3).

Settings are stored under `BaseApp/Preferences/Mod/LLMCopilot`.

## Manual smoke test

1. Symlink this repository into FreeCAD's `Mod` directory (e.g.
   `~/.local/share/FreeCAD/Mod/LLMCopilot`).
2. Launch FreeCAD (the copilot is not a workbench — it loads on startup and is
   available in every workbench).
3. Set your model and API key via **Edit -> Preferences -> LLM Copilot**.
4. Show the panel via **View -> Panels -> LLM Copilot** (it starts hidden).
5. Type: `make a 10mm cube`.
6. Confirm the proposed intent.
7. Verify a cube appears in the 3D view.
8. Click **Undo last change** and verify the cube is removed.

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
