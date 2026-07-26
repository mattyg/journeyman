"""Generate the benchmark variant matrix from cached dataset rows.

Runs under plain python3:

    python3 evals/fetch_dataset.py --rows 10      # cache raw material first
    python3 evals/synthesize.py                   # then synthesize scenarios

Reads refs/<slug>.row.json files cached by fetch_dataset.py and writes one
scenario JSON per (row, variant). The full dataset rows are the easy corner
of the input space — complete parametric spec plus a clean render — so each
row is expanded along the axes real users vary on:

mechanical variants (no LLM):
  create-img       full spec + reference image (replica baseline)
  create-text      full spec, no image
  image-minimal    reference image with a one-line prompt

LLM-authored variants (wording generated once, reviewed, committed —
never regenerated per eval run, so the benchmark stays stable):
  partial-spec     a deterministic subset of parameters withheld; judge is
                   told which, and grades assumption/question behavior
  vague            casual user phrasing, rough scale only (measure=False)
  functional       describes the use, not the geometry (measure=False)
  modify-param     natural-language request to change one parameter to a
                   computed new value, starting from the ground-truth model
                   (text only)
  modify-underspec vague strengthening request on the existing model
  modify-img       same starting model, but the user attaches a picture of
                   the current part to point at the feature to change

The generator model (EVAL_SYNTH_MODEL, default Sonnet) is deliberately
different from the judge default (Opus) so one model's stylistic assumptions
are not baked into both the questions and the grading.
"""
import argparse
import glob
import json
import os
import random
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from evals import cli, judge  # noqa: E402  (judge: _raw_complete/_settings)
from evals.fetch_dataset import BENCHMARKS, _write_scenario  # noqa: E402

# Before any os.environ default below is resolved.
cli.load_env()

DEFAULT_SYNTH_MODEL = os.environ.get(
    "EVAL_SYNTH_MODEL", "openrouter/anthropic/claude-sonnet-4-5")

_PARAM = re.compile(
    r"^[-*\s]*(?P<name>[A-Za-z_][A-Za-z0-9_ ]*?)\s*[=:]\s*"
    r"(?P<value>-?\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z%°]*)\s*$")


def parse_parameters(text):
    """Parse key_parameters into [{name, value, unit}].

    Handles both one-per-line ("- name = 12.3mm") and comma-separated
    ("- a=1mm, b=2mm") forms; rows in the dataset use both.
    """
    params = []
    seen = set()
    for line in (text or "").splitlines():
        for chunk in line.split(","):
            match = _PARAM.match(chunk.strip())
            if not match:
                continue
            name = match.group("name").strip()
            if name in seen:
                continue
            seen.add(name)
            params.append({
                "name": name,
                "value": float(match.group("value")),
                "unit": match.group("unit"),
            })
    return params


def _fmt(param, value=None):
    number = param["value"] if value is None else value
    text = f"{number:g}"
    return f"{param['name']} = {text}{param['unit']}"


