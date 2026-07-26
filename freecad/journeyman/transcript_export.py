"""Render transcript entries to Markdown for file export."""
import html
import re

_TAG = re.compile(r"<[^>]+>")


def _html_to_text(markup):
    """Crude rich-text flattening for assistant/status HTML blocks."""
    text = re.sub(r"<br\s*/?>", "\n", markup)
    text = re.sub(r"</(?:p|div|li|h[1-6])>", "\n", text)
    text = _TAG.sub("", text)
    return html.unescape(text).strip()


def _result_lines(result):
    lines = ["**Result:** " + ("Success" if result.ok else "Failed")]
    if not result.ok and result.error:
        lines += ["**Error:**", "", "```", result.error.rstrip(), "```"]
    if result.output.strip():
        lines += ["**Output:**", "", "```", result.output.rstrip(), "```"]
    for label, value in (
            ("Validation", getattr(result, "validation", "")),
            ("Standard error", getattr(result, "stderr", "")),
            ("FreeCAD console warnings",
             getattr(result, "console_warnings", "")),
            ("FreeCAD console errors",
             getattr(result, "console_errors", ""))):
        if value:
            lines += [f"**{label}:**", "", "```", value.rstrip(), "```"]
    if getattr(result, "rolled_back", False):
        lines.append("**Rolled back to the previous state.**")
    return lines


def _message_content_lines(content):
    """Render one message's content, whether plain text or a block list.

    Image blocks are noted rather than inlined: a base64 payload is hundreds of
    kilobytes that no reader can check by eye, and it would bury the text the
    export exists to show.
    """
    if isinstance(content, str):
        return ["```", content.rstrip() or "(empty)", "```"]
    lines = []
    for block in content or ():
        if not isinstance(block, dict):
            lines += ["```", str(block).rstrip(), "```"]
            continue
        kind = block.get("type")
        if kind == "text":
            lines += ["```", str(block.get("text", "")).rstrip(), "```"]
        elif kind == "image_url":
            url = str((block.get("image_url") or {}).get("url", ""))
            prefix, _, payload = url.partition(",")
            lines.append(
                f"_[image: {prefix or 'inline'}, {len(payload)} base64 chars]_")
        else:
            lines.append(f"_[{kind or 'unknown'} block]_")
    return lines or ["```", "(empty)", "```"]


def _context_lines(entry):
    """Render a model request exactly as it went over the wire.

    This is the record that makes a bad turn diagnosable — and restorable — so
    it carries every message in full rather than a summary. See
    ``Agent.send``, which hands the recorder the same list it sends.
    """
    messages = entry.get("messages", [])
    lines = [f"### Request to the model ({len(messages)} messages)"]
    for message in messages:
        lines += ["", f"**{message.get('role', 'unknown')}:**", ""]
        lines += _message_content_lines(message.get("content"))
    return lines


def _entry_to_markdown(entry):
    kind = entry.get("kind")
    if kind == "user":
        lines = ["## You", "", entry.get("text", "")]
        for image in entry.get("images", []):
            lines.append(f"_Attached: {image.get('name', 'image')}_")
        return lines
    if kind == "text":
        return [_html_to_text(entry.get("html", ""))]
    if kind == "reasoning":
        quoted = ["> " + line for line in entry.get("text", "").splitlines()]
        return ["> **Thinking**"] + quoted
    if kind == "tool":
        title = "### Tool · " + entry.get("tool", "")
        if entry.get("summary"):
            title += " — " + entry["summary"]
        lines = [title]
        if entry.get("details"):
            lines += ["", "```", entry["details"].rstrip(), "```"]
        if entry.get("result"):
            lines += ["", "**Result:**", "", "```",
                      entry["result"].rstrip(), "```"]
        return lines
    if kind == "step":
        lines = ["### " + (entry.get("intent") or "Executed Python step"),
                 "", "```python", entry.get("script", "").rstrip(), "```", ""]
        result = entry.get("result")
        if result is not None:
            lines += _result_lines(result)
        return lines
    if kind == "context":
        return _context_lines(entry)
    if kind == "question":
        lines = ["### Question", "", entry.get("question", "")]
        for option in entry.get("options", []):
            lines.append(
                f"- `{option.get('id', '')}` {option.get('label', '')} — "
                f"{option.get('description', '')}")
        answer = entry.get("answer") or []
        if answer:
            lines += ["", "**Selected:** " + ", ".join(answer)]
        return lines
    if kind == "timeout":
        lines = ["### Model request timed out", "",
                 entry.get("message", "")]
        decision = entry.get("decision")
        if decision is not None:
            lines += ["", "**Decision:** "
                      + ("Retried" if decision else "Stopped")]
        return lines
    return None  # status and unknown kinds are not exported


def entries_to_markdown(entries):
    """Render transcript entries to a Markdown document."""
    blocks = []
    for entry in entries:
        lines = _entry_to_markdown(entry)
        if lines:
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n" if blocks else ""
