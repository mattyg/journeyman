"""LLM judge for completed eval runs. Runs under plain python3:

    python3 evals/judge.py evals/runs/<run-dir> [--model anthropic/...]

For every attempt directory containing run.json, sends the transcript, the
final rendered montage, the reference image (if the scenario had one), and
the programmatic check results to a strong model, and writes judge.json:
{"score": 0-10, "issues": [{"classification", "summary", "evidence"}], ...}.

Issue classifications: harness-prompt | tool-api | model-limitation |
grader-doubt. Aggregated harness-prompt issues are the input to the next
harness improvement pass.
"""
import argparse
import base64
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from freecad.journeyman import llm_client  # noqa: E402
from freecad.journeyman.settings import Settings  # noqa: E402
from evals import cli  # noqa: E402

# Before any os.environ default below is resolved.
cli.load_env()

DEFAULT_JUDGE_MODEL = os.environ.get(
    "EVAL_JUDGE_MODEL", "openrouter/anthropic/claude-opus-4-8")

MAX_TRANSCRIPT_CHARS = 120_000

_RUBRIC_COMMON = """You are grading a single run of an autonomous CAD agent
that builds parametric FreeCAD models. You get the task, the run transcript,
programmatic geometry-check results, a rendered montage of the final model
(7 canonical views), and, when the task included one, the user's reference
image.

Return ONLY a JSON object:
{
  "score": <0-10 integer; 10 = a competent CAD designer's result>,
  "verdict": "<one sentence>",
  "issues": [
    {
      "classification": "harness-prompt" | "tool-api" | "model-limitation"
                        | "benchmark-defect" | "grader-doubt",
      "summary": "<one sentence naming the defect>",
      "evidence": "<short quote or observation from the transcript/renders>"
    }
  ]
}

Classification guide:
- harness-prompt: the system/turn protocol misled, under-specified, or failed
  to catch something it should have (wrong guidance, missing gate, bad
  feedback loop). These drive harness fixes — be precise.
- tool-api: a tool (script execution, inspection, view capture, API lookup)
  returned wrong/missing information or lacked a needed capability.
- model-limitation: the model had correct guidance and tools but still failed.
- benchmark-defect: the task prompt or the recorded expectation is itself at
  fault — self-contradictory, impossible, or accidentally ambiguous about
  something it clearly meant to pin down. Use this rather than blaming the
  agent for a task that could not be graded fairly.
  NOT a defect: deliberate under-specification. Several variants withhold
  information on purpose (the scenario hints name the variant, and
  partial-spec even lists what was withheld). Missing dimensions there are
  the test, not a flaw — the agent is being graded on whether it asks or
  states assumptions. Do not report those as benchmark-defect.
- grader-doubt: you cannot tell from the evidence; say what is missing.

Ambiguous instructions — read this before scoring:
A benchmark prompt is written in casual user language and may admit more than
one reasonable reading (what a dimension measures, which feature is meant,
how far "bigger" goes). Where that happens, do NOT score against the reading
you would have picked, and do NOT treat a numeric target in the scenario
hints as the only correct answer. Judge instead:
  1. Was the agent's interpretation a reasonable reading of what the user
     wrote?
  2. Did it make that interpretation visible — stating the assumption or
     asking — rather than silently picking one?
An agent that chose a defensible reading and said so plainly has done the
right thing and should score well, even if it differs from the recorded
target. Raise a benchmark-defect issue naming the ambiguity so the prompt can
be fixed. Reserve low scores for interpretations no careful reader would
make, or for changes made silently where the ambiguity was material.
"""

_RUBRIC_BY_KIND = {
    ("create", True): """Task type: CREATE from a reference image.
Grade: silhouette and feature match to the reference, overall scale and
proportions, fidelity-target adherence, parametric quality (fully constrained
sketches, sensible feature tree), and whether verification actually happened
per feature rather than only at the end.""",
    ("create", False): """Task type: CREATE from a text spec only. No image was
shown to the agent. Grade adherence to the written spec and dimensions,
parametric quality, and process. NEVER penalize visual details the spec did
not state.""",
    ("modify", False): """Task type: MODIFY an existing document, text
instructions only. Grade: was the requested change applied correctly and
parametrically; was pre-existing geometry and design intent preserved; was the
diff minimal (no rebuild-from-scratch of untouched features); did the agent
inspect before editing.""",
    ("modify", True): """Task type: MODIFY an existing document, with a
reference image attached. IMPORTANT: the image shows the part as it currently
is, not the desired end state — the user attached it to point at which feature
they mean, and the change itself is stated in the text. Grade: did the agent
correctly resolve the visual reference to the right feature in the open
document; was the stated change applied parametrically; was pre-existing
geometry and design intent preserved; was the diff minimal; did the agent
inspect the document before editing. Treating the image as the target and
therefore making no change is a failure, as is rebuilding the part to match
the image from scratch.""",
}


def _b64_file(path):
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii")


def _judge_settings(model):
    provider = model.split("/", 1)[0]
    env = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
           "openrouter": "OPENROUTER_API_KEY"}.get(provider, "")
    api_key = os.environ.get(env, "") if env else ""
    if env and not api_key:
        raise SystemExit(f"{env} is not set")
    return Settings(model, api_key, os.environ.get("EVAL_API_BASE", ""),
                    False, True, 1, 1)


