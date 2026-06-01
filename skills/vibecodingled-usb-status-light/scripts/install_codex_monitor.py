#!/usr/bin/env python3
"""Install and start the VibeCodingLed Codex monitor."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
INSTALL_SCRIPT = Path(__file__).resolve()
MONITOR_SCRIPT = SCRIPT_DIR / "codex_session_monitor.py"
STATE_DIR = Path(os.environ.get("VIBE_LED_STATE_DIR", str(Path.home() / ".vibecodingled")))
PID_FILE = STATE_DIR / "codex_session_monitor.pid"
LOG_FILE = STATE_DIR / "codex_session_monitor.log"
STARTUP_LOG_FILE = STATE_DIR / "codex_session_startup.log"
LEGACY_PID_FILE = STATE_DIR / "codex_task_count_monitor.pid"
STARTUP_NAME = "VibeCodingLed-Codex-Monitor"


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA is not set; Windows startup folder is unavailable.")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def hidden_creationflags() -> int:
    if sys.platform == "win32":
        return subprocess.CREATE_NO_WINDOW
    return 0


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    output_encoding = "mbcs" if sys.platform == "win32" else "utf-8"
    completed = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        text=True,
        encoding=output_encoding,
        errors="replace",
        capture_output=True,
        check=False,
        creationflags=hidden_creationflags(),
    )
    return str(pid) in (completed.stdout or "")


def existing_pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def stop_pid(pid: int) -> None:
    if pid <= 0 or not is_pid_running(pid):
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            text=True,
            capture_output=True,
            check=False,
            creationflags=hidden_creationflags(),
        )
    else:
        subprocess.run(["kill", str(pid)], check=False)


def stop_legacy_monitor() -> None:
    try:
        pid = int(LEGACY_PID_FILE.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return
    stop_pid(pid)
    try:
        LEGACY_PID_FILE.unlink()
    except OSError:
        pass


def write_startup_entry() -> Path:
    path = startup_dir() / f"{STARTUP_NAME}.vbs"
    old_cmd = startup_dir() / f"{STARTUP_NAME}.cmd"
    path.parent.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if old_cmd.exists():
        old_cmd.unlink()
    content = (
        'Set shell = CreateObject("WScript.Shell")\r\n'
        'cmd = "cmd.exe /c ""'
        f'set """"VIBE_LED_STATE_DIR={STATE_DIR}"""" && '
        f'""""{sys.executable}"""" """"{INSTALL_SCRIPT}"""" '
        f'>> """"{STARTUP_LOG_FILE}"""" 2>&1'
        '"""\r\n'
        "shell.Run cmd, 0, False\r\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


def start_monitor() -> int:
    stop_legacy_monitor()
    pid = existing_pid()
    if pid and is_pid_running(pid):
        stop_pid(pid)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(MONITOR_SCRIPT),
            "--interval",
            "1",
            "--reset-monitor-state",
        ],
        stdout=LOG_FILE.open("a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    PID_FILE.write_text(str(proc.pid), encoding="ascii")
    print(f"monitor started pid={proc.pid}")
    return proc.pid


def main() -> int:
    if sys.platform != "win32":
        print("startup install is currently implemented for Windows only.")
        start_monitor()
        return 0

    startup = write_startup_entry()
    print(f"startup hidden launcher installed: {startup}")
    start_monitor()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
