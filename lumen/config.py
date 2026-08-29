"""Load/save Lumen's config.hcl and systemprompt.txt under ~/.config/lumen/."""

import re
from datetime import date
from pathlib import Path

from . import hcl

CONFIG_DIR = Path.home() / ".config" / "lumen"
CONFIG_FILE = CONFIG_DIR / "config.hcl"
SYSTEM_PROMPT_FILE = CONFIG_DIR / "systemprompt.txt"

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

DEFAULT_SYSTEM_PROMPT = (
    "You are Lumen, a concise and helpful assistant running on a local "
    "Ollama model. Use the web_fetch and search tools when you need "
    "up-to-date or external information that you are not confident about. "
    "Keep answers direct and avoid unnecessary padding."
)


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


def load_system_prompt():
    ensure_files()
    try:
        text = SYSTEM_PROMPT_FILE.read_text()
    except Exception:
        return DEFAULT_SYSTEM_PROMPT
    return text.strip("\n") or DEFAULT_SYSTEM_PROMPT


def save_system_prompt(text):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    text = text.rstrip("\n")
    SYSTEM_PROMPT_FILE.write_text(text + "\n")


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
