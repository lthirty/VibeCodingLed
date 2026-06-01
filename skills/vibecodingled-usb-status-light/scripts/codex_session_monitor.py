#!/usr/bin/env python3
"""Monitor Codex session JSONL events and sync VibeCodingLed over USB HID.

Codex Desktop writes each conversation to a rollout JSONL file. The relevant
events are explicit: ``task_started`` when a turn begins, and ``task_complete``
or ``turn_aborted`` when that turn stops. This monitor tails those files and
translates the events into the shared VibeCodingLed thread model.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_INTERVAL_SECONDS = 1.0
DEFAULT_REAPPLY_SECONDS = 5.0
DEFAULT_MAX_THREADS = 80
DEFAULT_ACTIVE_WINDOW_HOURS = 24.0
DEFAULT_STATE_DIR = Path(
    os.environ.get("VIBE_LED_STATE_DIR", str(Path.home() / ".vibecodingled"))
)
MONITOR_STATE_FILE = DEFAULT_STATE_DIR / "codex_session_monitor.json"
THREAD_STATE_FILE = DEFAULT_STATE_DIR / "threads.json"
LEGACY_VIRTUAL_THREAD_RE = re.compile(r"^codex-active-\d+$")
STOP_EVENTS = {"task_complete", "turn_aborted"}


@dataclass(frozen=True)
class CodexThread:
    thread_id: str
    rollout_path: Path
    updated_at_ms: int


@dataclass(frozen=True)
class CodexEvent:
    sort_key: tuple[int, str, int]
    thread_id: str
    turn_id: str
    event_type: str

    @property
    def led_thread_id(self) -> str:
        return f"codex:{self.thread_id}"


def default_codex_state_db() -> Path:
    return Path.home() / ".codex" / "state_5.sqlite"


def default_vibe_script() -> Path:
    return Path(__file__).with_name("vibe_led.py")


def normalize_windows_path(value: str) -> Path:
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{int(time.time() * 1000)}.tmp")
    tmp.write_text(text, encoding="utf-8")

    last_error: OSError | None = None
    for _ in range(10):
        try:
            tmp.replace(path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.05)

    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        last_error = exc
        print(f"warning: failed to write monitor state {path}: {exc}", file=sys.stderr)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            if last_error is not None:
                print(f"warning: stale temp state file left at {tmp}", file=sys.stderr)


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


def read_recent_threads(
    state_db: Path,
    max_threads: int,
    active_window_hours: float,
) -> list[CodexThread]:
    if not state_db.exists():
        return []

    cutoff_ms = int((time.time() - max(1.0, active_window_hours) * 3600) * 1000)
    try:
        con = sqlite3.connect(str(state_db), timeout=1.0)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            select id, rollout_path, coalesce(updated_at_ms, updated_at * 1000, 0) as updated
            from threads
            where rollout_path is not null and rollout_path != ''
              and coalesce(updated_at_ms, updated_at * 1000, 0) >= ?
              and (thread_source = 'user' or source = 'vscode')
            order by updated desc
            limit ?
            """,
            (cutoff_ms, max_threads),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        try:
            con.close()  # type: ignore[name-defined]
        except Exception:
            pass

    threads: list[CodexThread] = []
    seen_paths: set[str] = set()
    for row in rows:
        rollout_path = normalize_windows_path(str(row["rollout_path"]))
        path_key = str(rollout_path).lower()
        if path_key in seen_paths or not rollout_path.exists():
            continue
        seen_paths.add(path_key)
        threads.append(
            CodexThread(
                thread_id=str(row["id"]),
                rollout_path=rollout_path,
                updated_at_ms=int(row["updated"] or 0),
            )
        )
    return threads


def event_timestamp(event: dict[str, Any], fallback_line: int) -> int:
    for key in ("started_at", "completed_at"):
        value = event.get(key)
        if isinstance(value, int):
            return value
    return fallback_line


def parse_event_line(
    raw_line: str,
    thread_id: str,
    path_key: str,
    line_no: int,
) -> CodexEvent | None:
    try:
        record = json.loads(raw_line)
    except json.JSONDecodeError:
        return None

    if record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    event = payload.get("event_msg") or payload
    if not isinstance(event, dict):
        return None

    event_type = str(event.get("type") or "")
    if event_type not in {"task_started", *STOP_EVENTS}:
        return None

    turn_id = str(event.get("turn_id") or "").strip()
    if not turn_id:
        return None

    return CodexEvent(
        sort_key=(event_timestamp(event, line_no), path_key, line_no),
        thread_id=thread_id,
        turn_id=turn_id,
        event_type=event_type,
    )


