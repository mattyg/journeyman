"""Generate benchmark scenarios from the gnucleus-ai/cad-gen-freecad dataset.

Runs under plain python3 (stdlib only):

    python3 evals/fetch_dataset.py --rows 10

Downloads row metadata via the HF datasets-server API and caches each row's
reference image, ground-truth .FCStd, and metadata (refs/<slug>.row.json)
under evals/benchmarks/refs/. Scenario generation from this material is
synthesize.py's job.

Optionally, fill measured bbox/volume expectations from the ground-truth
files (needs FreeCAD):

    freecadcmd evals/fetch_dataset.py --pass "--measure"
"""
import argparse
import glob
import json
import os
import re
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DATASET = "gnucleus-ai/cad-gen-freecad"
ROWS_API = "https://datasets-server.huggingface.co/rows"
RESOLVE = f"https://huggingface.co/datasets/{DATASET}/resolve/main/"
BENCHMARKS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "benchmarks")
REFS = os.path.join(BENCHMARKS, "refs")


def _get(url):
    request = urllib.request.Request(url, headers={"User-Agent": "evals/1.0"})
    with urllib.request.urlopen(request, timeout=60) as resp:
        return resp.read()


def _download(url, dest):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    data = _get(url)
    with open(dest, "wb") as fh:
        fh.write(data)
    return dest


def fetch_rows(count):
    query = urllib.parse.urlencode({
        "dataset": DATASET, "config": "default", "split": "train",
        "offset": 0, "length": count})
    payload = json.loads(_get(f"{ROWS_API}?{query}").decode("utf-8"))
    return [item["row"] for item in payload["rows"]]


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]


def _write_scenario(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    print(f"wrote {os.path.relpath(path, ROOT)}")


def download_row(row):
    """Cache a row's assets under refs/ and write refs/<slug>.row.json.

    Scenario generation lives in synthesize.py; this only fetches and
    normalizes the raw material.
    """
    slug = _slug(row.get("name") or row["id"])
    image_cell = row.get("image")
    image_url = image_cell.get("src") if isinstance(image_cell, dict) else None
    image_rel = None
    if image_url:
        image_rel = f"refs/{slug}.png"
        _download(image_url, os.path.join(BENCHMARKS, image_rel))
    fcstd_rel = None
    fcstd_path = row.get("fcstd_path")
    if fcstd_path:
        fcstd_rel = f"refs/{slug}.FCStd"
        _download(RESOLVE + fcstd_path.lstrip("/"),
                  os.path.join(BENCHMARKS, fcstd_rel))
    meta = {
        "dataset_id": row["id"],
        "slug": slug,
        "name": (row.get("name") or "").strip(),
        "description": (row.get("description") or "").strip(),
        "key_parameters": (row.get("key_parameters") or "").strip(),
        "image": image_rel,
        "fcstd": fcstd_rel,
    }
    meta_path = os.path.join(BENCHMARKS, "refs", f"{slug}.row.json")
    os.makedirs(os.path.dirname(meta_path), exist_ok=True)
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
    print(f"cached {slug}")
    return meta


def measure():
    """Fill expect.bbox_mm / volume_mm3 from each ground-truth FCStd."""
    import FreeCAD as app
    from evals import checks
    for path in sorted(glob.glob(os.path.join(BENCHMARKS, "*.json"))):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        expect = data.get("expect") or {}
        ref = expect.get("ground_truth")
        # measure=False marks variants whose prompt withholds exact sizes;
        # grading them on exact geometry would be unfair.
        if not ref or data.get("kind") != "create" \
                or expect.get("measure") is False:
            continue
        doc = app.openDocument(os.path.join(BENCHMARKS, ref), hidden=True)
        try:
            box = checks._overall_bbox(doc)
            volume = checks._total_volume(doc)
            if box is not None:
                data["expect"]["bbox_mm"] = [
                    round(box.XLength, 2), round(box.YLength, 2),
                    round(box.ZLength, 2)]
            if volume > 0:
                data["expect"]["volume_mm3"] = round(volume, 1)
        finally:
            app.closeDocument(doc.Name)
        _write_scenario(path, data)


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=10)
    parser.add_argument("--measure", action="store_true",
                        help="fill bbox/volume expectations (freecadcmd)")
    args = parser.parse_args(argv)
    if args.measure:
        measure()
        return 0
    for row in fetch_rows(args.rows):
        download_row(row)
    return 0


# freecadcmd executes scripts with __name__ set to the file's basename
# rather than "__main__", so accept both.
if __name__ in ("__main__", "fetch_dataset"):
    from evals import cli
    sys.exit(main(cli.script_args()))
