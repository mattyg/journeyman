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
    return [name for f in features if (name := getattr(f, "Name", ""))]


def _safe_value(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "Value"):
        try:
            return round(float(value.Value), 6)
        except Exception:
            pass
    return str(value)


def _cylinder_diameters(shape, limit=12):
    """Distinct cylindrical-face diameters, smallest first.

    Holes and bosses are cylinders, so this is the cheapest way to check that a
    stated fastener size actually exists in the finished solid. A run once
    measured its own model, saw no 12 mm cylinder where the bolt hole should
    be, and talked itself out of the discrepancy — the number was available and
    nothing consumed it.
    """
    diameters = set()
    try:
        faces = shape.Faces
    except Exception:
        return []
    for face in faces:
        surface = getattr(face, "Surface", None)
        if getattr(surface, "TypeId", "") != "Part::GeomCylinder":
            continue
        try:
            diameters.add(round(float(surface.Radius) * 2.0, 3))
        except Exception:
            continue
        if len(diameters) >= limit:
            break
    return sorted(diameters)


# Properties every object carries that the model never acts on. A denylist
# rather than an allowlist: an unfamiliar feature type still reports its real
# properties, so nothing the model depends on can vanish silently.
_NOISE_PROPERTIES = frozenset((
    "Shape", "ExpressionEngine", "Visibility", "Label2", "Placement",
    "AttachmentOffset", "_Body", "_LinkTouched", "Proxy", "ViewObject",
))


# No single property is worth more than a couple of lines to the model. A
# document once carried a chat-history object the name filter no longer
# recognised, and its compressed payload put ~367k tokens of base64 into every
# snapshot — past the point where some models could respond at all. Truncating
# by size means the next such property costs a line, not a session.
_MAX_PROPERTY_CHARS = 200


def _is_noise(name, value):
    """True for a property not worth its tokens in the model-facing state."""
    if name in _NOISE_PROPERTIES:
        return True
    # An empty string carries no information; 0 and False often do (a zero
    # Length is a real defect), so only strings are dropped on emptiness.
    return isinstance(value, str) and not value.strip()


def _bounded(value):
    """Cap a single property value so one field cannot flood the snapshot."""
    if isinstance(value, str) and len(value) > _MAX_PROPERTY_CHARS:
        return (value[:_MAX_PROPERTY_CHARS]
                + f"… (truncated, {len(value)} chars)")
    return value


def _format_number(value):
    """Trim trailing zeros so 15.0 reads as 15 and 12483.2 keeps its digit."""
    if isinstance(value, bool) or not isinstance(value, float):
        return str(value).lower() if isinstance(value, bool) else str(value)
    return f"{value:g}"


def format_state(state):
    """Render document state as flat text for the model.

    JSON spends several tokens per field on braces, quotes and colons, and this
    payload is re-read on most turns — the same content as line-oriented text
    costs roughly a third as much. Nothing parses this back; the structured
    dict from :func:`document_state` remains the interface for diffing and
    validation.
    """
    doc = state.get("document")
    if doc is None:
        return "No active document."
    lines = ["Document: " + str(doc)]
    objects = state.get("objects") or {}
    if not objects:
        lines.append("(empty — no objects yet)")
    for name in sorted(objects):
        item = objects[name]
        head = f"{name} ({item.get('type', 'unknown type')})"
        label = item.get("label")
        if label and label != name:
            head += f" label={label!r}"
        if item.get("body"):
            head += f" body={item['body']}"
        lines.append(head)
        lines.extend("  " + line for line in _object_lines(item))
    selection = state.get("selection") or ()
    if selection:
        lines.append("Selected: " + ", ".join(selection))
    return "\n".join(lines)


