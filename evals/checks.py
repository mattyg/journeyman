"""Deterministic geometry checks run on the document after an agent run.

Each check returns a dict: {"name", "ok", "detail", "measured"}. `run_checks`
never raises — a check that cannot run reports ok=None (skipped) so a grading
gap is visible without failing the run. Requires FreeCAD (run under freecadcmd).
"""


def _solids(doc):
    """Final visible solids: bodies and standalone Part features."""
    out = []
    for obj in doc.Objects:
        shape = getattr(obj, "Shape", None)
        if shape is None or shape.isNull():
            continue
        # Skip features consumed by a PartDesign Body; the Body carries the tip.
        if getattr(obj, "_Body", None) is not None:
            continue
        if obj.TypeId == "Sketcher::SketchObject":
            continue
        if shape.Solids:
            out.append(obj)
    return out


def _check(name, ok, detail="", measured=None):
    return {"name": name, "ok": ok, "detail": detail, "measured": measured}


def check_recompute(doc):
    doc.recompute()
    bad = [o.Name for o in doc.Objects
           if "Invalid" in o.State or "Error" in o.State]
    touched = [o.Name for o in doc.Objects if "Touched" in o.State]
    ok = not bad and not touched
    detail = ""
    if bad:
        detail += f"error objects: {bad} "
    if touched:
        detail += f"still touched: {touched}"
    return _check("recompute", ok, detail.strip())


def check_solids(doc):
    solids = _solids(doc)
    if not solids:
        return _check("solids", False, "no solid geometry in document")
    problems = []
    for obj in solids:
        if not obj.Shape.isValid():
            problems.append(f"{obj.Name}: invalid shape")
        elif not all(s.isClosed() for s in obj.Shape.Solids):
            problems.append(f"{obj.Name}: open solid")
    return _check("solids", not problems, "; ".join(problems),
                  measured=[o.Name for o in solids])


def _overall_bbox(doc):
    solids = _solids(doc)
    if not solids:
        return None
    box = solids[0].Shape.BoundBox
    for obj in solids[1:]:
        box.add(obj.Shape.BoundBox)
    return box


def check_bbox(doc, expect):
    """Overall size against expectation.

    bbox_mm/volume_mm3 describe a model built to a full specification. A
    modify task deliberately changes dimensions, so the expectation does not
    apply even if one was written down.
    """
    if not expect.bbox_mm or not expect.measure:
        return _check("bbox", None, "no expectation")
    box = _overall_bbox(doc)
    if box is None:
        return _check("bbox", False, "no solids to measure")
    measured = sorted([box.XLength, box.YLength, box.ZLength])
    target = sorted(float(v) for v in expect.bbox_mm)
    tol = expect.bbox_tol
    ok = all(abs(m - t) <= tol * max(t, 1e-9)
             for m, t in zip(measured, target))
    return _check(
        "bbox", ok,
        f"measured {['%.2f' % v for v in measured]} vs "
        f"target {['%.2f' % v for v in target]} (tol {tol:.0%})",
        measured=measured)


def _total_volume(doc):
    return sum(o.Shape.Volume for o in _solids(doc))


def check_volume(doc, expect):
    if not expect.volume_mm3 or not expect.measure:
        return _check("volume", None, "no expectation")
    volume = _total_volume(doc)
    target = float(expect.volume_mm3)
    ok = abs(volume - target) <= expect.volume_tol * max(target, 1e-9)
    return _check("volume", ok,
                  f"measured {volume:.1f} vs target {target:.1f} "
                  f"(tol {expect.volume_tol:.0%})", measured=volume)


def _sketch_problem(sketch):
    """Return why a sketch is not fully constrained, or None if it is fine."""
    from freecad.journeyman.script_executor import assert_sketch_constrained
    try:
        assert_sketch_constrained(sketch)
    except (ValueError, AssertionError) as exc:
        return str(exc)
    except Exception as exc:  # unexpected: report, don't hide
        return f"check failed: {exc}"
    return None


def unconstrained_sketches(doc):
    """Names of sketches that fail the constraint rule right now.

    Captured before a modify run so pre-existing faults are not blamed on
    the agent: several dataset ground-truth files ship with underconstrained
    sketches.
    """
    return {obj.Name for obj in doc.Objects
            if obj.TypeId == "Sketcher::SketchObject"
            and _sketch_problem(obj) is not None}


