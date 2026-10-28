"""Run a ccusage build verified for OpenAI's per-request 272K pricing boundary.

ccusage takes each model's long-context boundary from its embedded models.dev
snapshot, and pricing overrides cannot set it. A model the snapshot lacks falls
back to 200K, so every model in ``longContextModels`` is probed on both sides of
272K before the build is trusted. A source build of the pinned upstream
revision is installed separately from the system package. No session files or
third-party binaries enter this repository.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

REVISION = "7a063a09103184391dede2d6285b32a75903d328"
VERSION = "20.0.35+" + REVISION[:7]
SOURCE = "https://github.com/ccusage/ccusage.git"
BINARY = Path.home() / ".cache" / "aether-ledger" / "ccusage" / REVISION / "ccusage"
PRICING_PATH = Path(__file__).resolve().parents[1] / "config" / "official-pricing.json"


def long_context_models() -> list[str]:
    table = json.loads(PRICING_PATH.read_text(encoding="utf-8"))
    return list(table["ccusage"]["longContextModels"])


def verify(binary: str) -> None:
    """Prove the 272K boundary and Fast pricing for every long-context model."""
    models = long_context_models()
    if not models:
        raise ValueError("no long-context models to verify")
    for model in models:
        _verify_model(binary, model)


def _verify_model(binary: str, model: str) -> None:
    """Exercise both sides of 272K and Fast using synthetic, isolated logs."""
    with tempfile.TemporaryDirectory(prefix="aether-pricing-probe-") as raw:
        root = Path(raw)
        sessions = root / "sessions"
        sessions.mkdir()
        expected = {}
        for index, (input_tokens, speed) in enumerate(
            [(200001, "standard"), (272000, "standard"),
             (272001, "standard"), (272001, "fast")], 1
        ):
            day = f"2026-09-{index:02d}"
            usage = {"input_tokens": input_tokens, "cached_input_tokens": 100000,
                     "output_tokens": 1000, "reasoning_output_tokens": 0,
                     "total_tokens": input_tokens + 1000}
            events = [
                {"timestamp": day + "T12:00:00.000Z", "type": "turn_context",
                 "payload": {"model": model}},
                {"timestamp": day + "T12:00:01.000Z", "type": "event_msg",
                 "payload": {"type": "thread_settings_applied", "thread_settings": {
                     "service_tier": "priority" if speed == "fast" else "default"}}},
                {"timestamp": day + "T12:01:00.000Z", "type": "event_msg",
                 "payload": {"type": "token_count", "info": {
                     "model": model, "last_token_usage": usage,
                     "total_token_usage": usage}}},
            ]
            (sessions / f"rollout-{index}.jsonl").write_text(
                "\n".join(json.dumps(e, separators=(",", ":")) for e in events) + "\n"
            )
            long = input_tokens > 272000
            cost = ((input_tokens - 100000) * (20 if long else 10)
                    + 100000 * (2 if long else 1) + 1000 * (75 if long else 50)) / 1e6
            expected[day] = cost * (2 if speed == "fast" else 1)
        config = root / "pricing.json"
        config.write_text(json.dumps({"defaults": {"pricingOverrides": {
            model: {
                "inputCostPerToken": 10e-6, "outputCostPerToken": 50e-6,
                "cacheReadInputTokenCost": 1e-6, "cacheCreationInputTokenCost": 12.5e-6,
                "inputCostPerTokenAbove200kTokens": 20e-6,
                "outputCostPerTokenAbove200kTokens": 75e-6,
                "cacheReadInputTokenCostAbove200kTokens": 2e-6,
                "cacheCreationInputTokenCostAbove200kTokens": 25e-6,
                "fastMultiplier": 2,
            }}}}))
        # An empty home config prevents the user's default Fast setting from
        # affecting the standard cases. All probe data is synthetic.
        env = {**os.environ, "CODEX_HOME": str(root)}
        result = subprocess.run(
            [binary, "codex", "daily", "--json", "--offline", "--speed", "auto",
             "--timezone", "UTC", "--config", str(config)],
            env=env, capture_output=True, text=True, check=True, timeout=30,
        )
        actual = {row["date"]: row["costUSD"] for row in json.loads(result.stdout)["daily"]}
        if actual.keys() != expected.keys() or any(
            abs(actual[day] - cost) > 1e-6 for day, cost in expected.items()
        ):
            raise ValueError(f"ccusage failed {model} 272K/Fast pricing verification")


def install() -> None:
    if BINARY.exists():
        verify(str(BINARY))
        return
    if shutil.which("cargo") is None:
        raise RuntimeError("Rust is required: install cargo, then rerun with --install")
    with tempfile.TemporaryDirectory(prefix="aether-ccusage-build-") as raw:
        source = Path(raw) / "source"
        subprocess.run(["git", "init", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "fetch", "--depth", "1", SOURCE, REVISION], check=True)
        subprocess.run(["git", "-C", str(source), "checkout", "--detach", "FETCH_HEAD"], check=True)
        subprocess.run(
            ["cargo", "build", "--locked", "--release", "-p", "ccusage",
             "--features", "fetch-litellm-pricing"], cwd=source / "rust",
            env={**os.environ, "CCUSAGE_VERSION": VERSION}, check=True,
        )
        built = source / "rust" / "target" / "release" / "ccusage"
        verify(str(built))
        BINARY.parent.mkdir(parents=True, exist_ok=True)
        staged = BINARY.with_suffix(".tmp")
        shutil.copy2(built, staged)
        os.replace(staged, BINARY)


def main() -> int:
    if sys.argv[1:] == ["--install"]:
        install()
        print("Installed and verified ccusage " + VERSION)
        return 0
    binary = str(BINARY) if BINARY.exists() else "ccusage"
    try:
        verify(binary)
    except (ValueError, OSError, subprocess.SubprocessError, KeyError) as exc:
        print("272K-capable ccusage required; run uv run python "
              "scripts/ccusage_runtime.py --install. " + str(exc), file=sys.stderr)
        return 1
    if sys.argv[1:] == ["--check"]:
        print(f"ccusage {VERSION} 272K and Fast pricing verified for "
              + ", ".join(long_context_models()))
        return 0
    os.execvp(binary, [binary, *sys.argv[1:]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
