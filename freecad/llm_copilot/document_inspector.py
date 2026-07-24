import json
from .history_store import is_internal_object


def _selection_names():
    try:
        import FreeCADGui as Gui
        return [o.Name for o in Gui.Selection.getSelection()]
    except Exception:
        return []


def _bbox_is_valid(bb):
    """Empty shapes (e.g. an empty Body) report a degenerate BoundBox filled
    with +/-1.79e308 (float max). Treat anything that huge as no bounding box."""
    try:
        limit = 1e12
        return all(abs(v) < limit for v in
                   (bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax))
    except Exception:
        return False


def _origin_plane_names(obj):
    """For a PartDesign Body (or any object with an Origin), return the real
    Names of its origin plane/axis features, so the model can reference them
    instead of guessing (e.g. doc.XY_Plane, which does not exist)."""
    origin = getattr(obj, "Origin", None)
    features = getattr(origin, "OriginFeatures", None) if origin is not None else None
    if not features:
        return []
    names = []
    for f in features:
        name = getattr(f, "Name", "")
        if name:
            names.append(name)
    return names


def _safe_value(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "Value"):
        try:
            return round(float(value.Value), 6)
        except Exception:
            pass
    return str(value)


def document_state(app, rich=True):
    """Return stable structured state suitable for diffs and model inspection."""
    doc = getattr(app, "ActiveDocument", None)
    if doc is None:
        return {"document": None, "objects": {}}
    objects = {}
    for obj in doc.Objects:
        if is_internal_object(obj):
            continue
        item = {"type": obj.TypeId, "label": obj.Label}
        shape = getattr(obj, "Shape", None)
        if shape is not None:
            try:
                item["shape"] = {
                    "null": shape.isNull(), "valid": shape.isValid(),
                    "solids": len(shape.Solids), "shells": len(shape.Shells),
                    "faces": len(shape.Faces), "edges": len(shape.Edges),
                    "vertices": len(shape.Vertexes),
                    "volume": round(float(shape.Volume), 6),
                    "area": round(float(shape.Area), 6),
                }
            except Exception as exc:
                item["shape"] = {"inspection_error": str(exc) or type(exc).__name__}
        bb = getattr(shape, "BoundBox", None) if shape is not None else None
        if bb is not None and _bbox_is_valid(bb):
            item["bbox"] = [round(float(v), 6) for v in
                            (bb.XLength, bb.YLength, bb.ZLength)]
        if rich:
            props = {}
            for name in getattr(obj, "PropertiesList", []):
                if name in ("Shape", "ExpressionEngine"):
                    continue
                try:
                    value = getattr(obj, name)
                    if isinstance(value, (str, int, float, bool)) or hasattr(value, "Value"):
                        props[name] = _safe_value(value)
                except Exception:
                    pass
            if props:
                item["properties"] = props
            item["depends_on"] = [x.Name for x in getattr(obj, "OutList", [])]
            item["used_by"] = [x.Name for x in getattr(obj, "InList", [])]
            if hasattr(obj, "SolverMessages"):
                item["solver"] = str(obj.SolverMessages)
            if hasattr(obj, "FullyConstrained"):
                item["fully_constrained"] = bool(obj.FullyConstrained)
            state = [str(x) for x in getattr(obj, "State", [])]
            if state:
                item["state"] = state
        objects[obj.Name] = item
    return {"document": doc.Name, "objects": objects}


def structured_diff(before, after):
    old, new = before.get("objects", {}), after.get("objects", {})
    lines = []
    for name in sorted(new.keys() - old.keys()):
        lines.append("Created: " + name + " " + json.dumps(new[name], sort_keys=True))
    for name in sorted(old.keys() - new.keys()):
        lines.append("Deleted: " + name + " " + json.dumps(old[name], sort_keys=True))
    for name in sorted(old.keys() & new.keys()):
        changes = []
        for key in sorted(set(old[name]) | set(new[name])):
            if old[name].get(key) != new[name].get(key):
                changes.append(f"{key}: {old[name].get(key)!r} -> {new[name].get(key)!r}")
        if changes:
            lines.append("Modified: " + name + "\n  " + "\n  ".join(changes))
    return "\n".join(lines) or "No observable document changes."


