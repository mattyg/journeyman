# Plan: assumption ledger + fidelity + INFERRED marking

## Motivation

A post-mortem of a photo-to-model attempt (see `cad-agent-harness.md`) found the
dominant failure was **unsurfaced numeric guesses**: scale, hole diameter, and
bend angle were guessed and stated as fact, so the human could not correct them
until three rounds later. The loop already implements most of the harness
(feature-tree planning, per-feature verification, headless render-and-look-back
via `view_capture`, topo-naming guards, final verification gate). The three real
gaps are:

1. No mechanism forcing the model to surface numeric assumptions **as** guesses
   before building (highest value — this is the whole point of the harness).
2. No `fidelity_target` (replica / stylised / functional-analogue) — the model
   defaults to replica, wrong for stylised work.
3. No `INFERRED` marking distinguishing observed vs. invented features.

Photo-to-model needs no new mode: `agent.send` already accepts `user_images` and
passes them to the model. It needs exactly these three additions.

All three are **settings-gated**, matching every other workflow switch, so they
default off and don't disturb existing behaviour.

## Design principles to respect

- Only `chat_panel` touches Qt; only `document_inspector`/`script_executor`
  touch FreeCAD; `agent` + `llm_client` + `cad_workflow` + `turn_protocol` stay
  plain testable Python. Keep the ledger logic in the pure layer.
- Reuse existing seams — don't add a parallel gate. The assumption ledger is a
  pre-build gate analogous to `cad_workflow.proposal_issues`, and its persistence
  belongs in the existing `_Turn.ledger` dict rendered by `cad_workflow.ledger_text`.

## Changes by file

### 1. `settings.py`
- Add three fields to the settings dataclass (near line 79, with the other
  workflow switches), all defaulting off:
  - `assumption_ledger: bool = False`
  - `fidelity_target: str = "unspecified"`  (`unspecified|replica|stylised|functional_analogue`)
  - `mark_inferred_features: bool = False`
- Wire each into `load_settings` (GetBool/GetString near line 275) and
  `save_settings` (SetBool/SetString near line 313). `fidelity_target` uses
  `GetString`/`SetString` with default `"unspecified"`.

### 2. `llm_client.py` — system prompt
- Add an `_ASSUMPTION_PROMPT` fragment, appended in `_system_prompt` only when
  `settings.assumption_ledger` is on. Content:
  - On the first `run_freecad_script` of a turn, enumerate every numeric value
    not given by the user as an assumption. Each row has a stable id, parameter
    name, numeric value, unit, source, confidence, consequence-if-wrong, and a
    short description of what would be wrong. Sort high → medium → low
    consequence.
  - A row is blocking when confidence is `low` and consequence is `high`.
    Submit the ledger on the proposed script; the agent will reject that script
    and require `ask_user` before anything executes.
  - Ask at most one clarification round of ≤3 sequential `ask_user` calls.
    Each call uses the existing single-question, 2–5-option schema; put numeric
    choices in the option labels rather than requesting free text. After the
    round, resubmit the script with the same assumption ids and updated
    values/statuses.
  - After the gate accepts the resubmitted ledger, proceed to build. Later
    scripts in the same turn may update assumption status/evidence but are not
    gated again.
- Add an `_FIDELITY_PROMPT` fragment keyed off `settings.fidelity_target != "unspecified"`,
  stating the target and its meaning (replica = reproduce faithfully; stylised =
  keep the gesture, discard surface detail, and *ignore extra reference detail*;
  functional_analogue = match function, not appearance). This is the field that
  most changes output from identical inputs.
- Add an `_INFERRED_PROMPT` fragment keyed off `settings.mark_inferred_features`:
  any feature required by the description but not visible in a reference must be
  built **and** named/commented `INFERRED`; never invent silently.
- These are string concatenations in `_system_prompt` (line 269), same pattern as
  `_WORKFLOW_PROMPT` / `_STAGE_PROMPT`.

### 3. `cad_workflow.py` — the gate helper (pure)
- Add `assumption_ledger_missing(proposal, turn_state) -> list[str]`, analogous to
  `proposal_issues`. It returns issues when the proposal is the first
  `run_freecad_script` of the turn and the model has not emitted a valid
  assumption ledger. This intentionally uses the first script call as the
  enforceable boundary: arbitrary FreeCAD Python cannot be classified reliably
  as geometry-creating before execution. The caller handles the settings gate.