def _raw_complete(settings, content_blocks):
    """One raw text completion (no agent tool schema), provider-adapted."""
    provider, wire_model = llm_client._split_model(settings.model)
    base = llm_client._base_url(settings, provider)
    if provider == "anthropic":
        blocks = []
        for block in content_blocks:
            if block["type"] == "text":
                blocks.append({"type": "text", "text": block["text"]})
            else:
                blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": "image/png",
                    "data": block["data"]}})
        payload = {"model": wire_model, "max_tokens": 4096,
                   "messages": [{"role": "user", "content": blocks}]}
        headers = {"x-api-key": settings.api_key,
                   "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        response = llm_client._http_post_json(
            base.rstrip("/") + "/v1/messages", headers, payload)
        return "".join(block.get("text", "")
                       for block in response.get("content", []))
    blocks = []
    for block in content_blocks:
        if block["type"] == "text":
            blocks.append({"type": "text", "text": block["text"]})
        else:
            blocks.append({"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + block["data"]}})
    payload = {"model": wire_model,
               "messages": [{"role": "user", "content": blocks}]}
    headers = {"Authorization": "Bearer " + settings.api_key,
               "content-type": "application/json"}
    response = llm_client._http_post_json(
        base.rstrip("/") + "/chat/completions", headers, payload)
    return response["choices"][0]["message"]["content"] or ""


def _parse_verdict(text):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in judge response")
    verdict = json.loads(match.group(0))
    verdict["score"] = max(0, min(10, int(verdict.get("score", 0))))
    verdict.setdefault("issues", [])
    return verdict


def judge_attempt(attempt_dir, settings):
    with open(os.path.join(attempt_dir, "run.json"), encoding="utf-8") as fh:
        record = json.load(fh)
    transcript_path = os.path.join(attempt_dir, "transcript.md")
    transcript = ""
    if os.path.exists(transcript_path):
        with open(transcript_path, encoding="utf-8") as fh:
            transcript = fh.read()
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        half = MAX_TRANSCRIPT_CHARS // 2
        transcript = (transcript[:half]
                      + "\n\n[... transcript truncated ...]\n\n"
                      + transcript[-half:])

    had_image = bool(record.get("images"))
    rubric = _RUBRIC_BY_KIND[(record["kind"], had_image)]

    blocks = [{"type": "text", "text": _RUBRIC_COMMON + "\n" + rubric}]
    blocks.append({"type": "text", "text": (
        "## Task given to the agent\n" + record["prompt"]
        + "\n\n## Judge hints from the scenario\n"
        + "(Context for grading, not an answer key. Any 'target' here is the "
        "change the benchmark author intended when writing the prompt; if the "
        "prompt as written admits another reasonable reading, apply the "
        "ambiguity rule above.)\n"
        + json.dumps(record.get("judge") or {})
        + "\n\n## Run outcome\ntermination: " + record["termination"]
        + f", steps: {record['steps']}, "
        + f"elapsed: {record['elapsed_seconds']}s"
        + "\n\n## Programmatic check results\n"
        + json.dumps(record["checks"], indent=1))})
    montages = sorted(glob.glob(os.path.join(attempt_dir, "views", "*.png")))
    if montages:
        blocks.append({"type": "text",
                       "text": "## Final model — rendered canonical views"})
        blocks.append({"type": "image", "data": _b64_file(montages[0])})
    else:
        blocks.append({"type": "text",
                       "text": "## No final render available"})
    scenario_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "benchmarks")
    # On a modify task the attached image depicts the starting document, not
    # a target; labelling it as a target would invert the grading.
    reference_role = (
        "the part BEFORE the requested change (the starting document)"
        if record["kind"] == "modify"
        else "the target the agent was asked to reproduce")
    for name in record.get("images") or []:
        ref = os.path.join(scenario_dir, "refs", name)
        if os.path.exists(ref):
            blocks.append({"type": "text", "text": (
                f"## Image the user attached: {name}\n"
                f"This image shows {reference_role}.")})
            blocks.append({"type": "image", "data": _b64_file(ref)})
    blocks.append({"type": "text",
                   "text": "## Run transcript\n\n" + transcript})

    verdict = _parse_verdict(_raw_complete(settings, blocks))
    verdict["scenario"] = record["scenario"]
    verdict["judge_model"] = settings.model
    with open(os.path.join(attempt_dir, "judge.json"), "w",
              encoding="utf-8") as fh:
        json.dump(verdict, fh, indent=2)
    print(f"[judge] {os.path.basename(attempt_dir)}: "
          f"score {verdict['score']}/10, {len(verdict['issues'])} issues")
    return verdict


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir")
    parser.add_argument("--model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--force", action="store_true",
                        help="re-judge attempts that already have judge.json")
    args = parser.parse_args(argv)
    settings = _judge_settings(args.model)
    attempts = sorted(
        os.path.dirname(p) for p in
        glob.glob(os.path.join(args.run_dir, "*", "run.json")))
    if not attempts:
        raise SystemExit(f"no run.json found under {args.run_dir}")
    for attempt_dir in attempts:
        if not args.force and \
                os.path.exists(os.path.join(attempt_dir, "judge.json")):
            continue
        try:
            judge_attempt(attempt_dir, settings)
        except Exception as exc:
            print(f"[judge] {os.path.basename(attempt_dir)} failed: {exc}",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