def validate(app, names=None):
    """Detect silent FreeCAD failures without rejecting structural objects.

    When ``names`` is provided, only objects changed by the current transaction
    are checked. Pre-existing document issues and normal Origin geometry must
    never cause an unrelated diagnostic script to fail.
    """
    state = document_state(app, rich=True)
    wanted = set(names) if names is not None else None
    failures = []
    for name, item in state["objects"].items():
        if wanted is not None and name not in wanted:
            continue
        shape = item.get("shape")
        shape_optional = (
            item["type"].startswith("App::")
            or item["type"] in (
                "PartDesign::Body", "Sketcher::SketchObject",
                "PartDesign::FeatureBase"))
        if shape and shape.get("inspection_error") and not shape_optional:
            failures.append(
                f"{name}: shape could not be inspected: "
                f"{shape['inspection_error']}")
            continue
        empty_topology = (shape and not any(
            shape.get(key, 0) for key in
            ("solids", "shells", "faces", "edges", "vertices")))
        if shape and (((shape.get("null") or empty_topology)
                       and not shape_optional)
                      or not shape.get("valid", True)):
            failures.append(f"{name}: shape is null, empty, or invalid")
        props = item.get("properties", {})
        bad_state = [x for x in item.get("state", [])
                     if "error" in x.lower() or "invalid" in x.lower()]
        if bad_state:
            failures.append(f"{name}.State: {', '.join(bad_state)}")
        for key in ("Error", "Status", "SolverMessages"):
            value = props.get(key)
            if value and str(value).lower() not in ("ok", "valid", "none"):
                failures.append(f"{name}.{key}: {value}")
    if failures:
        return False, "\n".join(failures)
    summary = []
    for name, item in state["objects"].items():
        if wanted is not None and name not in wanted:
            continue
        if "shape" in item and not item["shape"].get("inspection_error"):
            s = item["shape"]
            summary.append(
                f"{name}: valid={s.get('valid', 'unknown')}, "
                f"solids={s.get('solids', 0)}, faces={s.get('faces', 0)}, "
                f"volume={s.get('volume', 0)}, bbox={item.get('bbox')}")
    if wanted is not None and not wanted:
        return True, "No document changes to validate."
    return True, "\n".join(summary) or "No invalid changed features detected."


def inspect(app, query=""):
    return (f"Query: {query}\n" if query else "") + json.dumps(
        document_state(app, rich=True), indent=2, sort_keys=True)


def snapshot(app, rich=False) -> str:
    doc = getattr(app, "ActiveDocument", None)
    if doc is None:
        return ("NO_ACTIVE_DOCUMENT\n"
                "There is no active document. The user must create or open one "
                "before using the copilot.")
    lines = [f"Document: {doc.Name}"]
    if not doc.Objects:
        lines.append("(empty — no objects yet)")
    for obj in doc.Objects:
        if is_internal_object(obj):
            continue
        parts = [f"- {obj.Name} (TypeId={obj.TypeId}, Label={obj.Label!r})"]
        shape = getattr(obj, "Shape", None)
        bb = getattr(shape, "BoundBox", None) if shape is not None else None
        if bb is not None and _bbox_is_valid(bb):
            parts.append(
                f"    BoundBox: ({bb.XMin:.2f},{bb.YMin:.2f},{bb.ZMin:.2f})"
                f"->({bb.XMax:.2f},{bb.YMax:.2f},{bb.ZMax:.2f})")
        planes = _origin_plane_names(obj)
        if planes:
            parts.append("    Origin features: " + ", ".join(planes))
        lines.append("\n".join(parts))
    sel = _selection_names()
    if sel:
        lines.append(f"Selected: {', '.join(sel)}")
    if rich:
        lines.append("[rich state]")
        lines.append(json.dumps(document_state(app, rich=True),
                                indent=2, sort_keys=True))
    return "\n".join(lines)
