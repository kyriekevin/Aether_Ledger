#!/usr/bin/env python3
"""Capture Claude subscription pressure from status-line stdin, then run the HUD.

This process never contacts Anthropic. Claude Code already supplies `rate_limits`
to the configured status-line command; the proxy keeps only daily percentage high
waters in a local cache and forwards the original JSON unchanged.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
CACHE_PATH = Path.home() / ".cache" / "aether-ledger" / "claude-rate-limits.json"
CONFIG_PATH = Path.home() / ".config" / "aether-ledger" / "claude-statusline.json"
WINDOWS = {"five_hour": "300", "seven_day": "10080"}
RETENTION_DAYS = 90


def _percentage(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return min(100.0, max(0.0, float(value)))


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def capture_rate_limits(
    raw: str,
    cache_path: Path = CACHE_PATH,
    observed_at: Optional[datetime] = None,
) -> bool:
    """Persist privacy-safe daily high waters; return whether a sample existed."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return False

    windows: dict[str, float] = {}
    for source, minutes in WINDOWS.items():
        window = rate_limits.get(source)
        if not isinstance(window, dict):
            continue
        percent = _percentage(window.get("used_percentage"))
        if percent is not None:
            windows[minutes] = percent
    if not windows:
        return False

    timestamp = observed_at or datetime.now(SHANGHAI)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=SHANGHAI)
    timestamp = timestamp.astimezone(SHANGHAI)
    day = timestamp.date().isoformat()
    oldest = (timestamp.date() - timedelta(days=RETENTION_DAYS - 1)).isoformat()

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = cache_path.with_suffix(cache_path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cache = _read_json(cache_path)
        days = cache.get("days")
        if not isinstance(days, dict):
            days = {}
        days = {
            raw_day: entry
            for raw_day, entry in days.items()
            if isinstance(raw_day, str) and raw_day >= oldest and isinstance(entry, dict)
        }
        entry = days.setdefault(day, {"windows": {}})
        stored_windows = entry.get("windows")
        if not isinstance(stored_windows, dict):
            stored_windows = {}
        for minutes, percent in windows.items():
            previous = _percentage(stored_windows.get(minutes)) or 0.0
            stored_windows[minutes] = max(previous, percent)
        entry["windows"] = stored_windows
        entry["observedAt"] = timestamp.isoformat()
        _atomic_write_json(cache_path, {"version": 1, "days": days})
    return True


def _original_command(config_path: Path = CONFIG_PATH) -> Optional[str]:
    command = _read_json(config_path).get("originalCommand")
    return command if isinstance(command, str) and command.strip() else None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    args = parser.parse_args(argv)
    raw = sys.stdin.read()
    try:
        capture_rate_limits(raw)
    except Exception:
        # Usage capture must never break the user's existing status line.
        pass
    command = _original_command(args.config)
    if command is None:
        return 0
    completed = subprocess.run(
        ["/bin/bash", "-c", command],
        input=raw,
        text=True,
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
