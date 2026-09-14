"""Entry point: `lumen` / `python -m lumen` / `python run.py`."""

import argparse
import curses

from . import config as config_mod
from . import tui


def main():
    parser = argparse.ArgumentParser(
        prog="lumen",
        description="Pure-Python ncurses harness for chatting with local Ollama models.",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Override the Ollama host from config.hcl (e.g. http://localhost:11434)",
    )
    parser.add_argument(
        "-s", "--systemprompt",
        dest="systemprompt",
        default=None,
        metavar="PATH",
        help=(
            "Path to an alternate system prompt text file (default: "
            "~/.config/lumen/systemprompt.txt). Created with the default "
            "prompt if it doesn't exist yet."
        ),
    )
    args = parser.parse_args()

    config_mod.ensure_files()

    try:
        curses.wrapper(tui.run, args.host, args.systemprompt)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
