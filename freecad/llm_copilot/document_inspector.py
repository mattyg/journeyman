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


def snapshot(app) -> str:
    doc = getattr(app, "ActiveDocument", None)
    if doc is None:
        return ("NO_ACTIVE_DOCUMENT\n"
                "There is no active document. Create one before adding geometry: "
                "doc = App.newDocument().")
    lines = [f"Document: {doc.Name}"]
    if not doc.Objects:
        lines.append("(empty — no objects yet)")
    for obj in doc.Objects:
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
    return "\n".join(lines)
