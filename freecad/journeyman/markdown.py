"""Small, dependency-free Markdown renderer for model replies.

The output is deliberately limited to the Markdown commonly produced by chat
models and is safe to pass to QTextDocument.setHtml().
"""
import html
import re


_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _break_escaped(text, chunk=48):
    # Treat HTML entities as one visible character so a break never corrupts
    # escaped source such as &lt; or &amp;.
    units = re.findall(r"&(?:#\d+|#x[0-9a-fA-F]+|\w+);|.", text)
    return "&#8203;".join(
        "".join(units[index:index + chunk])
        for index in range(0, len(units), chunk))


def _break_long_text_nodes(markup):
    """Add wrap points to long visible tokens without touching HTML tags."""
    parts = re.split(r"(<[^>]+>)", markup)
    for index in range(0, len(parts), 2):
        parts[index] = re.sub(
            r"\S{49,}", lambda match: _break_escaped(match.group(0)),
            parts[index])
    return "".join(parts)


def _code_block(lines):
    rendered = []
    for line in lines:
        leading = len(line) - len(line.lstrip(" "))
        escaped = html.escape(line[leading:])
        words = re.split(r"(\s+)", escaped)
        rendered.append(
            "&nbsp;" * leading
            + "".join(
                part if part.isspace() else _break_escaped(part)
                for part in words))
    return ('<div style="font-family:monospace;white-space:normal;'
            'word-wrap:break-word;">'
            + "<br>".join(rendered) + "</div>")


def _inline(text):
    text = html.escape(text, quote=True)
    text = _INLINE_CODE.sub(
        lambda match: "<code>" + _break_escaped(match.group(1)) + "</code>",
        text)

    def link(match):
        label, url = match.groups()
        if not url.lower().startswith(("http://", "https://", "mailto:")):
            return label
        return f'<a href="{url}">{label}</a>'

    text = _LINK.sub(link, text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__([^_\n]+)__", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"<i>\1</i>", text)
    return _break_long_text_nodes(text)


def to_html(markdown, inline_first=False):
    """Convert a conservative Markdown subset to safe HTML."""
    lines = (markdown or "").splitlines()
    out = []
    paragraph = []
    list_tag = None
    in_code = False
    code = []
    first_block = True

    def flush_paragraph():
        nonlocal first_block
        if paragraph:
            body = "<br>".join(_inline(x) for x in paragraph)
            if inline_first and first_block:
                out.append(body)
            else:
                out.append("<p>" + body + "</p>")
            first_block = False
            paragraph.clear()

    def close_list():
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    for line in lines:
        if line.strip().startswith("```"):
            if in_code:
                out.append(_code_block(code))
                code.clear()
                in_code = False
            else:
                flush_paragraph()
                close_list()
                in_code = True
            continue
        if in_code:
            code.append(line)
            continue
        if not line.strip():
            flush_paragraph()
            close_list()
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        item = re.match(r"^\s*[-*+]\s+(.+)$", line)
        numbered = re.match(r"^\s*\d+[.)]\s+(.+)$", line)
        if heading:
            flush_paragraph()
            close_list()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            first_block = False
        elif item or numbered:
            flush_paragraph()
            wanted = "ul" if item else "ol"
            if list_tag != wanted:
                close_list()
                list_tag = wanted
                out.append(f"<{list_tag}>")
                first_block = False
            out.append("<li>" + _inline((item or numbered).group(1)) + "</li>")
        else:
            close_list()
            paragraph.append(line)
    if in_code:
        out.append(_code_block(code))
        first_block = False
    flush_paragraph()
    close_list()
    return "".join(out)
