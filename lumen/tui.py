"""Curses UI: model picker, system-prompt editor, and chat screen."""

import curses
import curses.textpad as textpad
import textwrap
import threading
from datetime import datetime
from pathlib import Path

from . import chat as chat_mod
from . import config as config_mod
from . import markdown as md_mod
from .ollama_client import OllamaError

COLOR_TOPBAR = 1     # white on black -- top branding row only
COLOR_BRAND = 2      # blue on white  ("Lumen") -- top branding row only
COLOR_LIST = 3        # white on default terminal background (model list rows)
COLOR_USER = 4       # cyan
COLOR_ASSISTANT = 5  # white (model output)
COLOR_ERROR = 7      # red
COLOR_DIM = 8         # dim hint/status text
COLOR_THINKING = 9   # orange (falls back to yellow without 256-color support)
COLOR_CODE = 10       # green (inline code / fenced code blocks)
COLOR_TOOL = 11       # yellow (tool call log entries)
COLOR_GREY = 12        # grey (message timestamps)

COLLAPSE_ROLES = ("thinking", "thinking_live", "tool")

BRAND_LEFT = "Float64"
BRAND_RIGHT = "Lumen"


class _CancelEdit(Exception):
    pass


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    # Only the top branding row gets a hardcoded background; every other
    # pair uses -1 (the terminal's own default background) so Lumen never
    # paints over the user's terminal theme.
    curses.init_pair(COLOR_TOPBAR, curses.COLOR_WHITE, curses.COLOR_BLACK)
    curses.init_pair(COLOR_BRAND, curses.COLOR_BLUE, curses.COLOR_WHITE)
    curses.init_pair(COLOR_LIST, curses.COLOR_WHITE, -1)
    curses.init_pair(COLOR_USER, curses.COLOR_CYAN, -1)
    curses.init_pair(COLOR_ASSISTANT, curses.COLOR_WHITE, -1)
    curses.init_pair(COLOR_ERROR, curses.COLOR_RED, -1)
    curses.init_pair(COLOR_DIM, curses.COLOR_WHITE, -1)
    orange = 208 if curses.COLORS >= 256 else curses.COLOR_YELLOW
    curses.init_pair(COLOR_THINKING, orange, -1)
    curses.init_pair(COLOR_CODE, curses.COLOR_GREEN, -1)
    curses.init_pair(COLOR_TOOL, curses.COLOR_YELLOW, -1)
    grey = 244 if curses.COLORS >= 256 else curses.COLOR_WHITE
    curses.init_pair(COLOR_GREY, grey, -1)


def safe_addstr(win, y, x, text, attr=0):
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


def safe_hline(win, y, x, ch, n):
    try:
        win.hline(y, x, ch, n)
    except curses.error:
        pass


def draw_top_bar(stdscr, width):
    """Brand-only top bar: no status/model info is ever drawn here."""
    bar_attr = curses.color_pair(COLOR_TOPBAR)
    fill_attr = curses.color_pair(COLOR_BRAND)  # same white background as "Lumen"
    safe_addstr(stdscr, 0, 0, " " * max(0, width - 1), bar_attr)
    safe_addstr(stdscr, 0, 0, BRAND_LEFT, bar_attr | curses.A_BOLD)
    col = len(BRAND_LEFT) + 1
    brand_right = " " + BRAND_RIGHT
    safe_addstr(stdscr, 0, col, brand_right, curses.color_pair(COLOR_BRAND) | curses.A_BOLD)
    col += len(brand_right)
    rest = max(0, (width - 1) - col)
    if rest > 0:
        safe_addstr(stdscr, 0, col, " " * rest, fill_attr)


def _erase_last_word(s):
    """Ctrl+W: delete the trailing word (and the whitespace before it)."""
    s = s.rstrip()
    i = len(s)
    while i > 0 and not s[i - 1].isspace():
        i -= 1
    return s[:i]


def human_size(n):
    n = n or 0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def _format_tool_args(args):
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 60:
            v = v[:57] + "..."
        parts.append(f"{k}={v!r}")
    return ", ".join(parts)