def check_sketches_constrained(doc, preexisting=()):
    """Every sketch the agent is responsible for is fully constrained.

    Reuses the plugin's own assert_sketch_constrained so the eval grades by
    the same rule the agent is held to. That helper signals failure with
    ValueError; catching only AssertionError here would silently pass an
    underconstrained sketch, since such a sketch still solves cleanly.

    Sketches already faulty in the starting document are reported but not
    counted against the run — the agent did not create them, and fixing
    them was not the task.
    """
    preexisting = set(preexisting or ())
    problems, inherited = [], []
    for obj in doc.Objects:
        if obj.TypeId != "Sketcher::SketchObject":
            continue
        problem = _sketch_problem(obj)
        if problem is None:
            continue
        if obj.Name in preexisting:
            inherited.append(obj.Name)
        else:
            problems.append(f"{obj.Name}: {problem}")
    detail = "; ".join(problems)
    if inherited:
        note = ("pre-existing in the starting document, not graded: "
                + ", ".join(sorted(inherited)))
        detail = f"{detail} ({note})" if detail else note
    return _check("sketches_constrained", not problems, detail)


def check_ground_truth(app, doc, scenario):
    """Volume ratio against the reference .FCStd.

    Only a pass/fail signal when the task was to *reproduce* the reference
    from a full specification. On a modify task the reference is the
    starting document, so a changed volume is the point of the exercise; and
    when the prompt withheld exact sizes (measure=False) any plausible size
    is correct. In those cases the ratio is still measured and reported —
    the judge uses it as evidence — but it does not pass or fail.
    """
    expect = scenario.expect
    if not expect.ground_truth:
        return _check("ground_truth", None, "no expectation")
    graded = scenario.kind == "create" and expect.measure
    try:
        ref = app.openDocument(expect.ground_truth, hidden=True)
    except Exception as exc:
        return _check("ground_truth", None, f"could not open reference: {exc}")
    try:
        ref_volume = _total_volume(ref)
        got_volume = _total_volume(doc)
        if ref_volume <= 0:
            return _check("ground_truth", None, "reference has no volume")
        ratio = got_volume / ref_volume
        if not graded:
            why = ("modify task — reference is the starting document"
                   if scenario.kind == "modify"
                   else "prompt withheld exact sizes")
            return _check(
                "ground_truth", None,
                f"volume ratio {ratio:.2f} (model/reference); "
                f"not graded: {why}", measured=ratio)
        return _check("ground_truth", 0.5 <= ratio <= 2.0,
                      f"volume ratio {ratio:.2f} (model/reference)",
                      measured=ratio)
    finally:
        try:
            app.closeDocument(ref.Name)
        except Exception:
            pass


def check_preserved(doc, expect, before_volumes):
    """Modify runs: named objects still exist with unchanged shape volume."""
    if not expect.preserve_objects:
        return _check("preserved", None, "no expectation")
    problems = []
    for name in expect.preserve_objects:
        obj = doc.getObject(name)
        if obj is None:
            problems.append(f"{name}: deleted")
            continue
        before = before_volumes.get(name)
        shape = getattr(obj, "Shape", None)
        if before is not None and shape is not None and not shape.isNull():
            if abs(shape.Volume - before) > 1e-6 * max(before, 1.0):
                problems.append(
                    f"{name}: volume changed {before:.3f} -> "
                    f"{shape.Volume:.3f}")
    return _check("preserved", not problems, "; ".join(problems))


def snapshot_volumes(doc):
    """Per-object shape volumes, captured before the agent runs."""
    volumes = {}
    for obj in doc.Objects:
        shape = getattr(obj, "Shape", None)
        if shape is not None and not shape.isNull():
            try:
                volumes[obj.Name] = shape.Volume
            except Exception:
                pass
    return volumes


def baseline(doc):
    """Everything about the starting document later checks must know.

    One capture point, so a check that needs "was this already true before
    the agent ran?" cannot silently start blaming the agent for the state of
    a dataset file.
    """
    return {
        "volumes": snapshot_volumes(doc),
        "unconstrained_sketches": sorted(unconstrained_sketches(doc)),
    }


def run_checks(app, doc, scenario, before=None):
    before = before or {}
    before_volumes = before.get("volumes", {})
    preexisting = before.get("unconstrained_sketches", ())
    checks = []
    for fn in (lambda: check_recompute(doc),
               lambda: check_solids(doc),
               lambda: check_bbox(doc, scenario.expect),
               lambda: check_volume(doc, scenario.expect),
               lambda: check_sketches_constrained(doc, preexisting),
               lambda: check_ground_truth(app, doc, scenario),
               lambda: check_preserved(doc, scenario.expect,
                                       before_volumes or {})):
        try:
            checks.append(fn())
        except Exception as exc:  # a broken check must not kill the run
            checks.append(_check("internal", None, f"check crashed: {exc}"))
    ran = [c for c in checks if c["ok"] is not None]
    return {
        "checks": checks,
        "passed": sum(1 for c in ran if c["ok"]),
        "ran": len(ran),
        "all_ok": all(c["ok"] for c in ran) if ran else False,
    }
