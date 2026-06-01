#!/usr/bin/env python3
"""PC-side controller for the VibeCodingLed ATtiny85 HID firmware."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


VID = 0x16C0
PID = 0x05DF
BOOTLOADER_VID = 0x16D0
BOOTLOADER_PID = 0x0753
REPORT_ID = 1
MAGIC = 0x56
PROTOCOL_VERSION = 1

DEFAULT_STATE_DIR = Path(
    os.environ.get("VIBE_LED_STATE_DIR", str(Path.home() / ".vibecodingled"))
)
THREAD_STATE_FILE = DEFAULT_STATE_DIR / "threads.json"
THREAD_LOCK_FILE = DEFAULT_STATE_DIR / "threads.lock"
SETTINGS_FILE = DEFAULT_STATE_DIR / "settings.json"
DEFAULT_BUZZER_PWM_DUTY = 8
BUZZER_PWM_DUTY_STEPS = [2, 4, 8, 16, 24, 32, 48, 64, 96, 128]


STATES: dict[str, int] = {
    "off": 0,
    "running": 1,
    "partial": 2,
    "done": 3,
    # Backward-compatible alias for old scripts and docs.
    "stopped": 2,
}

STATE_LABELS = {
    0: "off",
    1: "running",
    2: "partial",
    3: "done",
}

ERRORS = {
    0: "ok",
    1: "bad-length",
    2: "bad-magic",
    3: "bad-version",
    4: "bad-checksum",
    5: "bad-mode",
}


@dataclass
class Command:
    state: str
    brightness: int = 255
    rate: int = 0
    flags: int = 0
    seq: int = 0


def import_hid():
    try:
        import hid  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            "Missing Python package 'hidapi'. Install it with:\n"
            "  python -m pip install --user hidapi"
        ) from exc
    return hid


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def load_settings() -> dict[str, Any]:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(data: dict[str, Any]) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp_path = SETTINGS_FILE.with_name(
        f"{SETTINGS_FILE.name}.{os.getpid()}.{int(time.time() * 1000)}.tmp"
    )
    tmp_path.write_text(text, encoding="utf-8")
    try:
        tmp_path.replace(SETTINGS_FILE)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def normalize_buzzer_pwm_duty(value: Any) -> int:
    try:
        duty = int(value)
    except (TypeError, ValueError):
        return DEFAULT_BUZZER_PWM_DUTY
    if duty < 1:
        return 1
    if duty > 254:
        return 254
    return duty


def current_buzzer_pwm_duty() -> int:
    return normalize_buzzer_pwm_duty(
        load_settings().get("buzzer_pwm_duty", DEFAULT_BUZZER_PWM_DUTY)
    )


def save_buzzer_pwm_duty(duty: int) -> int:
    normalized = normalize_buzzer_pwm_duty(duty)
    data = load_settings()
    data["buzzer_pwm_duty"] = normalized
    data["updated_at"] = now_iso()
    save_settings(data)
    return normalized


def adjacent_buzzer_pwm_duty(direction: str) -> int:
    current = current_buzzer_pwm_duty()
    if direction == "louder":
        for duty in BUZZER_PWM_DUTY_STEPS:
            if duty > current:
                return duty
        return BUZZER_PWM_DUTY_STEPS[-1]

    for duty in reversed(BUZZER_PWM_DUTY_STEPS):
        if duty < current:
            return duty
    return BUZZER_PWM_DUTY_STEPS[0]


@contextmanager
def thread_state_lock() -> Iterator[None]:
    """Serialize hook/sync writers so concurrent AI events cannot corrupt JSON."""
    THREAD_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fp = THREAD_LOCK_FILE.open("a+b")
    try:
        lock_fp.seek(0)
        if lock_fp.read(1) == b"":
            lock_fp.write(b"\0")
            lock_fp.flush()
        lock_fp.seek(0)

        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(lock_fp.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_fp.seek(0)
                msvcrt.locking(lock_fp.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
    finally:
        lock_fp.close()


def checksum(payload: list[int]) -> int:
    value = 0
    for byte in payload[:7]:
        value ^= byte
    return value


def build_payload(command: Command) -> list[int]:
    if command.state not in STATES:
        choices = ", ".join(sorted(STATES))
        raise SystemExit(f"Unknown state '{command.state}'. Choices: {choices}")

    payload = [
        MAGIC,
        PROTOCOL_VERSION,
        STATES[command.state],
        command.brightness & 0xFF,
        command.rate & 0xFF,
        command.flags & 0xFF,
        command.seq & 0xFF,
        0,
    ]
    payload[7] = checksum(payload)
    return payload


def matching_devices(hid_module):
    devices = []
    for item in hid_module.enumerate():
        vendor_id = item.get("vendor_id")
        product_id = item.get("product_id")
        if (vendor_id, product_id) in {
            (VID, PID),
            (BOOTLOADER_VID, BOOTLOADER_PID),
        }:
            devices.append(item)
    return devices


def print_devices(hid_module) -> int:
    devices = matching_devices(hid_module)
    if not devices:
        print("No VibeCodingLed/Digispark HID device found.")
        print("Firmware HID target: 16C0:05DF")
        print("Micronucleus bootloader: 16D0:0753")
        return 1

    for item in devices:
        vid = item.get("vendor_id", 0)
        pid = item.get("product_id", 0)
        product = item.get("product_string") or ""
        manufacturer = item.get("manufacturer_string") or ""
        path = item.get("path")
        print(f"{vid:04X}:{pid:04X} {manufacturer} {product} path={path!r}")
    return 0


def open_device(hid_module):
    device = hid_module.device()
    try:
        device.open(VID, PID)
        return device
    except OSError as exc:
        raise SystemExit(
            "Could not open VibeCodingLed HID device 16C0:05DF. "
            "Check that the firmware has been uploaded and the board has "
            "re-enumerated out of the Micronucleus bootloader."
        ) from exc


def read_status(device) -> str:
    try:
        report = list(device.get_feature_report(REPORT_ID, 9))
    except OSError:
        return "status: unavailable"

    if not report:
        return "status: empty"

    payload = report[1:] if report[0] == REPORT_ID else report[:8]
    if len(payload) < 8 or payload[0] != MAGIC:
        return f"status: unexpected report {report!r}"

    state = STATE_LABELS.get(payload[2], f"mode-{payload[2]}")
    error = ERRORS.get(payload[3], f"error-{payload[3]}")
    return (
        f"status: state={state} error={error} "
        f"brightness={payload[4]} rate={payload[5]} seq={payload[6]}"
    )


def set_state(command: Command) -> int:
    hid_module = import_hid()
    effective_command = command
    if effective_command.flags == 0:
        effective_command = Command(
            state=command.state,
            brightness=command.brightness,
            rate=command.rate,
            flags=current_buzzer_pwm_duty(),
            seq=command.seq,
        )
    payload = build_payload(effective_command)

    device = open_device(hid_module)
    try:
        written = device.send_feature_report([REPORT_ID] + payload)
        print(
            f"sent: state={effective_command.state} "
            f"brightness={effective_command.brightness} "
            f"rate={effective_command.rate} flags={effective_command.flags} "
            f"seq={effective_command.seq} bytes={written}"
        )
        print(read_status(device))
    finally:
        device.close()
    return 0


def show_status() -> int:
    hid_module = import_hid()
    device = open_device(hid_module)
    try:
        print(read_status(device))
    finally:
        device.close()
    return 0


def demo(delay: float) -> int:
    sequence = ["off", "running", "partial", "done", "off"]
    for index, state in enumerate(sequence, start=1):
        set_state(Command(state=state, seq=index))
        time.sleep(delay)
    return 0


def load_thread_state() -> dict[str, Any]:
    if not THREAD_STATE_FILE.exists():
        return {"threads": {}}
    try:
        data = json.loads(THREAD_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"threads": {}}
    if not isinstance(data, dict) or not isinstance(data.get("threads"), dict):
        return {"threads": {}}
    return data


def save_thread_state(data: dict[str, Any]) -> None:
    normalize_thread_state(data)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp_path = THREAD_STATE_FILE.with_name(
        f"{THREAD_STATE_FILE.name}.{os.getpid()}.{int(time.time() * 1000)}.tmp"
    )
    tmp_path.write_text(text, encoding="utf-8")

    last_error: OSError | None = None
    for _ in range(10):
        try:
            tmp_path.replace(THREAD_STATE_FILE)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.05)

    try:
        THREAD_STATE_FILE.write_text(text, encoding="utf-8")
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            if last_error is not None:
                print(f"warning: stale temp thread file left at {tmp_path}", file=sys.stderr)


def normalize_thread_state(data: dict[str, Any]) -> None:
    threads = data.setdefault("threads", {})
    if not isinstance(threads, dict):
        data["threads"] = {}
        return
    for thread_id, item in list(threads.items()):
        if not isinstance(item, dict) or item.get("status") not in {
            "running",
            "stopped",
        }:
            threads.pop(thread_id, None)


def aggregate_threads(data: dict[str, Any]) -> str:
    threads = data.get("threads", {})
    if not threads:
        return "off"
    has_running = any(
        isinstance(item, dict) and item.get("status") == "running"
        for item in threads.values()
    )
    has_stopped = any(
        isinstance(item, dict) and item.get("status") == "stopped"
        for item in threads.values()
    )
    if has_running and has_stopped:
        return "partial"
    if has_running:
        return "running"
    if has_stopped:
        return "done"
    return "off"


def print_thread_summary(data: dict[str, Any], aggregate: str) -> None:
    threads = data.get("threads", {})
    print(f"aggregate={aggregate} thread_count={len(threads)}")
    for thread_id in sorted(threads):
        item = threads[thread_id]
        reason = item.get("reason", "")
        reason_suffix = f" reason={reason}" if reason else ""
        source = item.get("source", "")
        source_suffix = f" source={source}" if source else ""
        print(
            f"- {thread_id}: {item.get('status', 'unknown')}"
            f"{reason_suffix}{source_suffix} updated_at={item.get('updated_at', '')}"
        )


def apply_thread_state(data: dict[str, Any]) -> int:
    normalize_thread_state(data)
    aggregate = aggregate_threads(data)
    print_thread_summary(data, aggregate)
    return set_state(Command(state=aggregate, seq=int(time.time()) & 0xFF))


def start_thread(thread_id: str, source: str) -> int:
    with thread_state_lock():
        data = load_thread_state()
        data["threads"][thread_id] = {
            "status": "running",
            "source": source,
            "updated_at": now_iso(),
        }
        save_thread_state(data)
        return apply_thread_state(data)


def stop_thread(thread_id: str, reason: str, source: str) -> int:
    with thread_state_lock():
        data = load_thread_state()
        data["threads"][thread_id] = {
            "status": "stopped",
            "reason": reason,
            "source": source,
            "updated_at": now_iso(),
        }
        save_thread_state(data)
        return apply_thread_state(data)


def clear_thread(thread_id: str) -> int:
    with thread_state_lock():
        data = load_thread_state()
        data.get("threads", {}).pop(thread_id, None)
        save_thread_state(data)
        return apply_thread_state(data)


def acknowledge_stopped_threads(source: str | None = None) -> int:
    with thread_state_lock():
        data = load_thread_state()
        threads = data.get("threads", {})
        if not isinstance(threads, dict):
            data["threads"] = {}
        else:
            acknowledged = []
            for thread_id, item in list(threads.items()):
                if not isinstance(item, dict):
                    continue
                if item.get("status") != "stopped":
                    continue
                if source and item.get("source") != source:
                    continue
                acknowledged.append(thread_id)
                threads.pop(thread_id, None)
            data["last_ack"] = {
                "source": source or "all",
                "acknowledged_count": len(acknowledged),
                "thread_ids": acknowledged,
                "updated_at": now_iso(),
            }
        save_thread_state(data)
        return apply_thread_state(data)


def reset_threads() -> int:
    with thread_state_lock():
        data = {"threads": {}}
        save_thread_state(data)
        return apply_thread_state(data)


def sync_threads(active_thread_ids: list[str], source: str, stopped_reason: str) -> int:
    active = {item.strip() for item in active_thread_ids if item.strip()}
    synced_at = now_iso()

    with thread_state_lock():
        data = load_thread_state()
        threads = data.setdefault("threads", {})

        for thread_id in active:
            threads[thread_id] = {
                "status": "running",
                "source": source,
                "updated_at": synced_at,
            }

        for thread_id, item in list(threads.items()):
            item_source = item.get("source")
            if (
                item.get("status") == "running"
                and item_source == source
                and thread_id not in active
            ):
                threads[thread_id] = {
                    "status": "stopped",
                    "reason": stopped_reason,
                    "source": source,
                    "updated_at": synced_at,
                }

        data["last_sync"] = {
            "source": source,
            "active_count": len(active),
            "updated_at": synced_at,
        }
        save_thread_state(data)
        return apply_thread_state(data)


def list_threads() -> int:
    with thread_state_lock():
        data = load_thread_state()
        print_thread_summary(data, aggregate_threads(data))
    return 0


def show_buzzer_volume() -> int:
    duty = current_buzzer_pwm_duty()
    percent = duty * 100.0 / 255.0
    print(f"buzzer_pwm_duty={duty} ({percent:.1f}%)")
    return 0


def set_buzzer_volume(duty: int) -> int:
    saved = save_buzzer_pwm_duty(duty)
    percent = saved * 100.0 / 255.0
    print(f"buzzer_pwm_duty={saved} ({percent:.1f}%)")
    with thread_state_lock():
        data = load_thread_state()
        return apply_thread_state(data)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Control the VibeCodingLed USB status light."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List matching USB HID devices.")
    subparsers.add_parser("status", help="Read current LED firmware status.")

    set_parser = subparsers.add_parser("set", help="Set one direct LED state.")
    set_parser.add_argument("state", choices=sorted(STATES))
    set_parser.add_argument("--brightness", type=int, default=255)
    set_parser.add_argument(
        "--rate",
        type=int,
        default=0,
        help="Optional blink half-cycle rate override. 0 uses firmware defaults.",
    )
    set_parser.add_argument("--flags", type=int, default=0)
    set_parser.add_argument(
        "--seq",
        type=int,
        default=int(time.time()) & 0xFF,
        help="Sequence byte used for traceability.",
    )

    demo_parser = subparsers.add_parser("demo", help="Run all LED states.")
    demo_parser.add_argument("--delay", type=float, default=2.0)

    buzzer_parser = subparsers.add_parser(
        "buzzer",
        help="Show or change the persisted buzzer PWM volume.",
    )
    buzzer_subparsers = buzzer_parser.add_subparsers(
        dest="buzzer_command", required=True
    )
    buzzer_subparsers.add_parser("get", help="Show current buzzer PWM duty.")
    buzzer_set = buzzer_subparsers.add_parser(
        "set", help="Set buzzer PWM duty, 1-254. Default is 8."
    )
    buzzer_set.add_argument("duty", type=int)
    buzzer_subparsers.add_parser("louder", help="Make the buzzer one step louder.")
    buzzer_subparsers.add_parser("quieter", help="Make the buzzer one step quieter.")
    buzzer_subparsers.add_parser("default", help="Reset buzzer PWM duty to 8.")

    thread_parser = subparsers.add_parser(
        "thread",
        help="Track multiple AI programming threads and update the LED aggregate.",
    )
    thread_subparsers = thread_parser.add_subparsers(
        dest="thread_command", required=True
    )

    thread_start = thread_subparsers.add_parser("start", help="Mark a thread running.")
    thread_start.add_argument("thread_id")
    thread_start.add_argument("--source", default="manual")

    thread_stop = thread_subparsers.add_parser(
        "stop",
        help=(
            "Mark a thread stopped. The LED is solid only while another "
            "thread is still running, and solid when all threads have stopped."
        ),
    )
    thread_stop.add_argument("thread_id")
    thread_stop.add_argument(
        "--reason",
        choices=["completed", "problem", "stopped"],
        default="stopped",
    )
    thread_stop.add_argument("--source", default="manual")

    thread_clear = thread_subparsers.add_parser(
        "clear", help="Remove one acknowledged thread from the aggregate."
    )
    thread_clear.add_argument("thread_id")

    thread_ack = thread_subparsers.add_parser(
        "ack",
        aliases=["acknowledge"],
        help=(
            "Acknowledge all stopped threads and keep monitoring running "
            "threads. Use this when the customer has seen the completed or "
            "failed task and wants the LED to return to the current active "
            "state."
        ),
    )
    thread_ack.add_argument(
        "--source",
        default=None,
        help="Optional monitor source filter, such as codex or cursor.",
    )

    thread_sync = thread_subparsers.add_parser(
        "sync",
        help=(
            "Sync an active thread snapshot from one monitor source. "
            "Previously running threads from that source but absent in this "
            "snapshot are marked stopped; the aggregate becomes done when "
            "nothing is running until the customer acknowledges it."
        ),
    )
    thread_sync.add_argument("thread_id", nargs="*")
    thread_sync.add_argument("--source", default="codex")
    thread_sync.add_argument(
        "--stopped-reason",
        choices=["completed", "problem", "stopped"],
        default="stopped",
    )

    thread_subparsers.add_parser("reset", help="Clear all threads and turn LED off.")
    thread_subparsers.add_parser("list", help="Show tracked threads without sending.")

    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.command == "list":
        return print_devices(import_hid())
    if args.command == "status":
        return show_status()
    if args.command == "set":
        return set_state(
            Command(
                state=args.state,
                brightness=args.brightness,
                rate=args.rate,
                flags=args.flags,
                seq=args.seq,
            )
        )
    if args.command == "demo":
        return demo(args.delay)
    if args.command == "buzzer":
        if args.buzzer_command == "get":
            return show_buzzer_volume()
        if args.buzzer_command == "set":
            return set_buzzer_volume(args.duty)
        if args.buzzer_command == "louder":
            return set_buzzer_volume(adjacent_buzzer_pwm_duty("louder"))
        if args.buzzer_command == "quieter":
            return set_buzzer_volume(adjacent_buzzer_pwm_duty("quieter"))
        if args.buzzer_command == "default":
            return set_buzzer_volume(DEFAULT_BUZZER_PWM_DUTY)
    if args.command == "thread":
        if args.thread_command == "start":
            return start_thread(args.thread_id, args.source)
        if args.thread_command == "stop":
            return stop_thread(args.thread_id, args.reason, args.source)
        if args.thread_command == "clear":
            return clear_thread(args.thread_id)
        if args.thread_command in {"ack", "acknowledge"}:
            return acknowledge_stopped_threads(args.source)
        if args.thread_command == "sync":
            return sync_threads(args.thread_id, args.source, args.stopped_reason)
        if args.thread_command == "reset":
            return reset_threads()
        if args.thread_command == "list":
            return list_threads()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