def split_withheld(params, seed):
    """Deterministically withhold about half the parameters (min 1, keep 1)."""
    if len(params) < 2:
        return list(params), []
    rng = random.Random(seed)
    count = max(1, len(params) // 2)
    withheld = rng.sample(params, count)
    if len(withheld) == len(params):
        withheld = withheld[:-1]
    names = {p["name"] for p in withheld}
    return [p for p in params if p["name"] not in names], withheld


def pick_modify_param(params, seed):
    """Deterministically pick a parameter to change and its new value."""
    if not params:
        return None
    rng = random.Random(seed + ":modify")
    param = rng.choice(params)
    new_value = round(param["value"] * 1.25, 2)
    return {"param": param, "new_value": new_value}


_GENERATION_PROMPT = """You write realistic user requests for a CAD journeyman
benchmark. Below is a part's engineering description, its full parameter
list, and instructions for several prompt variants. Write each variant the
way a real user would type it into a chat box — first person, plain
language, no bullet-list spec dumps unless asked.

Part name: {name}

Engineering description:
{description}

Full parameters:
{params}

Return ONLY a JSON object with these string fields:

"partial_spec": A request to model the part that naturally states ONLY these
parameters: {kept}. It must NOT mention, hint at, or give values for these
withheld parameters: {withheld}. Where the withheld ones would matter the
user just doesn't say (real users under-specify without noticing).

"vague": A short casual request for this kind of part. No exact dimensions —
at most one rough size ("about {rough_size}"). Mention what it's for if the
description implies a use.

"functional": Describe only the situation and what the part must do — the
use case — without naming its geometry or dimensions. The reader should have
to design the shape themselves.

"modify_param": The user has this exact part open and wants ONE change:
{modify_instruction}. Phrase it as a practical request (why they need it is
optional), giving the new value in natural language. Do not restate the
whole spec. CRITICAL: the new value must be unambiguous about what it
measures. For a diameter say "X across"/"X diameter" (never "out to X",
which reads as a radius); for a radius say "X radius". A reader must not be
able to interpret the number as measuring something else.

"modify_underspec": The user has the part open and wants it sturdier /
beefed up, phrased vaguely with no numbers, as a real user would.

"modify_image": The user has the part open AND has attached a picture of
that same part as it currently is. They refer to the picture to point out
which feature they mean ("the holes in the picture", "this face here"), then
ask for this change: {modify_instruction}. Make clear the picture shows the
part now, not the goal. Do not name the feature's exact parameter name.
The same CRITICAL rule as modify_param applies: state unambiguously what the
new value measures (diameter vs radius).
"""


def _extract_json(text):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in generator response")
    return json.loads(match.group(0))


def generate_wordings(meta, params, kept, withheld, modify, settings):
    rough = max((p["value"] for p in params), default=0)
    modify_instruction = "increase the part's main dimension by about 25%"
    if modify:
        modify_instruction = (
            f"change {modify['param']['name']} from "
            f"{modify['param']['value']:g}{modify['param']['unit']} to "
            f"{modify['new_value']:g}{modify['param']['unit']}")
    prompt = _GENERATION_PROMPT.format(
        name=meta["name"] or meta["slug"],
        description=meta["description"],
        params="\n".join(_fmt(p) for p in params) or "(none parsed)",
        kept=", ".join(_fmt(p) for p in kept) or "(none)",
        withheld=", ".join(p["name"] for p in withheld) or "(none)",
        rough_size=f"{rough:g}mm",
        modify_instruction=modify_instruction,
    )
    text = judge._raw_complete(settings, [{"type": "text", "text": prompt}])
    wordings = _extract_json(text)
    required = ("partial_spec", "vague", "functional", "modify_param",
                "modify_underspec", "modify_image")
    missing = [key for key in required if not wordings.get(key)]
    if missing:
        raise ValueError(f"generator omitted fields: {missing}")
    return wordings


def _full_prompt(meta):
    prompt = meta["description"]
    if meta["key_parameters"]:
        prompt += "\n\nKey parameters:\n" + meta["key_parameters"]
    return prompt


def mechanical_scenarios(meta):
    """Variants that need no LLM: full-spec and image-minimal."""
    ground = {"ground_truth": meta["fcstd"]} if meta["fcstd"] else {}
    out = []
    if meta["image"]:
        out.append({
            "id": f"{meta['slug']}-create-img",
            "kind": "create",
            "prompt": _full_prompt(meta),
            "images": [meta["image"]],
            "expect": dict(ground),
            "judge": {"variant": "create-img", "fidelity": "replica",
                      "dataset_id": meta["dataset_id"],
                      "notes": "Full spec plus reference render of the "
                               "ground truth; grade silhouette and feature "
                               "match."},
        })
        out.append({
            "id": f"{meta['slug']}-image-minimal",
            "kind": "create",
            "prompt": "Model the part shown in the attached image. Make it "
                      "parametric.",
            "images": [meta["image"]],
            "expect": dict(ground, measure=False),
            "judge": {"variant": "image-minimal", "fidelity": "replica",
                      "dataset_id": meta["dataset_id"],
                      "notes": "Only the image was given — no dimensions. "
                               "Grade proportions and feature match, plus "
                               "whether scale assumptions were surfaced; "
                               "never exact sizes."},
        })
    out.append({
        "id": f"{meta['slug']}-create-text",
        "kind": "create",
        "prompt": _full_prompt(meta),
        "expect": dict(ground),
        "judge": {"variant": "create-text",
                  "fidelity": "functional-analogue",
                  "dataset_id": meta["dataset_id"],
                  "notes": "Full text spec, no image; grade spec adherence "
                           "only."},
    })
    return out


def llm_scenarios(meta, params, kept, withheld, modify, wordings):
    ground = {"ground_truth": meta["fcstd"]} if meta["fcstd"] else {}
    out = [
        {
            "id": f"{meta['slug']}-partial-spec",
            "kind": "create",
            "prompt": wordings["partial_spec"],
            "images": [],
            "expect": dict(ground, bbox_tol=0.25, volume_tol=0.35),
            "judge": {"variant": "partial-spec",
                      "dataset_id": meta["dataset_id"],
                      "withheld": [_fmt(p) for p in withheld],
                      "notes": "These parameters were deliberately withheld "
                               "from the prompt. Grade whether the agent "
                               "flagged assumptions or asked about them — "
                               "never whether it guessed the exact values."},
        },
        {
            "id": f"{meta['slug']}-vague",
            "kind": "create",
            "prompt": wordings["vague"],
            "expect": dict(ground, measure=False),
            "judge": {"variant": "vague", "dataset_id": meta["dataset_id"],
                      "notes": "Casual request with at most a rough size. "
                               "Grade process quality: sensible defaults, "
                               "surfaced assumptions, plausible proportions. "
                               "The ground truth is one valid answer, not "
                               "the required one."},
        },
        {
            "id": f"{meta['slug']}-functional",
            "kind": "create",
            "prompt": wordings["functional"],
            "expect": dict(ground, measure=False),
            "judge": {"variant": "functional",
                      "fidelity": "functional-analogue",
                      "dataset_id": meta["dataset_id"],
                      "notes": "Only the use case was described. Grade "
                               "whether the design would serve that use; "
                               "any geometry that works is correct."},
        },
    ]
    if meta["fcstd"]:
        modify_judge = {"variant": "modify-param",
                        "dataset_id": meta["dataset_id"],
                        "notes": "Grade whether exactly this change was made "
                                 "parametrically with everything else "
                                 "preserved."}
        if modify:
            modify_judge["target"] = {
                "parameter": modify["param"]["name"],
                "old": modify["param"]["value"],
                "new": modify["new_value"],
                "unit": modify["param"]["unit"],
            }
        out.append({
            "id": f"{meta['slug']}-modify-param",
            "kind": "modify",
            "prompt": wordings["modify_param"],
            "start_document": meta["fcstd"],
            # measure=False: the reference is the starting point, not the
            # target, so exact-size grading does not apply.
            "expect": dict(ground, measure=False),
            "judge": modify_judge,
        })
        out.append({
            "id": f"{meta['slug']}-modify-underspec",
            "kind": "modify",
            "prompt": wordings["modify_underspec"],
            "start_document": meta["fcstd"],
            "expect": dict(ground, measure=False),
            "judge": {"variant": "modify-underspec",
                      "dataset_id": meta["dataset_id"],
                      "notes": "Deliberately vague strengthening request. "
                               "Grade whether the agent asked or stated a "
                               "concrete interpretation before editing, and "
                               "preserved design intent."},
        })
        if meta["image"]:
            # The only render available is of the *unmodified* part, so it
            # cannot depict the target state. It plays the role it plays in
            # real use: pointing at which feature to change, while the change
            # itself is stated in text.
            image_modify_judge = {
                "variant": "modify-img",
                "dataset_id": meta["dataset_id"],
                "notes": "The attached image shows the part as it is now, "
                         "NOT the desired result — it identifies the feature "
                         "to change. Grade whether the agent resolved the "
                         "visual reference to the right feature in the open "
                         "document, applied the stated change "
                         "parametrically, and left everything else intact. "
                         "Treating the image as the target (i.e. making no "
                         "change) is a failure.",
            }
            if modify:
                image_modify_judge["target"] = dict(
                    modify_judge.get("target", {}))
            out.append({
                "id": f"{meta['slug']}-modify-img",
                "kind": "modify",
                "prompt": wordings["modify_image"],
                "images": [meta["image"]],
                "start_document": meta["fcstd"],
                "expect": dict(ground, measure=False),
                "judge": image_modify_judge,
            })
    return out


def synthesize_row(meta, settings, skip_llm=False):
    params = parse_parameters(meta["key_parameters"])
    kept, withheld = split_withheld(params, meta["dataset_id"])
    modify = pick_modify_param(params, meta["dataset_id"])
    scenarios = mechanical_scenarios(meta)
    if not skip_llm:
        wordings = generate_wordings(
            meta, params, kept, withheld, modify, settings)
        scenarios += llm_scenarios(
            meta, params, kept, withheld, modify, wordings)
    for scenario in scenarios:
        _write_scenario(
            os.path.join(BENCHMARKS, scenario["id"] + ".json"), scenario)
    return scenarios


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", action="append", default=[],
                        help="only these cached rows (default: all)")
    parser.add_argument("--model", default=DEFAULT_SYNTH_MODEL)
    parser.add_argument("--mechanical-only", action="store_true",
                        help="skip LLM-authored variants (no API key needed)")
    parser.add_argument("--force", action="store_true",
                        help="overwrite existing scenario files; without "
                             "this, rows with any existing scenario are "
                             "skipped to keep committed wordings stable")
    args = parser.parse_args(argv)
    metas = []
    for path in sorted(glob.glob(
            os.path.join(BENCHMARKS, "refs", "*.row.json"))):
        with open(path, encoding="utf-8") as fh:
            meta = json.load(fh)
        if not args.slug or meta["slug"] in args.slug:
            metas.append(meta)
    if not metas:
        raise SystemExit("no cached rows; run fetch_dataset.py --rows N first")
    settings = None
    if not args.mechanical_only:
        settings = judge._judge_settings(args.model)
    total = 0
    for meta in metas:
        existing = glob.glob(os.path.join(BENCHMARKS,
                                          meta["slug"] + "-*.json"))
        if existing and not args.force:
            print(f"skip {meta['slug']} (scenarios exist; --force to redo)")
            continue
        total += len(synthesize_row(meta, settings,
                                    skip_llm=args.mechanical_only))
    print(f"synthesized {total} scenarios")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