def _object_lines(item):
    """The indented detail lines for one object in :func:`format_state`."""
    lines = []
    shape = item.get("shape") or {}
    if shape.get("inspection_error"):
        lines.append("shape could not be inspected: "
                     + str(shape["inspection_error"]))
    elif shape:
        parts = ["shape", "valid" if shape.get("valid") else "INVALID"]
        if shape.get("null"):
            parts.append("null")
        for key in ("solids", "shells", "faces", "edges", "vertices"):
            if shape.get(key):
                parts.append(f"{key}={shape[key]}")
        for key in ("volume", "area"):
            if shape.get(key):
                parts.append(f"{key}={_format_number(shape[key])}")
        bbox = item.get("bbox")
        if bbox:
            parts.append("bbox=" + "x".join(_format_number(v) for v in bbox))
        lines.append(" ".join(parts))
        if shape.get("cylinder_diameters"):
            lines.append("cylinder diameters: " + ", ".join(
                _format_number(d) for d in shape["cylinder_diameters"]))
    # Sketch health drives real guards in cad_workflow; always worth its tokens.
    if "fully_constrained" in item:
        lines.append(
            "fully constrained" if item["fully_constrained"]
            else "NOT fully constrained")
    if item.get("solver"):
        lines.append("solver: " + str(item["solver"]))
    if item.get("state"):
        lines.append("state: " + ", ".join(item["state"]))
    if item.get("origin_features"):
        lines.append("origin features: " + ", ".join(item["origin_features"]))
    props = item.get("properties") or {}
    if props:
        lines.append(" ".join(
            f"{key}={_format_number(props[key])}" for key in sorted(props)))
    links = []
    if item.get("depends_on"):
        links.append("<- " + ", ".join(item["depends_on"]))
    if item.get("used_by"):
        links.append("-> " + ", ".join(item["used_by"]))
    if links:
        lines.append("  ".join(links))
    return lines


class DocumentState(dict):
    """Structured document snapshot with its common derived operations.

    It remains mapping-compatible for serialized histories and lightweight test
    doubles, while giving production callers one owner for the state schema.
    """

    def delta_from(self, previous):
        return DocumentDelta(previous, self)

    def health(self):
        solids = volume = 0
        invalid = []
        for name, item in self.get("objects", {}).items():
            shape = item.get("shape") or {}
            if shape.get("inspection_error"):
                continue
            solids += int(shape.get("solids") or 0)
            volume += float(shape.get("volume") or 0.0)
            if shape.get("valid") is False:
                invalid.append(name)
        return solids, volume, invalid

    def structured_diff_from(self, previous):
        return self.delta_from(previous).structured()


def as_document_state(value):
    """Adapt serialized/test mapping state to the behavior-bearing type."""
    return value if isinstance(value, DocumentState) else DocumentState(value or {})


def document_state(app, rich=True):
    """Return stable structured state suitable for diffs and model inspection."""
    doc = getattr(app, "ActiveDocument", None)
    if doc is None:
        return DocumentState(
            {"document": None, "selection": [], "objects": {}})
    objects = {}
    for obj in doc.Objects:
        if is_internal_object(obj):
            continue
        item = {"type": obj.TypeId, "label": obj.Label}
        try:
            parent_body = obj.getParentGeoFeatureGroup()
        except Exception:
            parent_body = None
        if (parent_body is not None
                and getattr(parent_body, "TypeId", "") == "PartDesign::Body"):
            item["body"] = parent_body.Name
        origin_features = _origin_plane_names(obj)
        if origin_features:
            item["origin_features"] = origin_features
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
                holes = _cylinder_diameters(shape)
                if holes:
                    item["shape"]["cylinder_diameters"] = holes
            except Exception as exc:
                item["shape"] = {"inspection_error": str(exc) or type(exc).__name__}
        bb = getattr(shape, "BoundBox", None) if shape is not None else None
        if bb is not None and _bbox_is_valid(bb):
            item["bbox"] = [round(float(v), 6) for v in
                            (bb.XLength, bb.YLength, bb.ZLength)]
        if rich:
            props = {}
            for name in getattr(obj, "PropertiesList", []):
                if name in _NOISE_PROPERTIES:
                    continue
                try:
                    value = getattr(obj, name)
                    if isinstance(value, (str, int, float, bool)) or hasattr(value, "Value"):
                        safe = _safe_value(value)
                        if not _is_noise(name, safe):
                            props[name] = _bounded(safe)
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
    return DocumentState({
        "document": doc.Name,
        "selection": _selection_names(),
        "objects": objects,
    })


