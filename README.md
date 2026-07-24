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
- **Reasoning** (providers that support it) — Off / Low / Medium / High. When the
  model returns reasoning, the chat shows a collapsible **Thinking** line you can
  expand to read it. Higher effort can improve results but is slower/costlier;
  not all models expose reasoning even when requested.
- **Autonomy** — *Confirm intent before running* (default on), *Auto-approve
  consecutive steps* (default off), *Max auto-approved steps* (default 5),
  *Self-correction attempts* (default 3).
- **Conversation** — *Persist chat history in FreeCAD documents* (default on).
  Disable it to prevent embedded history from loading or updating. **Clear
  context** removes previously embedded history regardless of this preference.
  When the model needs a consequential clarification, it can present a
  persistent multiple-choice card with two to five options, descriptions, a
  recommended choice, and optional multi-selection. Choosing an option resumes
  the same agent turn; the question and answer remain in document history.
- **Self-checking harness** — seven independent experiment switches:
  *Validate geometry*, *Structured before/after diff*, *Require verification
  evidence*, *Read-only inspection tool*, *Rendered views*, *Rollback failed
  validation*, and *Rich document state*. Rendered views default off because
  they require an image-capable model; the other checks default on.
  Three image-rendering experiments—*Technical edge overlay*, *Stable colors
  for separate objects*, and *Depth-enhanced multi-light shading*—default on.
  *Installed-version FreeCAD API lookup* is one consolidated preference. It
  gives the model safe, bounded access to public module members, signatures,
  docstrings, the installed version number, and a bundled workflow field guide.
  Relevant `AttributeError` and `TypeError` failures trigger the same lookup
  automatically before the model retries.
  When rendered views are enabled, **Image capture** chooses *Global views
  only*, *Changed elements only*, or *Global + changed elements*. **Max isolated
  elements** limits the number of independently rendered changed objects.
- **Structured CAD workflow** — six independent switches for *Structured design
  planning*, *Parametric feature preference*, *Sketch constraint checking*,
  *Stage-order guidance*, *Design-ledger context*, and *Final design review*.
  These default on. The model declares its construction strategy, ordered
  feature plan, current stage, active plan step, and measurable success criteria
  before editing. The approval dialog includes that plan.

When enabled, the harness checks shapes and feature state after execution,
reports topology, dimensions, volume, dependencies and changed properties,
and asks the model to correct silent failures. Validation can abort the active
transaction before an invalid result is committed. A model cannot finish a
changed task until it returns concrete verification evidence. The inspection
tool provides detailed state without modifying the document or requesting an
execution approval. Rendered-view mode supplies one contact sheet containing
front, back, left, right, top, bottom, and isometric views after successful
changes, and can add equivalent contact sheets for changed final objects.
Images are software-rendered from tessellated shapes: the visible camera and
object visibility are never changed, and capture works under `freecadcmd`
without an active 3D view or OpenGL context. PartDesign features are represented
by their containing Body so intermediate features do not produce redundant
images. The default technical-illustration style uses a warm neutral background,
depth-tested dark feature edges, deterministic muted colors for distinct
objects, three-direction lighting, and subtle darkening of recessed geometry.
Each script result also includes Python standard error and execution-scoped
warnings/errors emitted through `FreeCAD.Console`. They remain visible in
FreeCAD while also being shown in the expandable script result and returned to
the model. Native messages that bypass both Python and standard error cannot be
captured by FreeCAD's headless Python API.

Structured workflow mode guides the model through analysis, sketch/base
construction, additive features, subtractive features, finishing, and
verification while allowing inapplicable stages to be skipped. It does not
blindly require sketches: Part primitives, surface workflows, inspection, and
careful modification of an existing feature tree remain explicit strategies.
After each successful script, the model receives a compact design ledger and
warnings about unconstrained or unattached Part Design sketches, opaque
features, and operations inconsistent with the declared stage. Completion
requires the model to report that it reviewed the result against the plan and
success criteria. When verification evidence is enabled, that evidence must
also be concrete. A separate verify-stage tool call is not required, avoiding
repeated `finish`/review cycles.

Settings are stored under `BaseApp/Preferences/Mod/LLMCopilot`.

Completion requests use a five-minute timeout to accommodate high-reasoning
models and image-heavy context. If a request still times out, the chat presents
**Retry same request** and **Stop** controls. Retry keeps the existing agent turn
and does not append a duplicate user message; Stop preserves the conversation
so the user can continue later.

## Manual smoke test

1. Symlink this repository into FreeCAD's `Mod` directory (e.g.
   `~/.local/share/FreeCAD/Mod/LLMCopilot`).
2. Launch FreeCAD (the copilot is not a workbench — it loads on startup and is
   available in every workbench).
3. Set your model and API key via **Edit -> Preferences -> LLM Copilot**.
4. Create or open a FreeCAD document. Chat controls remain disabled when no
   document is active.
5. Show the panel via **View -> Panels -> LLM Copilot** (it starts hidden).
6. Type: `make a 10mm cube`.
7. Confirm the proposed intent.
8. Verify a cube appears in the 3D view.
9. Click **Undo last change** and verify the cube is removed.

The panel keeps a separate conversation for each open document. Its context
line shows an approximate token and message count, including the system prompt
and tool definitions. **Clear context** resets only the active document's
conversation and transcript; it does not change or undo the FreeCAD model.
Conversation context and transcript entries are compressed into a hidden
internal object in the FreeCAD document, so saving and reopening the `.FCStd`
restores the chat. Clearing context removes that embedded history.
Each model call also adds a minimized **Context sent to model** entry to the
transcript. Expanding it shows the newly-added system, document, validation,
diff, inspection, and verification messages, with any rendered contact sheets
displayed inline.

Use **Attach images…** to add up to eight PNG, JPEG, WebP, GIF, or BMP files to
a message. Attachments appear as removable previews, are resized to at most
1600 pixels, normalized to PNG, displayed in the transcript/context, and sent
with the user's text. When conversation persistence is enabled, the normalized
image data is embedded with the document history rather than referenced by its
original filesystem path.

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