def _entry_lines(idx, role, text, meta, ts, wrap_width, collapsed_indices):
    """Render one history entry to a list of display lines (each a list of
    (text, color, extra_attr) segments). Returns (lines, owner_idx) where
    owner_idx is `idx` if the entry is mouse-collapsible, else None.

    Any line that gets a leading marker (timestamp, "(thinking)", a tool
    header, ...) is wrapped *narrower* than wrap_width by the marker's
    width first, so text + marker never exceeds wrap_width and can't run
    off the right edge of the window.
    """
    ts_seg = (f"[{ts}] ", COLOR_GREY, 0) if ts else None
    ts_width = len(ts_seg[0]) if ts_seg else 0
    msg_width = max(10, wrap_width - ts_width)

    def with_ts(seg_line):
        return [ts_seg] + seg_line if ts_seg else seg_line

    if role in ("assistant", "assistant_live"):
        md_lines = md_mod.render_to_lines(
            text, msg_width, COLOR_ASSISTANT, COLOR_CODE, COLOR_DIM, curses.A_BOLD
        )
        out = [with_ts(md_lines[0])] + md_lines[1:]
        return out, None

    if role == "user":
        wrapped = textwrap.wrap(text, msg_width) or [""]
        out = [[(wline, COLOR_USER, 0)] for wline in wrapped]
        out[0] = with_ts(out[0])
        return out, None

    if role == "error":
        wrapped = textwrap.wrap("! " + text, msg_width) or [""]
        out = [[(wline, COLOR_ERROR, 0)] for wline in wrapped]
        out[0] = with_ts(out[0])
        return out, None

    if role in ("thinking", "thinking_live"):
        if idx in collapsed_indices:
            summary = f"▸ (thinking, {len(text)} chars — click to expand)"
            wrapped = textwrap.wrap(summary, wrap_width) or [""]
            return [[(wline, COLOR_THINKING, 0)] for wline in wrapped], idx
        think_width = max(10, wrap_width - len("▾ (thinking) "))
        md_lines = md_mod.render_to_lines(text, think_width, COLOR_THINKING, COLOR_CODE, COLOR_DIM, 0)
        prefix_seg = ("▾ (thinking) ", COLOR_THINKING, 0)
        out = [[prefix_seg] + md_lines[0]] + md_lines[1:]
        return out, idx

    if role == "tool":
        name = (meta or {}).get("name", "?")
        full_header = f"tool: {name}({_format_tool_args((meta or {}).get('args'))})"
        if idx in collapsed_indices:
            summary = f"▸ {full_header} — click to expand"
            wrapped = textwrap.wrap(summary, wrap_width) or [""]
            return [[(wline, COLOR_TOOL, 0)] for wline in wrapped], idx
        header_lines = textwrap.wrap(f"▾ {full_header}", wrap_width) or [""]
        out = [[(hline, COLOR_TOOL, curses.A_BOLD)] for hline in header_lines]
        for raw_line in text.split("\n"):
            for wline in textwrap.wrap(raw_line, wrap_width) or [""]:
                out.append([(wline, COLOR_TOOL, 0)])
        return out, idx

    if role == "info":
        out = []
        for raw_line in text.split("\n"):
            for wline in textwrap.wrap(raw_line, wrap_width) or [""]:
                out.append([(wline, COLOR_DIM, 0)])
        return out, None

    return [[(text, COLOR_LIST, 0)]], None


def _handle_click(row, row_click_map, collapsed_indices):
    """Toggle the collapsed state of whatever collapsible block owns `row`."""
    idx = row_click_map.get(row)
    if idx is None:
        return False
    if idx in collapsed_indices:
        collapsed_indices.discard(idx)
    else:
        collapsed_indices.add(idx)
    return True


_TRANSCRIPT_LABELS = {
    "user": "USER",
    "assistant": "ASSISTANT",
    "assistant_live": "ASSISTANT",
    "thinking": "THINKING",
    "thinking_live": "THINKING",
    "tool": "TOOL",
    "error": "ERROR",
}


