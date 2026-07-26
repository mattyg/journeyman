"""Aggregate and compare eval runs. Runs under plain python3:

    python3 evals/report.py evals/runs/<run-dir>
    python3 evals/report.py --diff evals/runs/<old> evals/runs/<new>

Writes summary.json into the run directory and prints a markdown table.
Diff mode compares per-scenario mean judge scores and check pass rates;
score deltas under 2 points on a single attempt are flagged as noise.
"""
import argparse
import collections
import glob
import json
import os
import sys

NOISE_THRESHOLD = 2.0


def load_summary(run_dir):
    scenarios = collections.defaultdict(
        lambda: {"attempts": 0, "checks_passed": 0, "checks_ran": 0,
                 "scores": [], "terminations": [], "issues": [],
                 "variant": None})
    for record_path in sorted(glob.glob(
            os.path.join(run_dir, "*", "run.json"))):
        attempt_dir = os.path.dirname(record_path)
        with open(record_path, encoding="utf-8") as fh:
            record = json.load(fh)
        entry = scenarios[record["scenario"]]
        entry["variant"] = (record.get("judge") or {}).get("variant")
        entry["attempts"] += 1
        entry["checks_passed"] += record["checks"]["passed"]
        entry["checks_ran"] += record["checks"]["ran"]
        entry["terminations"].append(record["termination"])
        judge_path = os.path.join(attempt_dir, "judge.json")
        if os.path.exists(judge_path):
            with open(judge_path, encoding="utf-8") as fh:
                verdict = json.load(fh)
            entry["scores"].append(verdict.get("score"))
            entry["issues"].extend(verdict.get("issues", []))
    summary = {}
    for scenario_id, entry in sorted(scenarios.items()):
        scores = [s for s in entry["scores"] if s is not None]
        summary[scenario_id] = {
            "attempts": entry["attempts"],
            "mean_score": (sum(scores) / len(scores)) if scores else None,
            "check_rate": (entry["checks_passed"] / entry["checks_ran"]
                           if entry["checks_ran"] else None),
            "terminations": entry["terminations"],
            "issues": entry["issues"],
            "variant": entry["variant"],
        }
    return summary


def _fmt(value, pattern="{:.1f}"):
    return pattern.format(value) if value is not None else "—"


def print_summary(run_dir, summary):
    print(f"\n## Eval summary — {os.path.basename(run_dir)}\n")
    print("| scenario | attempts | judge score | checks | terminations |")
    print("|---|---|---|---|---|")
    for scenario_id, entry in summary.items():
        print(f"| {scenario_id} | {entry['attempts']} "
              f"| {_fmt(entry['mean_score'])} "
              f"| {_fmt(entry['check_rate'], '{:.0%}')} "
              f"| {', '.join(entry['terminations'])} |")
    by_variant = collections.defaultdict(lambda: {"scores": [], "n": 0})
    for entry in summary.values():
        if entry["variant"]:
            group = by_variant[entry["variant"]]
            group["n"] += entry["attempts"]
            if entry["mean_score"] is not None:
                group["scores"].append(entry["mean_score"])
    if by_variant:
        print("\n### Score by input variant\n")
        print("| variant | scenarios | mean judge score |")
        print("|---|---|---|")
        for variant, group in sorted(by_variant.items()):
            mean = (sum(group["scores"]) / len(group["scores"])
                    if group["scores"] else None)
            print(f"| {variant} | {group['n']} | {_fmt(mean)} |")
    by_class = collections.Counter(
        issue.get("classification", "unclassified")
        for entry in summary.values() for issue in entry["issues"])
    if by_class:
        print("\n### Judge issues by classification\n")
        for classification, count in by_class.most_common():
            print(f"- {classification}: {count}")
        # These two route to different fixes: one changes the harness, the
        # other changes the benchmark prompt.
        for classification, heading in (
                ("harness-prompt", "Harness-prompt issues (fix-pass input)"),
                ("benchmark-defect",
                 "Benchmark defects (fix the scenario, not the harness)")):
            listed = [(scenario_id, issue)
                      for scenario_id, entry in summary.items()
                      for issue in entry["issues"]
                      if issue.get("classification") == classification]
            if listed:
                print(f"\n### {heading}\n")
                for scenario_id, issue in listed:
                    print(f"- [{scenario_id}] {issue.get('summary', '')}")


def print_diff(old_dir, new_dir, old, new):
    print(f"\n## Eval diff — {os.path.basename(old_dir)} -> "
          f"{os.path.basename(new_dir)}\n")
    print("| scenario | score old→new | Δ | checks old→new | note |")
    print("|---|---|---|---|---|")
    regressions = 0
    for scenario_id in sorted(set(old) | set(new)):
        entry_old, entry_new = old.get(scenario_id), new.get(scenario_id)
        if entry_old is None or entry_new is None:
            note = "only in " + ("new" if entry_old is None else "old")
            entry = entry_new or entry_old
            print(f"| {scenario_id} | {_fmt(entry['mean_score'])} | — "
                  f"| {_fmt(entry['check_rate'], '{:.0%}')} | {note} |")
            continue
        score_old, score_new = entry_old["mean_score"], entry_new["mean_score"]
        delta = (score_new - score_old
                 if score_old is not None and score_new is not None else None)
        note = ""
        if delta is not None:
            single = (entry_old["attempts"] == 1 or entry_new["attempts"] == 1)
            if abs(delta) < NOISE_THRESHOLD and single:
                note = "within noise (single attempt)"
            elif delta < 0:
                note = "REGRESSION"
                regressions += 1
            elif delta > 0:
                note = "improved"
        print(f"| {scenario_id} | {_fmt(score_old)} → {_fmt(score_new)} "
              f"| {_fmt(delta, '{:+.1f}')} "
              f"| {_fmt(entry_old['check_rate'], '{:.0%}')} → "
              f"{_fmt(entry_new['check_rate'], '{:.0%}')} | {note} |")
    return regressions


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dirs", nargs="+")
    parser.add_argument("--diff", action="store_true")
    args = parser.parse_args(argv)
    if args.diff:
        if len(args.run_dirs) != 2:
            parser.error("--diff needs exactly two run directories")
        old_dir, new_dir = args.run_dirs
        regressions = print_diff(old_dir, new_dir,
                                 load_summary(old_dir), load_summary(new_dir))
        return 1 if regressions else 0
    for run_dir in args.run_dirs:
        summary = load_summary(run_dir)
        with open(os.path.join(run_dir, "summary.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
        print_summary(run_dir, summary)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
