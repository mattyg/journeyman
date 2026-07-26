# FreeCAD Journeyman

A provider-agnostic AI assistant for FreeCAD. It lets you describe CAD edits in
plain language, then follows an **inspect -> act -> review** loop: it looks
at the current document, proposes a small script to make the change, runs
it, and shows you the outcome.

Review is **outcome-based**, not code-based: you don't see the generated
Python. Instead, for every step you see a plain-language statement of intent
(what Journeyman is about to do) and, after it runs, a visual result with a
**Keep** / **Undo last change** choice. The underlying code is intentionally
hidden from the chat surface.

## Provider-agnostic, zero dependencies

Journeyman talks to models over plain HTTPS using only the Python standard
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

Open **Edit -> Preferences -> Journeyman**:

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

Settings are stored under `BaseApp/Preferences/Mod/Journeyman`.

Completion requests use a five-minute timeout to accommodate high-reasoning
models and image-heavy context. If a request still times out, the chat presents
**Retry same request** and **Stop** controls. Retry keeps the existing agent turn
and does not append a duplicate user message; Stop preserves the conversation
so the user can continue later.

## Manual smoke test

1. Symlink this repository into FreeCAD's `Mod` directory (e.g.
   `~/.local/share/FreeCAD/Mod/Journeyman`).
2. Launch FreeCAD (Journeyman is not a workbench — it loads on startup and is
   available in every workbench).
3. Set your model and API key via **Edit -> Preferences -> Journeyman**.
4. Create or open a FreeCAD document. Chat controls remain disabled when no
   document is active.
5. Show the panel via **View -> Panels -> Journeyman** (it starts hidden).
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

## Evals

An automated benchmark pipeline replaces the manual run/export/critique loop
when iterating on the agent harness. Scenarios live in `evals/benchmarks/`
and cover create-from-image, create-from-text, and modify-existing tasks.

All eval scripts read credentials and model overrides from a gitignored
`.env` at the repo root. Copy the template and fill in one key:

```
cp .env.example .env      # then set OPENROUTER_API_KEY=sk-or-...
```

Real environment variables take precedence, so `OPENROUTER_API_KEY=... nix
develop -c ...` still overrides the file for one-off runs. Only the evals
use this; the FreeCAD plugin itself reads its key from FreeCAD preferences.

Scenarios are synthesized from the
[gnucleus-ai/cad-gen-freecad](https://huggingface.co/datasets/gnucleus-ai/cad-gen-freecad)
dataset (description + reference render + ground-truth `.FCStd` per part).
Each part expands into a variant matrix spanning the input space — the ground
truth stays constant while the input varies, so grading stays objective:

| variant | starts from | image | tests |
|---|---|---|---|
| `create-img` | empty doc | yes | replica fidelity from a full spec |
| `create-text` | empty doc | no | spec adherence without visuals |
| `image-minimal` | empty doc | yes | proportions and scale assumptions |
| `partial-spec` | empty doc | no | assumption/question behavior |
| `vague` | empty doc | no | sensible defaults from casual phrasing |
| `functional` | empty doc | no | designing from a use case |
| `modify-param` | existing model | no | precise parametric edit |
| `modify-img` | existing model | yes | resolving a visual reference to a feature |
| `modify-underspec` | existing model | no | clarifying before editing |

In `modify-img` the attached picture shows the part **as it currently is**
(the only render available is of the ground truth), so it plays its real-world
role of pointing at *which* feature to change while the change itself is
stated in text. The judge is told this explicitly, since treating the image as
the target would invert the grading. Variant
wordings are generated once by an LLM, reviewed, and committed; they are
never regenerated per run, keeping the benchmark stable across harness
iterations.

```
nix develop -c python3 evals/fetch_dataset.py --rows 10   # cache raw material
nix develop -c python3 evals/synthesize.py                # write scenario matrix
nix develop -c freecadcmd evals/fetch_dataset.py --pass "--measure"
```

`synthesize.py` needs an API key for the wording variants
(`--mechanical-only` skips them); it uses `EVAL_SYNTH_MODEL` (default
Sonnet), deliberately different from the judge's default (Opus).

Run the agent headlessly against scenarios (freecadcmd requires script
arguments inside one quoted `--pass` string):

```
nix develop -c freecadcmd evals/runner.py --pass "--scenario hanger-modify"
nix develop -c freecadcmd evals/runner.py --pass "--prefix disc-spring"
nix develop -c freecadcmd evals/runner.py --pass "--all --repeat 2"
```

Runs use the same harness defaults a user gets on a fresh install (via
`load_settings`, not the `Settings` dataclass defaults, which differ), and
`run.json` records the full configuration so scores stay comparable. Use
`--set name=value` to toggle a feature for an experiment, e.g.
`--set assumption_ledger=true`.

`--prefix <base-part>` runs every variant of one base input — the fastest way
to see how the harness handles the same part across all input styles.
`--variant <name>` narrows any selection to one variant type (e.g.
`--all --variant modify-img` runs that variant across every part). Both are
repeatable and can be combined; the runner prints the selected scenarios
before it starts.

Each attempt writes `evals/runs/<stamp>-<sha>/<scenario>/` with the
transcript, final `.FCStd`, rendered canonical views, and `run.json`
containing deterministic geometry checks (recompute, solid validity,
bounding box/volume vs. expectations, constrained sketches, ground-truth
volume ratio, preserved objects on modify tasks).

Grade runs with the LLM judge and aggregate/compare:

```
nix develop -c python3 evals/judge.py evals/runs/<run-dir>
nix develop -c python3 evals/report.py evals/runs/<run-dir>
nix develop -c python3 evals/report.py --diff evals/runs/<old> evals/runs/<new>
```

The report also aggregates mean scores per input variant across parts, which
is the tuning signal — e.g. "fine on full specs, falls apart on
partial-spec" points at assumption/clarification handling rather than
geometry skills.

The judge scores each attempt 0–10 and classifies issues as
`harness-prompt`, `tool-api`, `model-limitation`, `benchmark-defect`, or
`grader-doubt`;
aggregated harness-prompt issues are the input for the next harness fix
pass, and `--diff` (exit code 1 on regression) guards against a fix for one
scenario regressing another.

## Safety note

Journeyman executes LLM-generated Python directly against your active
FreeCAD document. Each proposed step runs as a single transaction, so every
change can be undone with one **Undo last change** click (or FreeCAD's
normal Undo). Always review the stated intent before confirming a step,
especially when `AutoApproveLoop` is enabled.