class DocumentDelta:
    """The single derivation of "what changed" between two document snapshots.

    All three consumers — offscreen rendering (which objects to render), the
    model feedback diff, and the CAD-workflow review — read from one delta so
    "changed" means exactly the same thing everywhere. ``before``/``after`` are
    document-state dicts as returned by :func:`document_state`.
    """

    def __init__(self, before, after):
        self.before = as_document_state(before)
        self.after = as_document_state(after)
        self.old = self.before.get("objects", {})
        self.new = self.after.get("objects", {})

    @property
    def created(self):
        """Names present only in the after state."""
        return sorted(self.new.keys() - self.old.keys())

    @property
    def deleted(self):
        """Names present only in the before state."""
        return sorted(self.old.keys() - self.new.keys())

    @property
    def modified(self):
        """Names present in both states whose recorded properties differ."""
        return sorted(
            name for name in self.old.keys() & self.new.keys()
            if self.old[name] != self.new[name])

    @property
    def changed_names(self):
        """Created or modified names, sorted — objects worth re-rendering."""
        return sorted(
            name for name in self.new
            if name not in self.old or self.old.get(name) != self.new.get(name))

    def created_types(self):
        """The set of type ids among newly-created objects."""
        return {self.new[name].get("type", "") for name in self.created}

    def structured(self):
        """Human/model-readable created/deleted/modified summary."""
        lines = []
        for name in self.created:
            item = self.new[name]
            lines.append(
                "Created: " + name + " (" + item.get("type", "unknown type")
                + ")")
        for name in self.deleted:
            lines.append(
                "Deleted: " + name + " ("
                + self.old[name].get("type", "unknown type") + ")")
        for name in self.modified:
            changes = []
            for key in sorted(set(self.old[name]) | set(self.new[name])):
                if self.old[name].get(key) != self.new[name].get(key):
                    if key == "properties":
                        before_props = self.old[name].get(key, {})
                        after_props = self.new[name].get(key, {})
                        for prop in sorted(set(before_props) | set(after_props)):
                            if before_props.get(prop) != after_props.get(prop):
                                changes.append(prop)
                    else:
                        changes.append(key)
            if changes:
                lines.append(
                    "Modified: " + name + " (" + ", ".join(changes) + ")")
        return "\n".join(lines) or "No observable document changes."


def structured_diff(before, after):
    return as_document_state(after).structured_diff_from(before)


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
            line = (
                f"{name}: valid={s.get('valid', 'unknown')}, "
                f"solids={s.get('solids', 0)}, faces={s.get('faces', 0)}, "
                f"volume={s.get('volume', 0)}, bbox={item.get('bbox')}")
            if s.get("cylinder_diameters"):
                line += f", cylinder_diameters={s['cylinder_diameters']}"
            summary.append(line)
    if wanted is not None and not wanted:
        return True, "No document changes to validate."
    return True, "\n".join(summary) or "No invalid changed features detected."


def inspect(app, query=""):
    state = document_state(app, rich=True)
    if query:
        objects = state["objects"]
        lowered = query.lower()
        # Match the query against name, label and type. Matching the name
        # alone meant a query that said "the pad" rather than "Pad001" missed
        # everything and fell back to dumping the whole document.
        selected = {
            name for name, item in objects.items()
            if name.lower() in lowered
            or str(item.get("label", "")).lower() in lowered
            or any(part and part.lower() in lowered
                   for part in str(item.get("type", "")).split("::"))}
        related = set(selected)
        for name in selected:
            item = objects[name]
            related.update(item.get("depends_on", ()))
            related.update(item.get("used_by", ()))
        if selected:
            state = dict(state)
            state["objects"] = {
                name: objects[name] for name in sorted(related)
                if name in objects}
    return (f"Query: {query}\n" if query else "") + format_state(state)


def snapshot(app, rich=False) -> str:
    doc = getattr(app, "ActiveDocument", None)
    if doc is None:
        return ("NO_ACTIVE_DOCUMENT\n"
                "There is no active document. The user must create or open one "
                "before using Journeyman.")
    if rich:
        return "[rich state]\n" + format_state(document_state(app, rich=True))
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
    return "\n".join(lines)
