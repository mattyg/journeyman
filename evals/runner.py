"""Headless benchmark runner. Run under freecadcmd (freecadcmd eats unknown
options, so script arguments go through --pass as one quoted string):

    freecadcmd evals/runner.py --pass "--scenario hanger-modify"
    freecadcmd evals/runner.py --pass "--prefix disc-spring"
    freecadcmd evals/runner.py --pass "--all --repeat 2"

--prefix runs every variant of one base part (all nine disc-spring
scenarios above); --variant narrows any selection to given variant types,
e.g. --pass "--all --variant modify-img".

Requires the provider API key in the environment, matching the --model
prefix (default is openrouter/..., so OPENROUTER_API_KEY; ANTHROPIC_API_KEY
or OPENAI_API_KEY for direct providers).
Each attempt writes runs/<timestamp>-<sha>/<scenario-id>[-<n>]/ containing
transcript.md, final.FCStd, views/, and run.json.
"""
import argparse
import base64
import datetime
import json
import os
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from freecad.journeyman import (  # noqa: E402
    document_inspector, llm_client, script_executor, transcript_export,
    view_capture)
from freecad.journeyman.agent import Agent  # noqa: E402
from freecad.journeyman.settings import Settings, load_settings  # noqa: E402
from evals import checks, cli, scenarios  # noqa: E402

# Before any os.environ default below is resolved.
cli.load_env()

_ENV_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "ollama": "",  # no key needed
}

DEFAULT_MODEL = os.environ.get(
    "EVAL_MODEL", "openrouter/anthropic/claude-sonnet-4-5")


class _Client:
    def complete(self, messages, settings):
        return llm_client.complete(messages, settings)

    def system_prompt(self, settings):
        return llm_client.system_prompt(settings)


def _die(message):
    # freecadcmd suppresses SystemExit messages; print before exiting.
    print(f"[eval] error: {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


class _DefaultParams:
    """A param_get that always returns the caller's default.

    Running the eval on the Settings dataclass defaults would measure a
    harness nobody ships: several features (parametric preference, sketch
    constraint verification, structured planning) default to False on the
    dataclass but True in load_settings. Feeding load_settings this object
    reproduces exactly what a user gets on a fresh install, so the eval
    grades the shipped harness and stays correct when those defaults change.
    """

    def GetBool(self, _key, default=False):
        return default

    def GetInt(self, _key, default=0):
        return default

    def GetString(self, _key, default=""):
        return default


def build_settings(model, overrides=None):
    provider = model.split("/", 1)[0]
    env_key = _ENV_KEYS.get(provider)
    if env_key is None:
        _die(f"unknown provider prefix in model '{model}'")
    api_key = os.environ.get(env_key, "") if env_key else ""
    if env_key and not api_key:
        _die(f"{env_key} is not set")
    settings = load_settings(_DefaultParams())
    settings.model = model
    settings.api_key = api_key
    settings.api_base = os.environ.get("EVAL_API_BASE", "")
    # Headless-run necessities: nothing can approve a step or answer a
    # prompt, and renders are the judge's only view of the result.
    settings.confirm_before_running = False
    settings.auto_approve_loop = True
    settings.max_auto_approved_steps = 25
    settings.rendered_views = True
    settings.persist_chat_history = False
    settings.reasoning_effort = os.environ.get("EVAL_REASONING", "off")
    for key, value in (overrides or {}).items():
        if not hasattr(settings, key):
            _die(f"unknown setting {key!r}")
        setattr(settings, key, value)
    return settings


def _parse_overrides(items):
    """--set name=value pairs, typed from the dataclass field they target."""
    overrides = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep:
            _die(f"--set expects name=value, got {item!r}")
        key = key.strip()
        current = getattr(Settings, key, None)
        if isinstance(current, bool) or raw.lower() in ("true", "false"):
            value = raw.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(current, int) or raw.strip().lstrip("-").isdigit():
            value = int(raw)
        else:
            value = raw
        overrides[key] = value
    return overrides


def _git_sha():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
            text=True).strip()
    except Exception:
        return "nogit"


def _load_images(paths):
    images = []
    for path in paths:
        with open(path, "rb") as fh:
            images.append({
                "name": os.path.basename(path),
                "data": base64.b64encode(fh.read()).decode("ascii"),
            })
    return images


