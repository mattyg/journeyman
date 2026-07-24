"""Safe, compact FreeCAD API lookup for the installed application version."""

import importlib
import inspect
import re


MODULES = {
    "FreeCAD": "FreeCAD",
    "App": "FreeCAD",
    "Part": "Part",
    "PartDesign": "PartDesign",
    "Sketcher": "Sketcher",
}

FIELD_GUIDE = {
    "document": (
        "Use App.ActiveDocument. Create another document only when the user "
        "explicitly requests one. Objects are retrieved by internal Name with "
        "doc.getObject(name); labels are not reliable identifiers."),
    "body origin plane datum attachment support mapmode": (
        "Part Design origin planes belong to body.Origin.OriginFeatures. Use "
        "the actual feature Name from document inspection; doc.XY_Plane and "
        "doc.Body are not valid generic attributes. Sketch attachment commonly "
        "uses AttachmentSupport=[(plane,'')] and MapMode='FlatFace'."),
    "sketch constraint geometry": (
        "Create Sketcher::SketchObject inside the intended Body, add geometry "
        "with addGeometry, and dimensional/geometric constraints with "
        "Sketcher.Constraint. Recompute and inspect solver/constraint state; "
        "do not assume geometry indices after deleting elements."),
    "pad pocket hole revolve partdesign": (
        "Native PartDesign features should live in one Body and reference the "
        "preceding sketch/feature through their documented link properties. "
        "Prefer editable native features over replacing the result with an "
        "opaque Part::Feature."),
    "part shape boolean fuse cut common": (
        "Part shapes support fuse, cut, and common. Assign the resulting Shape "
        "to a document feature only when a Part workflow is appropriate. Check "
        "isNull(), isValid(), Solids, Volume, and bounding dimensions."),
    "transaction recompute undo": (
        "The host owns the execution transaction. Do not open, commit, or abort "
        "transactions in model scripts. Call doc.recompute() only when feature "
        "dependencies must update before later statements."),
    "topology face edge vertex": (
        "Topological indices such as Face1 and Edge3 can change after upstream "
        "edits. Prefer origin/datum references and geometric measurements; "
        "inspect actual surfaces, curves, centers, axes, and bounds before use."),
}


def _version(app):
    try:
        return ".".join(str(value) for value in app.Version()[:3])
    except Exception:
        return "unknown"


def _guide(query):
    words = set(re.findall(r"[a-z0-9_]+", (query or "").lower()))
    ranked = []
    for keywords, text in FIELD_GUIDE.items():
        score = len(words & set(keywords.split()))
        if score:
            ranked.append((score, text))
    ranked.sort(reverse=True)
    return [text for _score, text in ranked[:3]]


def _resolve(module_name, symbol):
    canonical = MODULES.get(module_name or "FreeCAD")
    if canonical is None:
        raise ValueError(
            "Unsupported module. Choose FreeCAD, Part, PartDesign, or Sketcher.")
    value = importlib.import_module(canonical)
    path = (symbol or "").strip()
    if path.startswith(module_name + "."):
        path = path[len(module_name) + 1:]
    resolved = canonical
    for component in filter(None, path.split(".")):
        if not component.isidentifier() or component.startswith("_"):
            raise ValueError("Only public dotted symbol names are allowed.")
        value = getattr(value, component)
        resolved += "." + component
    return value, resolved


def _describe(value, resolved, query):
    lines = ["Symbol: " + resolved, "Type: " + type(value).__name__]
    try:
        signature = str(inspect.signature(value))
    except (TypeError, ValueError):
        signature = ""
    if signature:
        lines.append("Signature: " + resolved + signature)
    doc = inspect.getdoc(value) or ""
    if doc:
        lines.append("Documentation:\n" + doc[:4000])
    terms = [
        word.lower() for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", query)
        if len(word) > 2]
    members = []
    try:
        names = [name for name in dir(value) if not name.startswith("_")]
        if terms:
            matching = [
                name for name in names
                if any(term in name.lower() for term in terms)]
            names = matching or names
        members = names[:80]
    except Exception:
        pass
    if members:
        lines.append("Public members:\n" + ", ".join(members))
    return "\n".join(lines)


def lookup(app, query="", module="FreeCAD", symbol=""):
    """Return bounded installed-version introspection plus relevant guidance."""
    lines = ["Installed FreeCAD version: " + _version(app)]
    try:
        value, resolved = _resolve(module, symbol)
        lines.append(_describe(value, resolved, query))
    except Exception as exc:
        lines.append("Lookup error: " + str(exc))
    guidance = _guide(" ".join((query or "", symbol or "")))
    if guidance:
        lines.append("Bundled field guide:\n- " + "\n- ".join(guidance))
    result = "\n\n".join(lines)
    return result[:12000]
