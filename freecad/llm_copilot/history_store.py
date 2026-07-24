"""Compressed, versioned chat history embedded in a FreeCAD document."""
import base64
import json
import zlib

from .types import ExecResult


OBJECT_NAME = "LLMCopilotChatHistory"
PROPERTY_NAME = "Payload"
FORMAT_VERSION = 1


def is_internal_object(obj):
    return getattr(obj, "Name", "") == OBJECT_NAME


def _entry_to_data(entry):
    data = {key: value for key, value in entry.items()
            if key not in ("widget", "label") and not key.startswith("_")}
    result = data.get("result")
    if isinstance(result, ExecResult):
        data["result"] = {
            "ok": result.ok, "output": result.output, "error": result.error,
            "validation_ok": result.validation_ok,
            "validation": result.validation,
            "rolled_back": result.rolled_back,
            "stderr": result.stderr,
            "console_warnings": result.console_warnings,
            "console_errors": result.console_errors,
        }
    return data


def _entry_from_data(data):
    entry = dict(data)
    result = entry.get("result")
    if isinstance(result, dict):
        entry["result"] = ExecResult(**result)
    return entry


def encode(messages, entries):
    payload = {
        "version": FORMAT_VERSION,
        "messages": messages,
        "entries": [_entry_to_data(entry) for entry in entries
                    if entry.get("kind") != "status"],
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.b64encode(zlib.compress(raw, 9)).decode("ascii")


def decode(encoded):
    if not encoded:
        return [], []
    raw = zlib.decompress(base64.b64decode(encoded.encode("ascii")))
    payload = json.loads(raw.decode("utf-8"))
    if payload.get("version") != FORMAT_VERSION:
        return [], []
    return payload.get("messages", []), [
        _entry_from_data(entry) for entry in payload.get("entries", [])]


def save(doc, messages, entries):
    obj = doc.getObject(OBJECT_NAME)
    if obj is None:
        obj = doc.addObject("App::FeaturePython", OBJECT_NAME)
        obj.Label = "LLM Copilot chat history"
        obj.addProperty("App::PropertyString", PROPERTY_NAME, "LLM Copilot")
        try:
            obj.Visibility = False
        except Exception:
            pass
        try:
            obj.ViewObject.ShowInTree = False
        except Exception:
            pass
    setattr(obj, PROPERTY_NAME, encode(messages, entries))


def load(doc):
    obj = doc.getObject(OBJECT_NAME)
    if obj is None or not hasattr(obj, PROPERTY_NAME):
        return [], []
    try:
        return decode(getattr(obj, PROPERTY_NAME))
    except Exception:
        return [], []


def clear(doc):
    if doc.getObject(OBJECT_NAME) is not None:
        doc.removeObject(OBJECT_NAME)