class _Recorder:
    """Collects transcript entries in the shapes entries_to_markdown expects."""

    def __init__(self, verbose=True):
        self.entries = []
        self.context_payloads = 0
        self.verbose = verbose

    def _log(self, text):
        if self.verbose:
            print(f"[eval] {text}", flush=True)

    def on_intent(self, text):
        return True  # auto-approve everything headlessly

    def on_result(self, result, snapshot, intent, script):
        self.entries.append({"kind": "step", "intent": intent,
                             "script": script, "result": result})
        self._log(f"step: {intent!r} ok={result.ok}")

    def on_reasoning(self, text):
        self.entries.append({"kind": "reasoning", "text": text})

    def on_context(self, messages):
        self.context_payloads += len(messages)
        self.entries.append({"kind": "context", "messages": messages})

    def on_tool(self, tool, summary, details):
        self.entries.append({"kind": "tool", "tool": tool,
                             "summary": summary, "details": details})
        self._log(f"tool: {tool} — {summary}")

    def on_tool_result(self, tool, summary, feedback):
        for entry in reversed(self.entries):
            if entry.get("kind") == "tool" and entry.get("tool") == tool \
                    and "result" not in entry:
                entry["result"] = feedback
                return
        self.entries.append({"kind": "tool", "tool": tool,
                             "summary": summary, "result": feedback})

    def on_question(self, proposal):
        # Benchmarks must never block on a human; answer with the first
        # option and record the exchange so the judge can see the guess.
        selected = [proposal.options[0]["id"]]
        self.entries.append({
            "kind": "question", "question": proposal.question,
            "options": proposal.options, "answer": selected,
        })
        self._log(f"auto-answered question: {proposal.question!r}")
        return selected

    def on_timeout(self, message):
        # One retry per timeout keeps flaky networking from zeroing a run;
        # the retry budget inside llm_client has already been spent.
        already = sum(1 for e in self.entries if e.get("kind") == "timeout")
        retry = already < 3
        self.entries.append({"kind": "timeout", "message": message,
                             "decision": retry})
        return retry


