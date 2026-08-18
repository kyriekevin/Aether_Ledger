#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Install or remove the local Claude status-line quota tap."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import tempfile
from pathlib import Path

SOURCE_PROXY = Path(__file__).with_name("claude_statusline_proxy.py")
DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"
DEFAULT_INSTALL_DIR = Path.home() / ".local" / "share" / "aether-ledger"
DEFAULT_CONFIG = Path.home() / ".config" / "aether-ledger" / "claude-statusline.json"


def _read_object(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"missing JSON file: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read JSON file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"expected a JSON object in {path}")
    return payload


def _atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _settings_command(settings: dict, path: Path) -> str:
    status_line = settings.get("statusLine")
    if not isinstance(status_line, dict) or status_line.get("type") != "command":
        raise RuntimeError(f"{path} has no command statusLine to preserve")
    command = status_line.get("command")
    if not isinstance(command, str) or not command.strip():
        raise RuntimeError(f"{path} has an empty statusLine command")
    return command


def install(
    settings_path: Path,
    install_dir: Path,
    config_path: Path,
    python: str,
    *,
    dry_run: bool = False,
) -> bool:
    settings = _read_object(settings_path)
    current = _settings_command(settings, settings_path)
    target = install_dir / SOURCE_PROXY.name
    installed = shlex.join([python, str(target), "--config", str(config_path)])

    existing_config = {}
    if config_path.exists():
        existing_config = _read_object(config_path)
    prior_installed = existing_config.get("installedCommand")
    prior_original = existing_config.get("originalCommand")
    if current == installed:
        original = existing_config.get("originalCommand")
        if not isinstance(original, str) or not original.strip():
            raise RuntimeError("statusLine points to the quota tap but its recovery config is missing")
        if dry_run:
            return False
    elif current == prior_installed:
        if not isinstance(prior_original, str) or not prior_original.strip():
            raise RuntimeError("installed quota tap has no recoverable original command")
        original = prior_original
        if dry_run:
            return True
    else:
        if str(target) in current:
            raise RuntimeError("statusLine already mentions the quota tap in an unknown form")
        original = current
        if dry_run:
            return True

    proxy = SOURCE_PROXY.read_text(encoding="utf-8")
    _atomic_write(target, proxy, mode=0o755)
    recovery = {
        "version": 1,
        "originalCommand": original,
        "installedCommand": installed,
    }
    _atomic_write(
        config_path,
        json.dumps(recovery, indent=2, ensure_ascii=False) + "\n",
    )
    settings["statusLine"]["command"] = installed
    _atomic_write(
        settings_path,
        json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
    )
    return current != installed


def uninstall(settings_path: Path, config_path: Path, *, dry_run: bool = False) -> bool:
    settings = _read_object(settings_path)
    current = _settings_command(settings, settings_path)
    recovery = _read_object(config_path)
    installed = recovery.get("installedCommand")
    original = recovery.get("originalCommand")
    if not isinstance(installed, str) or not isinstance(original, str):
        raise RuntimeError(f"invalid recovery config: {config_path}")
    if current == original:
        return False
    if current != installed:
        raise RuntimeError("statusLine changed after installation; refusing to overwrite it")
    if dry_run:
        return True
    settings["statusLine"]["command"] = original
    _atomic_write(
        settings_path,
        json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    parser.add_argument("--install-dir", type=Path, default=DEFAULT_INSTALL_DIR)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--python", default=shutil.which("python3") or "python3")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    try:
        if args.uninstall:
            changed = uninstall(args.settings, args.config, dry_run=args.dry_run)
            action = "would restore" if args.dry_run and changed else "restored"
        else:
            changed = install(
                args.settings,
                args.install_dir,
                args.config,
                args.python,
                dry_run=args.dry_run,
            )
            action = "would install" if args.dry_run and changed else "installed"
    except RuntimeError as exc:
        parser.error(str(exc))
    if not changed:
        action = "already current"
    print(f"Claude status-line quota tap: {action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