def parse_full_file(thread: CodexThread) -> tuple[dict[str, str], int, int]:
    active: dict[str, str] = {}
    line_no = 0
    try:
        with thread.rollout_path.open("r", encoding="utf-8", errors="replace") as fp:
            for line_no, line in enumerate(fp, start=1):
                event = parse_event_line(
                    line,
                    thread.thread_id,
                    str(thread.rollout_path).lower(),
                    line_no,
                )
                if not event:
                    continue
                if event.event_type == "task_started":
                    active[thread.thread_id] = event.turn_id
                elif active.get(thread.thread_id) == event.turn_id:
                    active.pop(thread.thread_id, None)
        offset = thread.rollout_path.stat().st_size
    except OSError:
        return {}, 0, 0
    return active, offset, line_no


def read_new_events(
    thread: CodexThread,
    file_state: dict[str, Any],
) -> tuple[list[CodexEvent], dict[str, Any]]:
    path = thread.rollout_path
    offset = int(file_state.get("offset", 0) or 0)
    line_no = int(file_state.get("line_no", 0) or 0)

    try:
        size = path.stat().st_size
        if offset > size:
            offset = 0
            line_no = 0

        events: list[CodexEvent] = []
        with path.open("rb") as fp:
            fp.seek(offset)
            chunk = fp.read()
        if not chunk:
            return events, {"offset": offset, "line_no": line_no}

        last_newline = chunk.rfind(b"\n")
        if last_newline < 0:
            return events, {"offset": offset, "line_no": line_no}

        complete_chunk = chunk[: last_newline + 1]
        text = complete_chunk.decode("utf-8", errors="replace")
        for raw in text.splitlines(keepends=True):
            line_no += 1
            event = parse_event_line(
                raw,
                thread.thread_id,
                str(path).lower(),
                line_no,
            )
            if event:
                events.append(event)

        return events, {
            "offset": offset + len(complete_chunk),
            "line_no": line_no,
            "updated_at_ms": thread.updated_at_ms,
        }
    except OSError:
        return [], file_state


def bootstrap_state(threads: list[CodexThread]) -> tuple[dict[str, Any], list[str]]:
    files: dict[str, Any] = {}
    active_threads: dict[str, str] = {}
    for thread in threads:
        active, offset, line_no = parse_full_file(thread)
        active_threads.update(active)
        files[str(thread.rollout_path)] = {
            "offset": offset,
            "line_no": line_no,
            "pending": "",
            "updated_at_ms": thread.updated_at_ms,
        }

    led_thread_ids = [f"codex:{thread_id}" for thread_id in sorted(active_threads)]
    state = {
        "initialized": True,
        "files": files,
        "active_turns": active_threads,
        "last_events": [],
        "updated_at": now_iso(),
    }
    return state, led_thread_ids


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def clear_existing_source_threads(vibe_script: Path, source: str) -> None:
    data = read_json(THREAD_STATE_FILE)
    threads = data.get("threads")
    if not isinstance(threads, dict):
        return
    source_thread_ids = [
        thread_id
        for thread_id, item in threads.items()
        if isinstance(item, dict) and item.get("source") == source
    ]
    for thread_id in source_thread_ids:
        run_vibe(vibe_script, ["thread", "clear", str(thread_id)])