def run_scenario(app, scenario, settings, out_dir, budget_seconds):
    os.makedirs(out_dir, exist_ok=True)
    for doc in list(app.listDocuments().values()):
        app.closeDocument(doc.Name)
    if scenario.start_document:
        doc = app.openDocument(scenario.start_document)
    else:
        doc = app.newDocument(scenario.id.replace("-", "_"))
    app.setActiveDocument(doc.Name)
    before = checks.baseline(doc)

    recorder = _Recorder()
    recorder.entries.append({"kind": "user", "text": scenario.prompt,
                             "images": [{"name": os.path.basename(p)}
                                        for p in scenario.images]})

    def capture(changed_names, strategy, max_isolated, **options):
        return view_capture.capture(
            app, changed_names, strategy, max_isolated, **options)

    agent = Agent(client=_Client(), inspector=document_inspector.snapshot,
                  executor=script_executor, app=app, settings=settings,
                  view_capture=capture)

    cancel_event = threading.Event()
    watchdog = threading.Timer(budget_seconds, cancel_event.set)
    watchdog.daemon = True
    watchdog.start()
    started = time.time()
    termination = "completed"
    summary = ""
    try:
        summary = agent.send(
            scenario.prompt,
            recorder.on_intent,
            recorder.on_result,
            on_reasoning=recorder.on_reasoning,
            on_context=recorder.on_context,
            user_images=_load_images(scenario.images) or None,
            cancel_event=cancel_event,
            on_question=recorder.on_question,
            on_timeout=recorder.on_timeout,
            on_tool=recorder.on_tool,
            on_tool_result=recorder.on_tool_result)
        if cancel_event.is_set():
            termination = "budget_exceeded"
    except Exception as exc:
        termination = "budget_exceeded" if cancel_event.is_set() else "crashed"
        summary = f"{type(exc).__name__}: {exc}"
    finally:
        watchdog.cancel()
    elapsed = time.time() - started
    recorder.entries.append(
        {"kind": "text", "html": f"<p>{summary}</p>"})

    doc.recompute()
    final_path = os.path.join(out_dir, "final.FCStd")
    doc.saveAs(final_path)

    views_dir = os.path.join(out_dir, "views")
    os.makedirs(views_dir, exist_ok=True)
    try:
        for index, image in enumerate(capture((), "global", 0)):
            with open(os.path.join(views_dir, f"montage-{index}.png"),
                      "wb") as fh:
                fh.write(base64.b64decode(image["data"]))
    except Exception as exc:
        print(f"[eval] view capture failed: {exc}", flush=True)

    with open(os.path.join(out_dir, "transcript.md"), "w",
              encoding="utf-8") as fh:
        fh.write(transcript_export.entries_to_markdown(recorder.entries))

    check_results = checks.run_checks(app, doc, scenario, before)
    steps = sum(1 for e in recorder.entries if e.get("kind") == "step")
    run_record = {
        "scenario": scenario.id,
        "kind": scenario.kind,
        "model": settings.model,
        "git_sha": _git_sha(),
        "elapsed_seconds": round(elapsed, 1),
        "steps": steps,
        "termination": termination,
        "summary": summary,
        "checks": check_results,
        "starting_document_faults": before["unconstrained_sketches"],
        # The harness configuration is half of what a score means; without it
        # a later run is not comparable.
        "settings": {
            key: value for key, value in vars(settings).items()
            if key not in ("api_key", "api_base")},
        "judge": scenario.judge,
        "prompt": scenario.prompt,
        "images": [os.path.basename(p) for p in scenario.images],
    }
    with open(os.path.join(out_dir, "run.json"), "w",
              encoding="utf-8") as fh:
        json.dump(run_record, fh, indent=2)
    print(f"[eval] {scenario.id}: {termination}, {steps} steps, "
          f"checks {check_results['passed']}/{check_results['ran']}, "
          f"{elapsed:.0f}s -> {out_dir}", flush=True)
    return run_record


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", action="append", default=[],
                        help="scenario id (repeatable)")
    parser.add_argument("--prefix", action="append", default=[],
                        help="run every scenario whose id starts with this, "
                             "e.g. --prefix disc-spring for all variants of "
                             "one base part (repeatable)")
    parser.add_argument("--variant", action="append", default=[],
                        help="restrict to these variant suffixes, e.g. "
                             "--variant modify-img (repeatable)")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--budget", type=int, default=900,
                        help="wall-clock seconds per attempt")
    parser.add_argument("--set", action="append", default=[], dest="overrides",
                        metavar="NAME=VALUE",
                        help="override a harness setting for this run, e.g. "
                             "--set assumption_ledger=true (repeatable). "
                             "Defaults otherwise match a fresh install.")
    parser.add_argument("--output", default=os.path.join(ROOT, "evals",
                                                         "runs"))
    args = parser.parse_args(argv)

    available = scenarios.load_all()
    if args.all:
        selected = list(available)
    elif args.scenario or args.prefix:
        known = {s.id for s in available}
        missing = [sid for sid in args.scenario if sid not in known]
        if missing:
            _die(f"unknown scenario id(s) {missing}; available: "
                 + ", ".join(sorted(known)))
        selected = [s for s in available if s.id in set(args.scenario)]
        for prefix in args.prefix:
            matched = [s for s in available
                       if s.id.startswith(prefix)
                       and s not in selected]
            if not matched:
                _die(f"no scenario id starts with {prefix!r}; available: "
                     + ", ".join(sorted(known)))
            selected += matched
        selected.sort(key=lambda s: s.id)
    else:
        parser.error("pass --scenario <id>, --prefix <base>, or --all")

    if args.variant:
        wanted = set(args.variant)
        filtered = [s for s in selected
                    if (s.judge.get("variant") in wanted
                        or any(s.id.endswith("-" + v) for v in wanted))]
        if not filtered:
            _die(f"no selected scenario matches variant(s) "
                 f"{sorted(wanted)}")
        selected = filtered

    import FreeCAD as app
    settings = build_settings(args.model, _parse_overrides(args.overrides))
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(args.output, f"{stamp}-{_git_sha()}")
    attempts = len(selected) * args.repeat
    print(f"[eval] {len(selected)} scenario(s) x {args.repeat} attempt(s) "
          f"= {attempts}, model {settings.model}", flush=True)
    for scenario in selected:
        print(f"[eval]   - {scenario.id} "
              f"({scenario.judge.get('variant', scenario.kind)})", flush=True)
    failures = 0
    for scenario in selected:
        for attempt in range(1, args.repeat + 1):
            suffix = f"-{attempt}" if args.repeat > 1 else ""
            out_dir = os.path.join(run_dir, scenario.id + suffix)
            record = run_scenario(app, scenario, settings, out_dir,
                                  args.budget)
            if record["termination"] != "completed" \
                    or not record["checks"]["all_ok"]:
                failures += 1
    print(f"[eval] run directory: {run_dir}", flush=True)
    return 1 if failures else 0


# freecadcmd executes scripts with __name__ set to the file's basename
# rather than "__main__", so accept both.
if __name__ in ("__main__", "runner"):
    sys.exit(main(cli.script_args()))
