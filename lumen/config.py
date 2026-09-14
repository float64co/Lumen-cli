"""Load/save Lumen's config.hcl and systemprompt.txt under ~/.config/lumen/."""

import re
from datetime import date
from pathlib import Path

from . import hcl

CONFIG_DIR = Path.home() / ".config" / "lumen"
CONFIG_FILE = CONFIG_DIR / "config.hcl"
SYSTEM_PROMPT_FILE = CONFIG_DIR / "systemprompt.txt"
SYSTEM_PROMPTS_DIR = CONFIG_DIR / "systemprompts"

DEFAULT_CONFIG = {
    "ollama_host": "http://localhost:11434",
    "default_model": "",
    "temperature": 0.7,
    "max_tokens": 2048,
    "tools": {
        "web_fetch": True,
        "search": True,
    },
}

DEFAULT_SYSTEM_PROMPT = (Path(__file__).parent / "default_systemprompt.txt").read_text().strip()


def _merge_defaults(cfg, defaults):
    out = dict(defaults)
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_defaults(v, out[k])
        else:
            out[k] = v
    return out


def ensure_files():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(hcl.dumps(DEFAULT_CONFIG) + "\n")
    if not SYSTEM_PROMPT_FILE.exists():
        SYSTEM_PROMPT_FILE.write_text(DEFAULT_SYSTEM_PROMPT + "\n")


def load_config():
    ensure_files()
    try:
        raw = hcl.loads(CONFIG_FILE.read_text())
    except Exception:
        raw = {}
    return _merge_defaults(raw, DEFAULT_CONFIG)


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(hcl.dumps(cfg) + "\n")


def load_system_prompt(path=None):
    """Load a system prompt. With no `path`, uses the default
    ~/.config/lumen/systemprompt.txt (bootstrapped if missing). A custom
    `path` is bootstrapped with the default prompt too if it doesn't exist
    yet, so -s/--systemprompt works the same way for a brand-new file.
    """
    if path is None:
        ensure_files()
        target = SYSTEM_PROMPT_FILE
    else:
        target = Path(path).expanduser()
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(DEFAULT_SYSTEM_PROMPT + "\n")
    try:
        text = target.read_text()
    except Exception:
        return DEFAULT_SYSTEM_PROMPT
    return text.strip("\n") or DEFAULT_SYSTEM_PROMPT


def save_system_prompt(text, path=None):
    target = Path(path).expanduser() if path else SYSTEM_PROMPT_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    text = text.rstrip("\n")
    target.write_text(text + "\n")


def list_system_prompts():
    """Files in ~/.config/lumen/systemprompts/, for the Ctrl+P picker.

    Bootstrapped with one starter file on first use so the picker is never
    empty; left alone once anything exists there.
    """
    SYSTEM_PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in SYSTEM_PROMPTS_DIR.iterdir() if p.is_file())
    if not files:
        starter = SYSTEM_PROMPTS_DIR / "default.txt"
        starter.write_text(DEFAULT_SYSTEM_PROMPT + "\n")
        files = [starter]
    return files


def expand_system_prompt(text):
    """Substitute the $date / $year variables with today's values.

    The saved system prompt keeps the literal placeholders; only the copy
    handed to the model has them expanded, so they stay current across
    long-running sessions.
    """
    today = date.today()
    text = re.sub(r"\$date\b", today.isoformat(), text)
    text = re.sub(r"\$year\b", str(today.year), text)
    return text