def apply_initial_sync(vibe_script: Path, source: str, active_ids: list[str]) -> int:
    clear_existing_source_threads(vibe_script, source)
    result = run_vibe(vibe_script, ["thread", "sync", "--source", source, *active_ids])
    print(result.stdout, end="")
    if result.returncode != 0:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def apply_event(vibe_script: Path, source: str, event: CodexEvent) -> int:
    if event.event_type == "task_started":
        args = ["thread", "start", event.led_thread_id, "--source", source]
    else:
        reason = "problem" if event.event_type == "turn_aborted" else "completed"
        args = [
            "thread",
            "stop",
            event.led_thread_id,
            "--reason",
            reason,
            "--source",
            source,
        ]

    result = run_vibe(vibe_script, args)
    print(result.stdout, end="")
    if result.returncode != 0:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def acknowledge_source(vibe_script: Path, source: str) -> int:
    result = run_vibe(vibe_script, ["thread", "ack", "--source", source])
    print(result.stdout, end="")
    if result.returncode != 0:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def apply_active_snapshot(
    vibe_script: Path,
    source: str,
    active_turns: dict[str, Any],
) -> int:
    active_ids = [
        f"codex:{thread_id}"
        for thread_id in sorted(active_turns)
        if str(thread_id).strip()
    ]
    result = run_vibe(vibe_script, ["thread", "sync", "--source", source, *active_ids])
    print(result.stdout, end="")
    if result.returncode != 0:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def sync_once(
    state_db: Path,
    vibe_script: Path,
    source: str,
    max_threads: int,
    active_window_hours: float,
    reapply_seconds: float,
) -> int:
    threads = read_recent_threads(state_db, max_threads, active_window_hours)
    monitor_state = read_json(MONITOR_STATE_FILE)

    if not monitor_state.get("initialized"):
        monitor_state, active_ids = bootstrap_state(threads)
        rc = apply_initial_sync(vibe_script, source, active_ids)
        monitor_state["last_sync"] = {
            "kind": "bootstrap",
            "active_count": len(active_ids),
            "returncode": rc,
            "updated_at": now_iso(),
        }
        write_json(MONITOR_STATE_FILE, monitor_state)
        print(f"codex_session bootstrap active_count={len(active_ids)}")
        return rc

    files = monitor_state.setdefault("files", {})
    if not isinstance(files, dict):
        files = {}
        monitor_state["files"] = files

    events: list[CodexEvent] = []
    for thread in threads:
        path_key = str(thread.rollout_path)
        file_state = files.get(path_key)
        if not isinstance(file_state, dict):
            file_state = {"offset": 0, "line_no": 0, "pending": ""}
        new_events, new_state = read_new_events(thread, file_state)
        events.extend(new_events)
        files[path_key] = new_state

    events.sort(key=lambda item: item.sort_key)

    active_turns = monitor_state.setdefault("active_turns", {})
    if not isinstance(active_turns, dict):
        active_turns = {}
        monitor_state["active_turns"] = active_turns

    returncode = 0
    applied: list[dict[str, str]] = []
    for event in events:
        if event.event_type == "task_started":
            if not active_turns:
                returncode = returncode or acknowledge_source(vibe_script, source)
            active_turns[event.thread_id] = event.turn_id
        else:
            if active_turns.get(event.thread_id) == event.turn_id:
                active_turns.pop(event.thread_id, None)
            else:
                active_turns.pop(event.thread_id, None)
        rc = apply_event(vibe_script, source, event)
        returncode = returncode or rc
        applied.append(
            {
                "thread_id": event.thread_id,
                "turn_id": event.turn_id,
                "event_type": event.event_type,
            }
        )

    monitor_state["last_events"] = applied[-20:]
    monitor_state["last_sync"] = {
        "kind": "events",
        "event_count": len(events),
        "active_count": len(active_turns),
        "returncode": returncode,
        "updated_at": now_iso(),
    }
    monitor_state["state_db"] = str(state_db)
    monitor_state["vibe_script"] = str(vibe_script)

    if events:
        print(f"codex_session events={len(events)} active_count={len(active_turns)}")
    else:
        now = time.time()
        last_reapply_at = float(monitor_state.get("last_reapply_at", 0) or 0)
        if now - last_reapply_at >= max(1.0, reapply_seconds):
            rc = apply_active_snapshot(vibe_script, source, active_turns)
            returncode = returncode or rc
            monitor_state["last_reapply_at"] = now

        last_idle_log_at = float(monitor_state.get("last_idle_log_at", 0) or 0)
        if now - last_idle_log_at >= 60:
            print(f"codex_session idle active_count={len(active_turns)}")
            monitor_state["last_idle_log_at"] = now

    write_json(MONITOR_STATE_FILE, monitor_state)
    return returncode


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor Codex session events and update VibeCodingLed."
    )
    parser.add_argument(
        "--state-db",
        type=Path,
        default=default_codex_state_db(),
        help="Path to Codex state_5.sqlite.",
    )
    parser.add_argument(
        "--vibe-script",
        type=Path,
        default=default_vibe_script(),
        help="Path to vibe_led.py.",
    )
    parser.add_argument("--source", default="codex")
    parser.add_argument("--max-threads", type=int, default=DEFAULT_MAX_THREADS)
    parser.add_argument(
        "--active-window-hours",
        type=float,
        default=DEFAULT_ACTIVE_WINDOW_HOURS,
        help=(
            "Only scan Codex threads updated within this many hours. "
            "This prevents historical unfinished sessions from being treated "
            "as currently running."
        ),
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--reset-monitor-state", action="store_true")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument(
        "--reapply-seconds",
        type=float,
        default=DEFAULT_REAPPLY_SECONDS,
        help=(
            "Re-send the current aggregate state this often even when Codex "
            "has no new events. This lets the device recover after USB "
            "unplug/replug because firmware state resets on power loss."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.reset_monitor_state:
        try:
            MONITOR_STATE_FILE.unlink()
        except FileNotFoundError:
            pass

    if args.once:
        return sync_once(
            args.state_db,
            args.vibe_script,
            args.source,
            args.max_threads,
            args.active_window_hours,
            args.reapply_seconds,
        )

    while True:
        sync_once(
            args.state_db,
            args.vibe_script,
            args.source,
            args.max_threads,
            args.active_window_hours,
            args.reapply_seconds,
        )
        time.sleep(max(0.5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
