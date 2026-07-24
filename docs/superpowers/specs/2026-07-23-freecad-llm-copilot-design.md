# FreeCAD LLM Copilot — Design

**Date:** 2026-07-23
**Status:** Implemented

> **Amendment (during implementation): LLM client is stdlib-only, not LiteLLM.**
> The original design specified LiteLLM as the provider abstraction. During
> implementation we found LiteLLM (and every comparable library — aisuite, the
> anthropic SDK) depends on packages absent from FreeCAD's bundled Python
> (`httpx`/`requests`, compiled `pydantic-core`/`jiter`) and not on the Addon
> Manager allow-list. We replaced it with a zero-dependency client using only
> `urllib` + `json`, with two adapters: OpenAI-compatible (OpenAI, Ollama,
> OpenRouter) and Anthropic-native (`/v1/messages`). Provider is selected by the
> model-string prefix. This keeps the exact `complete(messages, settings) ->
> LLMProposal` interface, so the agent loop is unchanged, and makes the addon
> install-free on every FreeCAD. The "Packaging Note" below is therefore moot.

## Summary

A FreeCAD plugin that lets a provider-agnostic, externally-hosted LLM act as a
CAD copilot. The user describes what they want in plain language; the LLM
inspects the active document, proposes and executes FreeCAD Python, and the user
reviews the result by its **visual impact on the model** — not by reading code.

Delivered as a lightweight **FreeCAD Workbench** (installable via the Addon
Manager) contributing a single dockable **chat panel**.

## Core Loop

**inspect → propose → execute → read-back**, governed by user-configurable
autonomy settings.

1. **User types intent** in the chat panel.
2. **Inspect** — read the active document into a structured text snapshot
   (object tree, types, key parameters, bounding boxes, current selection),
   injected into LLM context so code is grounded in what actually exists.
3. **Propose** — send conversation + snapshot + tool definitions to the LLM
   (via LiteLLM). LLM returns **plain-language intent** + a Python script (as a
   `run_freecad_script` tool call).
4. **Approval gate** (see settings) — plain-language intent is shown; behavior
   depends on "Confirm before running".
5. **Act** — execute the script inside FreeCAD wrapped in a single transaction,
   `recompute()`, capture success/traceback.
6. **Read-back** — re-inspect; feed result (or error) back to the LLM, which
   reports done or self-corrects (looping to step 3, bounded).
7. **Keep / Undo** — user judges the visible result; "Undo" cleanly rolls back
   the transaction. Outcome is reported back into the conversation.

## Review Model

Review is **outcome-based, not code-based.** FreeCAD users are CAD designers,
not Python developers; generated code is meaningless noise to them. They see:

- Plain-language **intent** before/around execution.
- The **visual result** in the 3D viewport.
- **Keep / Undo** controls (one transaction = one clean undo).

Raw code may live behind an optional advanced/debug disclosure for power users,
but is never the primary review surface.

## Components

Boundaries are drawn so the hard logic is testable without launching FreeCAD.
Only `chat_panel` touches Qt; only `document_inspector` / `script_executor`
touch the FreeCAD API.

- **`llm_client`** — Wraps LiteLLM. Provider-agnostic; reads model/key/base-URL
  from settings. Sends conversation + tool definitions, parses tool calls. No
  FreeCAD, no Qt.
- **`document_inspector`** — Reads the active document into a structured text
  description for the LLM. Pure read; FreeCAD API, no Qt.
- **`script_executor`** — Runs LLM-generated Python inside FreeCAD wrapped in a
  single transaction (`openTransaction`/`commitTransaction`, abort on error),
  `recompute()`s, captures output/errors. FreeCAD API, no Qt.
- **`agent`** — Orchestrates the loop; holds conversation state; enforces the
  approval gate, step cap, and self-correction bound. Plain Python — uses
  inspector/executor/llm_client, touches neither Qt nor FreeCAD directly.
- **`chat_panel`** — Qt/PySide dock widget. The only UI. Shows intent,
  keep/undo controls, history. Talks only to `agent`.
- **`settings`** — Stored in FreeCAD's parameter system.
- **`workbench`** — Registration/scaffolding; loads the plugin, toggles the panel.

## Settings

| Setting | Default | Purpose |
|---|---|---|
| Model | — | e.g. `anthropic/claude-opus-4-8`, `openai/gpt-5.4`, `ollama/llama3` |
| API key / base URL | — | Provider credentials/endpoint |
| Confirm before running | on | On: show intent and wait for go-ahead before executing. Off: execute immediately (keep/undo still governs result). |
| Auto-approve loop steps | off | On: chain multiple inspect→act→read-back steps without stopping between them. |
| Max auto-approved steps | 5 | Hard cap on consecutive auto-approved steps; on hit, stop and hand back to user in plain language. |
| Self-correction attempts | 3 | Bounded retries when a script errors before surfacing failure. |

## Error Handling

- **Script errors** — traceback captured, transaction aborted (never a
  half-applied change); error returned to LLM for bounded self-correction, then
  a plain-language "I couldn't complete this."
- **No active document** — inspector reports it; agent offers to create one.
- **LLM/network failures** — rely on LiteLLM retries/fallbacks; final failure
  shows a clear message, conversation state preserved for retry.
- **Missing/invalid config** — panel prompts the user to open settings rather
  than failing mid-request.
- **Runaway loops** — "Max auto-approved steps" cap (setting) stops endless
  looping.

## Testing

- **`agent`** — core loop, tested with fake llm_client/inspector/executor.
  Covers: single-step happy path, multi-step chaining, hitting the step cap,
  self-correction on error, giving up after N attempts, both gate modes. No
  FreeCAD needed.
- **`llm_client`** — mocked LiteLLM; verifies request shaping + tool-call parsing.
- **`document_inspector` / `script_executor`** — headless FreeCAD integration
  suite (`freecadcmd`) against throwaway documents: inspect known doc → assert
  snapshot; run known script → assert geometry + clean transaction/undo.
- **`chat_panel` / `workbench`** — thin; manual smoke + light Qt instantiation.

## Packaging Note

LiteLLM has a moderate dependency footprint that must be bundled into FreeCAD's
Python environment. The implementation plan must address how dependencies are
vendored/installed so the Addon Manager install works cleanly.
