#!/usr/bin/env python3
"""Cursor hook entrypoint for VibeCodingLed."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
VIBE_SCRIPT = SCRIPT_DIR / "vibe_led.py"
SOURCE = "cursor"
THREAD_ID = "cursor-agent"


def read_stdin_json() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def run_vibe(args: list[str]) -> int:
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW
    completed = subprocess.run(
        [sys.executable, str(VIBE_SCRIPT), *args],
        text=True,
        capture_output=True,
        check=False,
        creationflags=creationflags,
    )
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    return completed.returncode


def main(argv: list[str]) -> int:
    action = argv[0] if argv else ""
    payload = read_stdin_json()

    if action in {"turn-start", "busy", "pre-tool"}:
        return run_vibe(["thread", "start", THREAD_ID, "--source", SOURCE])

    if action == "stop":
        status = str(payload.get("status", "")).lower()
        reason = "completed" if status == "completed" else "problem" if status in {"error", "aborted"} else "stopped"
        return run_vibe(
            [
                "thread",
                "stop",
                THREAD_ID,
                "--source",
                SOURCE,
                "--reason",
                reason,
            ]
        )

    if action in {"session-end", "idle"}:
        return run_vibe(["thread", "clear", THREAD_ID])

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
