"""Benchmark scenario schema and loader.

A scenario is one JSON file in evals/benchmarks/ describing a task the agent
should complete headlessly: a prompt, optional reference images, an optional
starting document, and expectations used by checks.py and judge.py. Relative
paths inside a scenario resolve against the benchmarks directory.
"""
import dataclasses
import json
import os

BENCHMARKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "benchmarks")

KINDS = ("create", "modify")


@dataclasses.dataclass
class Expectations:
    bbox_mm: list = None          # [x, y, z] overall size of the final model
    bbox_tol: float = 0.15        # relative tolerance on each bbox axis
    volume_mm3: float = None
    volume_tol: float = 0.20
    ground_truth: str = None      # .FCStd to compare against (absolute path)
    preserve_objects: list = None  # modify: object Names that must survive
    measure: bool = True          # False: don't fill exact bbox/volume from
                                  # ground truth (prompt withholds sizes)


@dataclasses.dataclass
class Scenario:
    id: str
    kind: str
    prompt: str
    images: list                  # absolute paths to reference PNGs
    start_document: str           # absolute path to a starting .FCStd, or None
    expect: Expectations
    judge: dict                   # free-form hints for the LLM judge
    path: str                     # the scenario file itself


def _resolve(base_dir, value):
    if not value:
        return None
    return value if os.path.isabs(value) else os.path.join(base_dir, value)


def load_scenario(path):
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    for field in ("id", "kind", "prompt"):
        if not raw.get(field):
            raise ValueError(f"{path}: missing required field '{field}'")
    if raw["kind"] not in KINDS:
        raise ValueError(f"{path}: kind must be one of {KINDS}")
    base = os.path.dirname(os.path.abspath(path))
    expect_raw = dict(raw.get("expect") or {})
    known = {f.name for f in dataclasses.fields(Expectations)}
    unknown = set(expect_raw) - known
    if unknown:
        raise ValueError(f"{path}: unknown expect fields {sorted(unknown)}")
    expect = Expectations(**expect_raw)
    expect.ground_truth = _resolve(base, expect.ground_truth)
    start_document = _resolve(base, raw.get("start_document"))
    if raw["kind"] == "modify" and not start_document:
        raise ValueError(f"{path}: modify scenarios need start_document")
    return Scenario(
        id=raw["id"],
        kind=raw["kind"],
        prompt=raw["prompt"],
        images=[_resolve(base, img) for img in raw.get("images") or []],
        start_document=start_document,
        expect=expect,
        judge=dict(raw.get("judge") or {}),
        path=os.path.abspath(path),
    )


def load_all(directory=BENCHMARKS_DIR):
    scenarios = []
    for name in sorted(os.listdir(directory)):
        if name.endswith(".json"):
            scenarios.append(load_scenario(os.path.join(directory, name)))
    ids = [s.id for s in scenarios]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate scenario ids: {sorted(dupes)}")
    return scenarios


def find(scenario_id, directory=BENCHMARKS_DIR):
    for scenario in load_all(directory):
        if scenario.id == scenario_id:
            return scenario
    raise KeyError(scenario_id)