- Represent the ledger as a new optional structured field on the proposal (see
  #4). The helper checks: ledger present; unique stable ids; non-empty names,
  units, and sources; numeric values; valid confidence/consequence/status enums;
  non-empty `if_wrong`; and high → medium → low consequence ordering. Keep it
  type/shape checks only — no FreeCAD calls.
- Add `blocking_assumptions(assumptions) -> tuple`, returning rows whose
  confidence is `low`, consequence is `high`, and status is still `unverified`.
  This gives `agent` an enforceable decision instead of relying on prompt
  compliance.
- Extend `ledger_text` to render a persisted assumption table when present in the
  `_Turn.ledger` dict, so it stays in the model's working memory across steps.
  Render id, name/value/unit, source, confidence, consequence, status, and
  evidence. Status changes are explicit model proposals checked by the merge
  rules below; the agent never infers confirmation merely because a build
  succeeded.

### 4. `llm_client.py` — tool schema
- Define the reusable schema for an assumption row:
  `{id, name, value, unit, source, confidence, consequence, if_wrong, status,
  evidence}`. `value` is numeric; `confidence` and `consequence` are
  `high|medium|low`; `status` is
  `unverified|user_confirmed|measured`; `evidence` defaults to an empty string.
- Do not add `assumptions` directly to the always-exposed base
  `_TOOL_PARAMETERS`. In `_openai_tools`, after deep-copying `TOOL_SCHEMA`, add
  the array property to `run_freecad_script` and make it required only when
  `settings.assumption_ledger` is enabled. `_anthropic_tools` already derives
  from `_openai_tools`, so both providers receive the same gated schema. With
  the setting off, the property is absent and existing behaviour is untouched.
- Parse the array in `_proposal_from_tool`, alongside `plan` and
  `success_criteria`, into immutable normalized rows in
  `proposal.assumptions`. Reject malformed shapes in the pure workflow helper,
  not in provider-specific code.

### 5. `agent.py` — dispatch
- In `send`, after the existing `structured_cad_planning` gate block
  (around line 328), add an assumption-ledger state machine with its own
  `turn.assumption_retries`, `turn.assumptions_requested`, and
  `turn.assumption_question_asked` state:
  1. Before the first script executes, validate `proposal.assumptions`. On
     validation failure, push `[assumption ledger required]` and `continue`,
     capped by `self_correction_attempts`.
  2. Persist a valid ledger immediately. If it contains blocking rows, do not
     execute the proposed script. Push `[assumption clarification required]`
     instructing the model to call `ask_user` for those rows, then `continue`.
  3. Permit one clarification round containing at most three sequential
     `ask_user` calls. Each call remains one question with 2–5 options, matching
     the existing schema and `_handle_question` behavior. If more than three
     blocking rows exist, the model must group them into no more than three
     decisions. Track the count separately from retries and persist each user
     selection through the existing question path.
  4. Require the next script proposal to reuse the stable ids, incorporate the
     answer, and change resolved rows to `user_confirmed`. Reject removal,
     duplicate ids, unexplained value changes, or a confirmation lacking
     evidence that cites the user selection.
  5. Once no blocking rows remain, set `turn.assumptions_accepted = True`, merge
     the rows into `turn.ledger`, reset `assumption_retries`, and allow the
     script to execute. Do not gate later scripts in that turn.
- Later proposals may update existing rows from `unverified` to `measured` only
  when evidence is supplied (for example an inspection or computed document
  measurement). They may not silently delete or rename ids. Implement this as a
  pure `merge_assumptions(previous, proposed) -> (merged, issues)` helper in
  `cad_workflow`.
- Assumption retries use their own counter so alternating planning and
  assumption failures cannot reset one another. Each gate remains independently
  capped by `self_correction_attempts`.
- No change to the image-feedback or render path — those already work.

### 6. `preferences.py`
- Add the three controls to the preferences page: two checkboxes
  (`assumption_ledger`, `mark_inferred_features`) and one combobox
  (`fidelity_target`) alongside the existing workflow switches. Follow the
  `loadSettings`/`saveSettings` protocol already in the file. (Not headlessly
  testable — verify in the GUI smoke test.)

### 7. `llm_client.py` — proposal type
- `LLMProposal` is defined in this file beside the provider-independent
  `_proposal_from_tool` mapper. Add `assumptions: tuple = ()` there so
  `agent`/`cad_workflow` can read it uniformly.

## Tests (pure-python, `tests/test_*.py`)
- `cad_workflow.assumption_ledger_missing`: returns issues when ledger absent on
  first script; passes when a well-formed ledger is present; rejects duplicate
  ids, non-numeric values, invalid enums, missing units/evidence fields, and
  incorrect consequence ordering.
- `blocking_assumptions` selects only low-confidence/high-consequence,
  unverified rows.
- `merge_assumptions` preserves stable ids, rejects deletion/renaming and
  unsupported value/status changes, and accepts `user_confirmed` or `measured`
  transitions with appropriate evidence.
- `ledger_text` renders the assumption table when present, omits it when absent.
- `_system_prompt` includes each fragment iff its setting is on (three cases).
- Tool schemas include and require `assumptions` for both providers only when
  `assumption_ledger=True`; the property is absent when disabled.
- `agent.send` gate:
  - a proposal with no assumptions triggers a correction and executes only
    after a valid ledger is supplied;
  - a blocking row prevents execution, triggers `ask_user`, and executes only
    after a resubmitted row incorporates the selection and becomes
    `user_confirmed`;
  - at most one assumption-question round of three sequential calls is allowed,
    and each call conforms to the existing one-question/options schema;
  - malformed resubmissions and retries are capped;
  - a second script in the same turn is not gated again;
  - planning and assumption retry counters do not reset each other.
  Use the existing fake client/executor doubles and assert that the executor has
  no calls before the gate succeeds.
- `settings` round-trips the three new fields.

## Verification
- Run `nix develop -c python3 -m pytest tests/test_*.py -v`.
- The prompt/settings changes have no FreeCAD dependency, so the integration
  suite is unaffected; still run `nix develop -c freecadcmd tests/integration/run_headless.py`
  to confirm no regression.
- Manual GUI smoke test for the preferences controls (per project note, the Qt
  page can't be exercised headlessly).

## Explicitly out of scope
- Image triage / rectification / silhouette extraction (Stage 1 of the harness):
  batch-pipeline machinery; the interactive loop lets the human supply a good
  photo and the assumption ledger catches the scale error more cheaply.
- Silhouette-IoU golden-set eval rig: a research harness, not runtime loop.
- Batch iteration caps: the human is the convergence mechanism every turn.
- A new "photo mode": the existing `user_images` path + assumption ledger is it.
