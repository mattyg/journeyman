"""Pure HTML formatting for the chat transcript.

These helpers turn model/user/script text into the wrap-friendly rich-text
markup the Qt labels render. They depend only on the stdlib, so unlike the
widget builders in ``chat_panel`` they can be unit-tested headlessly — which is
where the transcript's actual escaping/wrapping bugs live.
"""

import html
import re


def wrappable_escape(text, chunk=48):
    """Escape text and add zero-width breaks inside unusually long tokens.

    Long unbroken tokens (paths, hashes, base64) would otherwise force the
    transcript to scroll horizontally; a zero-width space every ``chunk``
    characters lets them wrap without changing the visible text.
    """
    parts = []
    for part in re.split(r"(\s+)", str(text or "")):
        if not part or part.isspace():
            parts.append(html.escape(part))
            continue
        parts.append("&#8203;".join(
            html.escape(part[index:index + chunk])
            for index in range(0, len(part), chunk)))
    return "".join(parts)


def wrapped_pre(text):
    """Monospace, newline-preserving HTML that remains word-wrappable."""
    lines = []
    for line in str(text or "").splitlines():
        leading = len(line) - len(line.lstrip(" "))
        lines.append(
            "&nbsp;" * leading + wrappable_escape(line[leading:]))
    return ('<div style="font-family:monospace;white-space:normal;'
            'word-wrap:break-word;">'
            + "<br>".join(lines) + "</div>")
