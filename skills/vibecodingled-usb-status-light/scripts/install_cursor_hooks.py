#!/usr/bin/env python3
"""Install Cursor hooks for VibeCodingLed."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
CURSOR_DIR = Path.home() / ".cursor"
HOOK_DIR = CURSOR_DIR / "hooks" / "vibecodingled"
HOOKS_JSON = CURSOR_DIR / "hooks.json"


def read_json(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "hooks": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
        return {"version": 1, "hooks": {}}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def hook_command(action: str) -> str:
    return f'"{sys.executable}" "{HOOK_DIR / "cursor_hook.py"}" {action}'


def ensure_hook(data: dict, event: str, action: str, matcher: str | None = None) -> None:
    hooks = data.setdefault("hooks", {})
    items = hooks.setdefault(event, [])
    command = hook_command(action)

    for item in items:
        if isinstance(item, dict) and "vibecodingled" in str(item.get("command", "")):
            item["command"] = command
            if matcher:
                item["matcher"] = matcher
            return

    item = {"command": command}
    if matcher:
        item["matcher"] = matcher
    items.append(item)


def install() -> None:
    HOOK_DIR.mkdir(parents=True, exist_ok=True)
    for name in ["vibe_led.py", "cursor_hook.py"]:
        shutil.copy2(SCRIPT_DIR / name, HOOK_DIR / name)

    data = read_json(HOOKS_JSON)
    data.setdefault("version", 1)

    ensure_hook(data, "beforeSubmitPrompt", "turn-start", "UserPromptSubmit")
    ensure_hook(data, "preToolUse", "busy")
    ensure_hook(data, "stop", "stop", "Stop")
    ensure_hook(data, "sessionEnd", "session-end")

    write_json(HOOKS_JSON, data)
    print(f"installed Cursor hooks: {HOOK_DIR}")
    print(f"updated: {HOOKS_JSON}")


if __name__ == "__main__":
    install()