def _format_transcript(history, model_name):
    """Render the full history (uncollapsed, untruncated) as plain text."""
    lines = [
        f"Lumen transcript -- model: {model_name}",
        f"Saved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    for role, text, meta, ts in history:
        label = _TRANSCRIPT_LABELS.get(role, role.upper())
        if role == "tool":
            name = (meta or {}).get("name", "?")
            label = f"TOOL: {name}({_format_tool_args((meta or {}).get('args'))})"
        header = f"[{ts}] {label}" if ts else label
        lines.append(header)
        lines.append(text)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


CHAT_HELP_MESSAGE = """Commands:
/help - show this help
/save <path> - save the full transcript (including tool output) to a text file
/exit - quit Lumen

Shortcuts:
Enter - send message
Up/Down - browse input history
Ctrl+W - delete last word
Ctrl+S - edit system prompt
Ctrl+T - collapse/expand all thinking blocks
Esc / Ctrl+X - stop the model, stay in chat
Ctrl+N - start a new conversation
Ctrl+B - back to model picker
Ctrl+Q / Ctrl+C - interrupt and quit
PgUp/PgDn - scroll the transcript
click - collapse or expand a thinking/tool block"""


def edit_system_prompt_screen(stdscr):
    curses.curs_set(1)
    h, w = stdscr.getmaxyx()
    current = config_mod.load_system_prompt()

    stdscr.erase()
    draw_top_bar(stdscr, w)
    safe_addstr(stdscr, 2, 2, "Edit system prompt", curses.A_BOLD)
    safe_addstr(
        stdscr, 3, 2,
        "Ctrl+G save   Ctrl+C cancel",
        curses.color_pair(COLOR_DIM),
    )
    safe_addstr(
        stdscr, 4, 2,
        "Variables: $date and $year expand to today's date/year when sent to the model",
        curses.color_pair(COLOR_DIM),
    )

    box_top, box_left = 6, 2
    box_h = max(3, h - box_top - 1)
    box_w = max(10, w - box_left - 2)
    safe_addstr(stdscr, box_top - 1, box_left, "-" * box_w, curses.color_pair(COLOR_DIM))

    win = curses.newwin(box_h, box_w, box_top, box_left)
    win.keypad(True)

    wrapped = []
    for line in current.split("\n"):
        wrapped.extend(textwrap.wrap(line, max(1, box_w - 1)) or [""])
    for i, line in enumerate(wrapped[: box_h]):
        safe_addstr(win, i, 0, line[: box_w - 1])
    win.move(min(len(wrapped), box_h - 1), 0)
    stdscr.refresh()

    box = textpad.Textbox(win, insert_mode=True)

    def validator(ch):
        if ch == 3:  # Ctrl+C -> cancel
            raise _CancelEdit()
        if ch in (curses.KEY_BACKSPACE, 127):
            return 8  # map to BS so Textbox's default handling applies
        return ch

    try:
        box.edit(validator)
    except _CancelEdit:
        curses.curs_set(0)
        return False

    text = box.gather().strip()
    curses.curs_set(0)
    if text:
        config_mod.save_system_prompt(text)
        return True
    return False


def select_model_screen(stdscr, client, config):
    curses.curs_set(0)
    idx = 0
    models = []
    error = None
    status = "Loading models..."

    def fetch():
        nonlocal models, error, status
        try:
            models = client.list_models()
            error = None
        except OllamaError as e:
            models = []
            error = str(e)
        status = None

    fetch()
    default_name = config.get("default_model") or ""
    if default_name:
        for i, m in enumerate(models):
            if m.get("name") == default_name:
                idx = i
                break

    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        draw_top_bar(stdscr, w)
        safe_addstr(stdscr, 2, 2, "Select an Ollama model", curses.A_BOLD)
        safe_addstr(
            stdscr, 3, 2,
            "up/down move   Enter select   s edit system prompt   r refresh   q quit",
            curses.color_pair(COLOR_DIM),
        )

        if error:
            safe_addstr(stdscr, 5, 2, f"Error: {error}"[: w - 4], curses.color_pair(COLOR_ERROR))
            safe_addstr(stdscr, 6, 2, "Press r to retry, q to quit.", curses.color_pair(COLOR_DIM))
        elif not models:
            msg = status or "No models found. Is `ollama serve` running and have you pulled a model?"
            safe_addstr(stdscr, 5, 2, msg[: w - 4], curses.color_pair(COLOR_DIM))
        else:
            list_top = 5
            visible = max(1, h - list_top - 2)
            max_start = max(0, len(models) - visible)
            start = max(0, min(idx - visible // 2, max_start))
            for row, m in enumerate(models[start:start + visible]):
                mi = start + row
                name = m.get("name", "?")
                size_str = human_size(m.get("size", 0))
                line = f"{name:<40} {size_str:>10}"
                attr = curses.color_pair(COLOR_LIST) | curses.A_REVERSE if mi == idx else curses.color_pair(COLOR_LIST)
                safe_addstr(stdscr, list_top + row, 2, line[: w - 4].ljust(min(len(line), w - 4)), attr)

        stdscr.refresh()
        key = stdscr.getch()

        if key in (curses.KEY_UP, ord("k")):
            idx = max(0, idx - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            idx = min(max(0, len(models) - 1), idx + 1)
        elif key in (10, 13, curses.KEY_ENTER):
            if models:
                return models[idx]
        elif key in (ord("s"), ord("S")):
            edit_system_prompt_screen(stdscr)
            curses.curs_set(0)
        elif key in (ord("r"), ord("R")):
            status = "Loading models..."
            fetch()
        elif key in (ord("q"), ord("Q"), 27):
            return None
        elif key == curses.KEY_RESIZE:
            continue


def chat_screen(stdscr, client, model_info, config):
    model_name = model_info.get("name", "?")
    curses.curs_set(1)

    system_prompt = config_mod.load_system_prompt()
    options = {}
    if config.get("temperature") is not None:
        options["temperature"] = config["temperature"]
    if config.get("max_tokens"):
        options["num_predict"] = config["max_tokens"]
    tools_enabled = config.get("tools", {})

    convo = chat_mod.Conversation(
        client, model_name, config_mod.expand_system_prompt(system_prompt), tools_enabled, options
    )

    history = []
    input_buf = ""
    scroll = 0
    status_msg = ""
    collapsed_indices = set()
    row_click_map = {}
    input_history = []
    hist_idx = None
    hist_draft = ""

    def _history_prev():
        nonlocal input_buf, hist_idx, hist_draft
        if not input_history:
            return
        if hist_idx is None:
            hist_draft = input_buf
            hist_idx = len(input_history) - 1
        elif hist_idx > 0:
            hist_idx -= 1
        input_buf = input_history[hist_idx]

    def _history_next():
        nonlocal input_buf, hist_idx, hist_draft
        if hist_idx is None:
            return
        if hist_idx < len(input_history) - 1:
            hist_idx += 1
            input_buf = input_history[hist_idx]
        else:
            hist_idx = None
            input_buf = hist_draft

    HELP_TEXT = (
        "Ctrl+S prompt  Ctrl+T thinking  Esc/Ctrl+X stop  Ctrl+N new  Ctrl+B models  "
        "Ctrl+Q/C/exit quit"
    )

    def add_block(role, text, meta=None):
        history.append([role, text, meta, datetime.now().strftime("%H:%M:%S")])
        if role == "tool":
            collapsed_indices.add(len(history) - 1)
        return len(history) - 1

    view_state = {"h": 1}

    def render():
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        draw_top_bar(stdscr, w)

        help_row = h - 1
        status_row = h - 2
        input_row = h - 3
        sep_row = h - 4

        safe_hline(stdscr, sep_row, 0, curses.ACS_HLINE, max(0, w - 1))

        wrap_width = max(10, w - 4)
        lines = []
        owners = []
        lines.append([("", COLOR_LIST, 0)])
        owners.append(None)
        for idx, (role, text, meta, ts) in enumerate(history):
            entry_lines, owner = _entry_lines(idx, role, text, meta, ts, wrap_width, collapsed_indices)
            for eline in entry_lines:
                lines.append(eline)
                owners.append(owner)
            lines.append([("", COLOR_LIST, 0)])
            owners.append(None)

        start_row = 1
        view_h = max(1, sep_row - start_row)
        view_state["h"] = view_h
        max_start = max(0, len(lines) - view_h)
        top = max(0, max_start - scroll)
        visible = lines[top:top + view_h]
        visible_owners = owners[top:top + view_h]
        row_click_map.clear()
        for i, (segs, owner) in enumerate(zip(visible, visible_owners)):
            x = 2
            for text, color, extra in segs:
                if not text:
                    continue
                safe_addstr(stdscr, start_row + i, x, text, curses.color_pair(color) | extra)
                x += len(text)
            if owner is not None:
                row_click_map[start_row + i] = owner

        prompt = "> " + input_buf
        safe_addstr(stdscr, input_row, 0, prompt[: max(0, w - 1)])

        status_line = f"Model: {model_name}"
        if status_msg:
            status_line += f"   |   {status_msg}"
        safe_addstr(stdscr, status_row, 0, status_line[: max(0, w - 1)], curses.color_pair(COLOR_DIM))
        safe_addstr(stdscr, help_row, 0, HELP_TEXT[: max(0, w - 1)], curses.color_pair(COLOR_DIM))

        stdscr.move(input_row, min(w - 1, len(prompt)))
        stdscr.refresh()

    render()

    while True:
        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            return None

        if key in (17, 3):  # Ctrl+Q or Ctrl+C
            return None
        if key == 2:  # Ctrl+B
            return "back"
        if key == 14:  # Ctrl+N
            convo = chat_mod.Conversation(
                client, model_name, config_mod.expand_system_prompt(system_prompt),
                tools_enabled, options,
            )
            history.clear()
            status_msg = f"New conversation with {model_name}"
            scroll = 0
            render()
            continue
        if key == 19:  # Ctrl+S
            saved = edit_system_prompt_screen(stdscr)
            curses.curs_set(1)
            if saved:
                system_prompt = config_mod.load_system_prompt()
                convo.set_system_prompt(config_mod.expand_system_prompt(system_prompt))
                status_msg = "System prompt updated."
            render()
            continue
        if key == 20:  # Ctrl+T: bulk collapse/expand all thinking blocks
            thinking_idxs = [i for i, e in enumerate(history) if e[0] in ("thinking", "thinking_live")]
            if any(i not in collapsed_indices for i in thinking_idxs):
                collapsed_indices.update(thinking_idxs)
            else:
                collapsed_indices.difference_update(thinking_idxs)
            render()
            continue
        if key == curses.KEY_MOUSE:
            try:
                _, _mx, my, _, bstate = curses.getmouse()
            except curses.error:
                bstate = 0
                my = -1
            if bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_RELEASED):
                _handle_click(my, row_click_map, collapsed_indices)
            render()
            continue
        if key == curses.KEY_RESIZE:
            render()
            continue
        if key in (curses.KEY_BACKSPACE, 127, 8):
            input_buf = input_buf[:-1]
            render()
            continue
        if key == 23:  # Ctrl+W: delete last word
            input_buf = _erase_last_word(input_buf)
            render()
            continue
        if key == curses.KEY_UP:
            _history_prev()
            render()
            continue
        if key == curses.KEY_DOWN:
            _history_next()
            render()
            continue
        if key == curses.KEY_PPAGE:
            scroll += max(1, view_state["h"] - 1)
            render()
            continue
        if key == curses.KEY_NPAGE:
            scroll = max(0, scroll - max(1, view_state["h"] - 1))
            render()
            continue
        if key in (10, 13, curses.KEY_ENTER):
            text = input_buf.strip()
            input_buf = ""
            if not text:
                render()
                continue
            if text.lower() == "/exit":
                return None
            if text.lower() == "/help":
                add_block("info", CHAT_HELP_MESSAGE)
                scroll = 0
                render()
                continue
            if text.lower() == "/save" or text.lower().startswith("/save "):
                arg = text[len("/save"):].strip()
                if not arg:
                    status_msg = "Usage: /save <path>"
                else:
                    try:
                        target = Path(arg).expanduser()
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(_format_transcript(history, model_name))
                        status_msg = f"Transcript saved to {target}"
                    except OSError as e:
                        status_msg = f"Failed to save transcript: {e}"
                render()
                continue

            input_history.append(text)
            hist_idx = None
            hist_draft = ""

            add_block("user", text)
            status_msg = ""
            scroll = 0
            render()

            live = {"text": ""}
            think = {"text": ""}
            dirty = {"flag": False}

            def _finalize_thinking():
                if history and history[-1][0] == "thinking_live":
                    history[-1][0] = "thinking"
                think["text"] = ""

            # These callbacks run on the background generation thread, so
            # they only touch shared state + a dirty flag; only the main
            # thread ever calls render() (curses is not thread-safe).
            def on_thinking(chunk):
                think["text"] += chunk
                if history and history[-1][0] == "thinking_live":
                    history[-1][1] = think["text"]
                else:
                    add_block("thinking_live", think["text"])
                dirty["flag"] = True

            def on_content(chunk):
                _finalize_thinking()
                live["text"] += chunk
                if history and history[-1][0] == "assistant_live":
                    history[-1][1] = live["text"]
                else:
                    add_block("assistant_live", live["text"])
                dirty["flag"] = True

            def on_tool_call(name, args, result):
                nonlocal status_msg
                _finalize_thinking()
                add_block("tool", result, meta={"name": name, "args": args})
                status_msg = f"ran tool: {name}"
                dirty["flag"] = True

            def on_notice(message):
                _finalize_thinking()
                add_block("info", message)
                dirty["flag"] = True

            stop_event = threading.Event()
            outcome = {"error": None, "done": False}

            def worker():
                try:
                    convo.send(
                        text,
                        on_content=on_content,
                        on_tool_call=on_tool_call,
                        on_thinking=on_thinking,
                        on_notice=on_notice,
                        stop_event=stop_event,
                    )
                except OllamaError as e:
                    outcome["error"] = e
                finally:
                    outcome["done"] = True

            gen_thread = threading.Thread(target=worker, daemon=True)
            gen_thread.start()

            stdscr.timeout(80)
            interrupted = False
            while not outcome["done"]:
                if dirty["flag"]:
                    render()
                    dirty["flag"] = False
                k = stdscr.getch()
                if k == -1:
                    continue
                if k in (3, 17):  # Ctrl+C / Ctrl+Q: interrupt + quit now
                    stop_event.set()
                    interrupted = True
                    break
                if k in (24, 27):  # Ctrl+X / Esc: interrupt generation, stay in chat
                    stop_event.set()
                    status_msg = "Generation stopped."
                    break
                if k == curses.KEY_RESIZE:
                    dirty["flag"] = True
                elif k in (curses.KEY_BACKSPACE, 127, 8):
                    input_buf = input_buf[:-1]
                    dirty["flag"] = True
                elif k == 23:  # Ctrl+W: delete last word
                    input_buf = _erase_last_word(input_buf)
                    dirty["flag"] = True
                elif k == curses.KEY_UP:
                    _history_prev()
                    dirty["flag"] = True
                elif k == curses.KEY_DOWN:
                    _history_next()
                    dirty["flag"] = True
                elif k in (10, 13, curses.KEY_ENTER):
                    if input_buf.strip().lower() == "/exit":
                        stop_event.set()
                        interrupted = True
                        break
                elif 32 <= k < 127:
                    input_buf += chr(k)
                    dirty["flag"] = True
            stdscr.timeout(-1)

            if interrupted:
                gen_thread.join(timeout=2.0)
                return None

            gen_thread.join(timeout=2.0)
            if outcome["error"] is not None:
                _finalize_thinking()
                add_block("error", str(outcome["error"]))
            else:
                _finalize_thinking()
                if history and history[-1][0] == "assistant_live":
                    history[-1][0] = "assistant"
                elif live["text"]:
                    add_block("assistant", live["text"])
            render()
            continue

        if 32 <= key < 127:
            input_buf += chr(key)
            render()
            continue
        # ignore anything else (function keys, etc.)


def run(stdscr, host_override=None):
    curses.curs_set(0)
    curses.raw()  # disable ^S/^Q flow control and ^C/^Z signal chars so
                  # our own Ctrl-key bindings (and ^C-to-cancel) actually
                  # reach getch() instead of being intercepted by the tty
    init_colors()
    stdscr.keypad(True)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)

    config = config_mod.load_config()
    if host_override:
        config["ollama_host"] = host_override

    from .ollama_client import OllamaClient

    client = OllamaClient(config["ollama_host"])

    while True:
        model_info = select_model_screen(stdscr, client, config)
        if not model_info:
            return
        config["default_model"] = model_info.get("name", "")
        try:
            config_mod.save_config(config)
        except Exception:
            pass
        result = chat_screen(stdscr, client, model_info, config)
        if result != "back":
            return
