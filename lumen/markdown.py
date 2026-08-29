"""Very small Markdown -> styled-line renderer for the curses chat view.

Not a full CommonMark implementation, just enough to make model answers
(bold/italic emphasis, inline code, fenced code blocks, headings, bullet
lists, blockquotes, links) render legibly in a terminal instead of showing
raw asterisks/backticks/hashes.

`render_to_lines` returns a list of "lines", where each line is itself a
list of (text, color_const, extra_attr) segments -- callers draw each
segment left to right so a single wrapped row can mix styles (e.g. plain
text followed by **bold** followed by `code`).
"""

import curses
import re

_FENCE_RE = re.compile(r"^```")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)")
_BULLET_RE = re.compile(r"^(\s*)[*\-+]\s+(.*)")
_QUOTE_RE = re.compile(r"^(\s*)>\s?(.*)")

_INLINE_RE = re.compile(
    r"(?P<code>`[^`]+`)"
    r"|(?P<bold>\*\*[^*]+\*\*|__[^_]+__)"
    r"|(?P<italic>\*[^*\n]+\*|_[^_\n]+_)"
    r"|(?P<link>\[[^\]]+\]\([^)]+\))"
)


def _split_blocks(text):
    """Split text into ('text' | 'code', [line, ...]) blocks on ``` fences."""
    blocks = []
    buf = []
    in_code = False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            blocks.append(("code" if in_code else "text", buf))
            buf = []
            in_code = not in_code
            continue
        buf.append(line)
    blocks.append(("code" if in_code else "text", buf))
    return [(kind, lns) for kind, lns in blocks if lns]


def _wrap_plain(line, width):
    """Hard-wrap a single line at `width`, preserving all characters exactly."""
    if not line:
        return [""]
    return [line[i:i + width] for i in range(0, len(line), width)] or [""]


def _inline_segments(text, base_color, base_extra, code_color):
    segments = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            segments.append((text[pos:m.start()], base_color, base_extra))
        if m.group("code"):
            segments.append((m.group("code")[1:-1], code_color, 0))
        elif m.group("bold"):
            segments.append((m.group("bold")[2:-2], base_color, base_extra | curses.A_BOLD))
        elif m.group("italic"):
            segments.append((m.group("italic")[1:-1], base_color, base_extra | curses.A_ITALIC))
        elif m.group("link"):
            raw = m.group("link")
            link_text, url = raw[1:-1].split("](", 1)
            segments.append((link_text, base_color, base_extra | curses.A_UNDERLINE))
            segments.append((f" ({url})", code_color, 0))
        pos = m.end()
    if pos < len(text):
        segments.append((text[pos:], base_color, base_extra))
    return segments


def _wrap_segments(segments, width, fallback_color):
    """Word-wrap a list of (text, color, extra) segments into display lines."""
    lines = []
    cur = []
    cur_len = 0

    def flush():
        nonlocal cur, cur_len
        while cur and cur[0][0].isspace():
            cur.pop(0)
        while cur and cur[-1][0].isspace():
            cur.pop()
        lines.append(cur if cur else [("", fallback_color, 0)])
        cur = []
        cur_len = 0

    for text, color, extra in segments:
        if not text:
            continue
        for tok in re.findall(r"\S+|\s+", text):
            if tok.isspace():
                if cur_len + len(tok) > width:
                    flush()
                else:
                    cur.append((tok, color, extra))
                    cur_len += len(tok)
                continue
            remaining = tok
            while remaining:
                space_left = width - cur_len
                if space_left <= 0:
                    flush()
                    space_left = width
                if len(remaining) <= space_left:
                    cur.append((remaining, color, extra))
                    cur_len += len(remaining)
                    remaining = ""
                else:
                    piece, remaining = remaining[:space_left], remaining[space_left:]
                    cur.append((piece, color, extra))
                    flush()

    if cur:
        flush()
    return lines or [[("", fallback_color, 0)]]


def render_to_lines(text, width, base_color, code_color, dim_color, base_extra=0):
    width = max(10, width)
    out = []

    for kind, content_lines in _split_blocks(text):
        if kind == "code":
            for raw in content_lines:
                for wl in _wrap_plain(raw, width):
                    out.append([(wl, code_color, 0)])
            continue

        for raw in content_lines:
            line = raw
            line_color = base_color
            line_extra = base_extra

            heading = _HEADING_RE.match(line)
            if heading:
                line = heading.group(2)
                line_extra = base_extra | curses.A_BOLD | curses.A_UNDERLINE

            quote = _QUOTE_RE.match(line)
            quote_prefix = None
            if quote:
                quote_prefix = "│ "
                line = quote.group(2)
                line_color = dim_color

            bullet = _BULLET_RE.match(line)
            bullet_prefix = None
            if bullet:
                bullet_prefix = bullet.group(1) + "• "
                line = bullet.group(2)

            if not line and quote_prefix is None and bullet_prefix is None:
                out.append([("", base_color, 0)])
                continue

            segs = _inline_segments(line, line_color, line_extra, code_color)
            if bullet_prefix:
                segs = [(bullet_prefix, base_color, base_extra)] + segs
            if quote_prefix:
                segs = [(quote_prefix, dim_color, 0)] + segs

            out.extend(_wrap_segments(segs, width, base_color))

    return out or [[("", base_color, 0)]]
