#!/usr/bin/env python3
"""Monitor Codex task_count and sync VibeCodingLed over USB HID.

This is a pragmatic fallback for Codex environments where the official
thread-list tool is unavailable to normal scripts. It reads Codex's persisted
global state, watches the active task count, and maps it to the product's
three LED states.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_INTERVAL_SECONDS = 1.0
DEFAULT_STATE_DIR = Path(
    os.environ.get("VIBE_LED_STATE_DIR", str(Path.home() / ".vibecodingled"))
)
MONITOR_STATE_FILE = DEFAULT_STATE_DIR / "codex_task_count_monitor.json"


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def default_codex_state_path() -> Path:
    return Path.home() / ".codex" / ".codex-global-state.json"


def nested_get(data: dict[str, Any], path: list[str]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def read_task_count(path: Path) -> int:
    data = read_json(path)
    candidates = [
        nested_get(
            data,
            ["electron-persisted-atom-state", "environment", "task_count"],
        ),
        nested_get(data, ["environment", "task_count"]),
        data.get("task_count"),
    ]
    for value in candidates:
        if isinstance(value, int) and value >= 0:
            return value
    return 0


def run_vibe(vibe_script: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW
    return subprocess.run(
        [sys.executable, str(vibe_script), *args],
        text=True,
        capture_output=True,
        check=False,
        creationflags=creationflags,
    )


def parse_aggregate(stdout: str, fallback: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("aggregate="):
            return line.split("=", 1)[1].split(" ", 1)[0].strip() or fallback
    return fallback


def highest_virtual_index(thread_ids: list[str], source: str) -> int:
    highest = 0
    pattern = re.compile(rf"^{re.escape(source)}-active-(\d+)$")
    for thread_id in thread_ids:
        match = pattern.match(thread_id)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest


def previous_active_ids(
    monitor_state: dict[str, Any], source: str, previous_count: int
) -> list[str]:
    stored = monitor_state.get("active_thread_ids")
    if isinstance(stored, list):
        active = [str(item) for item in stored if str(item).strip()]
        if active:
            return active

    if previous_count > 0:
        return [f"{source}-active-{index}" for index in range(1, previous_count + 1)]
    return []


def plan_active_ids(
    count: int, previous_ids: list[str], next_index: int, source: str
) -> tuple[list[str], int]:
    next_index = max(next_index, highest_virtual_index(previous_ids, source) + 1, 1)
    active_ids = list(previous_ids)

    if count < len(active_ids):
        active_ids = active_ids[:count]

    while len(active_ids) < count:
        active_ids.append(f"{source}-active-{next_index}")
        next_index += 1

    return active_ids, next_index


def sync_once(codex_state: Path, vibe_script: Path, source: str) -> int:
    count = read_task_count(codex_state)
    monitor_state = read_json(MONITOR_STATE_FILE)
    previous_count = int(monitor_state.get("last_task_count", 0) or 0)
    next_index = int(monitor_state.get("next_virtual_index", 1) or 1)
    previous_ids = previous_active_ids(monitor_state, source, previous_count)

    if count <= 0:
        result = run_vibe(vibe_script, ["thread", "reset"])
        active_ids: list[str] = []
        next_index = 1
        aggregate = parse_aggregate(result.stdout, "off")
    else:
        active_ids, next_index = plan_active_ids(count, previous_ids, next_index, source)
        result = run_vibe(
            vibe_script,
            ["thread", "sync", "--source", source, *active_ids],
        )
        aggregate = parse_aggregate(
            result.stdout,
            "stopped" if previous_count > count else "running",
        )

    monitor_state.update(
        {
            "last_task_count": count,
            "last_expected_aggregate": aggregate,
            "active_thread_ids": active_ids,
            "next_virtual_index": next_index,
            "last_returncode": result.returncode,
            "last_stdout": result.stdout[-2000:],
            "last_stderr": result.stderr[-2000:],
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "codex_state": str(codex_state),
            "vibe_script": str(vibe_script),
            "fallback_identity_note": (
                "Codex fallback only exposes task_count, so virtual thread IDs "
                "are maintained locally. When the count drops, the newest "
                "virtual active threads are treated as stopped."
            ),
        }
    )
    write_json(MONITOR_STATE_FILE, monitor_state)

    if result.returncode != 0:
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
    else:
        print(
            f"task_count={count} previous={previous_count} "
            f"expected={aggregate}"
        )
        print(result.stdout, end="")
    return result.returncode


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor Codex task_count and update VibeCodingLed."
    )
    parser.add_argument(
        "--codex-state",
        type=Path,
        default=default_codex_state_path(),
        help="Path to Codex .codex-global-state.json.",
    )
    parser.add_argument(
        "--vibe-script",
        type=Path,
        default=Path(__file__).with_name("vibe_led.py"),
        help="Path to vibe_led.py.",
    )
    parser.add_argument("--source", default="codex")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.once:
        return sync_once(args.codex_state, args.vibe_script, args.source)

    while True:
        sync_once(args.codex_state, args.vibe_script, args.source)
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
