# Repository guidance

This file contains implementation guidance for automated coding agents and
contributors. User-facing installation and product documentation belongs in
`README.md`.

## Scope and safety

- This is a FreeCAD addon named `Journeyman`; it is loaded at startup and is
  available across workbenches.
- The plugin executes generated Python against the active FreeCAD document.
  Preserve transaction, undo, document-pinning, validation, and cancellation
  behavior when changing execution paths.
- FreeCAD and Qt APIs are not thread-safe. Model network calls may run on a
  worker thread, but document and GUI operations must be marshalled to the main
  thread through the existing panel/session machinery.
- Keep the runtime dependency-free. The LLM client intentionally uses the
  Python standard library so the addon works in FreeCAD's bundled interpreter.
- Do not add secrets, provider keys, generated run data, or local FreeCAD files
  to commits.

## Architecture

The top-level modules are entry points and high-level coordinators:

- `agent.py`: model/tool turn coordinator.
- `llm_client.py`: provider transport, tool schemas, and typed proposals.
- `chat_panel.py`: Qt panel and main-thread integration.
- `init_gui.py`: FreeCAD startup registration.
- `api_reference.py`: bounded installed-version FreeCAD API lookup.

Domain packages own the implementation details:

- `workflow/`: stateful workflow engine, CAD policy, and model feedback.
- `document/`: structured document state, transactional execution, and
  document-bound session lifecycle.
- `transcript/`: durable conversation model, storage, export, and markup.
- `visual/`: offscreen CAD capture and reference-image processing.
- `config/`: settings model and the Qt preferences page.

Prefer importing through a domain package façade. Compatibility aliases in
`freecad.journeyman.__init__` support older flat imports but should not be used
by new code.

Keep domain invariants behind their owning abstraction:

- Workflow sequencing and ledger mutation belong in `WorkflowEngine`.
- Document schema derivations belong on `DocumentState`.
- Model-facing history projection belongs to `Transcript`.
- Per-document persistence and lifecycle state belong to `DocumentSession`.
- Tool availability, schema, and proposal type belong in `TOOL_SPECS`.

Avoid extracting small pass-through modules. A new module should hide a
meaningful implementation or establish a clear dependency boundary.

## Verification

Run the pure-Python suite after ordinary changes:

```sh
nix develop --command python3 -m pytest tests/test_*.py -q
```

Run the FreeCAD-dependent suite after changes to document inspection,
execution, rendering, persistence, or FreeCAD adapters:

```sh
nix develop --command freecadcmd tests/integration/run_headless.py
```

Also run:

```sh
git diff --check
```

The generic `pytest` command collects FreeCAD integration modules outside a
FreeCAD interpreter and is therefore not the supported full-suite command.

## Change discipline

- Preserve unrelated working-tree changes.
- Keep pure domain logic importable without FreeCAD or PySide where practical.
- PySide-dependent visual/configuration modules should remain lazily imported
  so headless tests can import the package.
- Update or add focused tests whenever an interface or invariant changes.
- Keep `README.md` concise and user-oriented; implementation and agent guidance
  belongs here.
