"""Shared public store names, model aliases, and token vocabulary."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

DATA_REPO_DIR = Path(__file__).resolve().parents[1]

EPOCH = date(2024, 1, 1)

AGENT_STORES = {
    "claude": "claude.json",
    "codex": "codex.json",
    "codex-multica": "codex-multica.json",
    "dsh-multica": "dsh-multica.json",
    "opencode": "opencode.json",
    "traex": "traex.json",
    "dsh": "dsh.json",
}

EFFORT_LEVELS = frozenset({"none", "low", "medium", "high", "xhigh", "max"})

SPEED_LEVELS = frozenset({"standard", "fast"})

_ALIAS_PREFIXES = (
    ("openrouter-3o", "claude-opus-4-8"),
    ("openrouter-2o", "claude-opus-4-7"),
    ("openrouter-1o", "claude-opus-4-6"),
)

_MODEL_ALIASES = {
    "gemini-3.1-pro": "gemini-3.1-pro-preview",
    "gemini-3-flash": "gemini-3-flash-preview",
}


def _normalise_model(name: str) -> str:
    """Lowercase a session model name and resolve known opaque aliases.

    Lowercasing alone lets ccusage's case-sensitive lookup price every real-name
    model (GPT-5.x, Gemini, DeepSeek). Aliases in _ALIAS_PREFIXES carry no vendor
    root for the substring match to catch, so they are additionally rewritten to
    the real slug they front before the (already lowercased) name is returned.
    """
    lowered = name.lower()
    for prefix, real in _ALIAS_PREFIXES:
        if lowered.startswith(prefix):
            return real
    return _MODEL_ALIASES.get(lowered, lowered)


def _token_value(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


SHANGHAI = ZoneInfo("Asia/Shanghai")
