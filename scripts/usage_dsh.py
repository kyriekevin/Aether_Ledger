"""Read DSH JSONL and compressed logs into daily usage observations."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Iterator

import usage_schema
import usage_sources
import usage_telemetry
from pricing import active_rate, load_pricing, standard_cost


DSH_SESSION_LOG_NAMES = ("session.jsonl", "session.jsonl.zstd")

DSH_DECODE_TIMEOUT_SECONDS = 30

_DSH_EFFORT_ALIASES = {"off": "none"}


def _whole_lines(text: str) -> str:
    """Drop a trailing line the decoder stopped in the middle of.

    A frame that did not decode to its end leaves the bytes it did produce, which
    normally stop partway through a record. Both decoders are trimmed to the last
    newline so that a partial read is always whole records, and so that the two
    of them return the same prefix for the same artifact. The dropped fragment
    would not have parsed as JSON anyway; this only keeps the contract statable.
    """
    cut = text.rfind("\n")
    return text[: cut + 1] if cut >= 0 else ""


def _zstd_frames_text(raw: bytes) -> tuple[str | None, bool]:
    """Decode a dsh session artifact, with whether it decoded to the end.

    Returns (text, complete). Text of None means nothing could be read at all —
    no decoder, or bytes that are not zstd — which the caller must not confuse
    with a session that legitimately recorded nothing.

    dsh appends one independently decodable frame per durable batch, so a log
    read while a session is live ends mid-frame and `complete` is False. That is
    the ordinary case, not an error: whatever decoded is real, the rest arrives
    next run, and re-reading the whole artifact every run means an early partial
    read can never inflate a day. Frames are append-only, and each run recomputes
    the day from scratch, so the completed read always supersedes the torn one.

    A damaged frame also reports False, and deliberately is not distinguished
    from a torn tail. Telling them apart is not reliably possible: zstd reports
    single-byte corruption as seven different messages, one of which is the same
    "premature end" a live tail produces, and frame boundaries cannot be found by
    scanning for the frame magic because those bytes also occur inside compressed
    payloads. So the count is reported without a claimed cause; a count that stays
    positive across runs with no live session is the tell.

    Decoding stops at the first frame that does not decode, and does not resume
    past it. For the ordinary cause — a live session's unfinished last frame —
    nothing follows it to lose. A frame damaged mid-file does cost the batches
    after it until the artifact is repaired or rotated; the per-day high-water
    merge keeps the days already recorded from dropping in the meantime. This is
    the deliberate price of not guessing at frame boundaries.

    Nothing is added to this script's (empty) dependency list for any of it.
    """
    if not raw:
        # dsh creates the artifact before it writes the first frame, so an empty
        # file is a session that has recorded nothing yet — not a broken one.
        return "", True
    try:
        from compression.zstd import ZstdDecompressor  # Python 3.14+
    except ImportError:
        pass
    else:
        decoded = bytearray()
        remaining = raw
        complete = True
        while remaining:
            decompressor = ZstdDecompressor()
            try:
                decoded += decompressor.decompress(remaining)
            except Exception:
                complete = False
                break
            if not decompressor.eof:
                complete = False
                break
            remaining = decompressor.unused_data
        if not decoded and not complete:
            return None, False
        text = decoded.decode("utf-8", "replace")
        return (text if complete else _whole_lines(text)), complete

    zstd = shutil.which("zstd")
    if zstd is None:
        return None, False
    # A partial or damaged frame makes `zstd -dc` exit non-zero *after* writing
    # every frame it did decode, so stdout is read regardless of the status. Only
    # an empty stdout means the artifact could not be read at all.
    try:
        out = subprocess.run(
            [zstd, "-dc", "-"], input=raw, capture_output=True,
            timeout=DSH_DECODE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, False
    if out.returncode == 0:
        return out.stdout.decode("utf-8", "replace"), True
    if not out.stdout:
        return None, False
    return _whole_lines(out.stdout.decode("utf-8", "replace")), False


def _read_dsh_session_log(path: Path) -> tuple[str | None, bool]:
    """One session log as text, plus whether it decoded to the end.

    Text of None means the log could not be read at all.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None, False
    if path.name.endswith(".zstd"):
        return _zstd_frames_text(raw)
    return raw.decode("utf-8", "replace"), True


def _event_day_from_millis(raw: object) -> date | None:
    """Convert one dsh event timestamp (epoch milliseconds) to the ledger day."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(raw / 1000, tz=usage_schema.SHANGHAI).date()
    except (OSError, OverflowError, ValueError):
        return None


def _normalise_dsh_effort(raw: object) -> str | None:
    """One dsh reasoning level as this repository spells it, or None."""
    if not isinstance(raw, str):
        return None
    level = raw.strip().lower()
    level = _DSH_EFFORT_ALIASES.get(level, level)
    return level if level in usage_schema.EFFORT_LEVELS else None


def collect_dsh_daily_since(
    since: date, roots: Iterable[Path] | None = None
) -> list[dict]:
    """Aggregate DeepSeek Harness usage per day straight from its session logs.

    ccusage has no dsh reader, so this is a first-party parse. `session` supplies
    the anonymous in-memory identity used to deduplicate copied logs;
    `request/header` and `request/context` name the model serving the calls that
    follow; and `assistant/message` carries one model call's token accounting.

    dsh's counts are disjoint by contract: `inputTokens` is uncached input only,
    and cached input arrives as cacheRead/cacheWrite, so the four components add
    up to the billed total. That is the same convention the ccusage-fed paths
    use, which is what lets one merged store hold both.

    Cost comes from config/official-pricing.json at the standard tier; dsh records
    no priority tier to apply a multiplier to. Models with no official row (the
    gateway routes dsh can reach) keep their real token counts and contribute zero,
    and are named on stderr so a new one is noticed rather than billing zero unseen.

    Session ids are used only for in-memory deduplication. The log's cwd, prompts,
    tool arguments and output, titles, and every identity are absent from output.
    """
    session_roots = usage_sources.dsh_session_roots() if roots is None else tuple(roots)
    daily: dict[str, dict] = {}
    messages: dict[tuple[object, int, int], tuple] = {}
    unreadable = 0
    partial = 0
    for path in _dsh_session_logs(session_roots):
        text, complete = _read_dsh_session_log(path)
        if text is None:
            unreadable += 1
            continue
        if not complete:
            partial += 1
        session_id: str | None = None
        model: str | None = None
        effort: str | None = None
        for line_number, line in enumerate(text.splitlines()):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # A live log's last record may still be mid-write, and packed
                # chunk rows are storage encodings this reader has no use for.
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            data = event.get("data")
            if kind == "session":
                raw_session_id = event.get("id")
                if isinstance(raw_session_id, str) and raw_session_id.strip():
                    session_id = raw_session_id.strip()
                continue
            if not isinstance(data, dict):
                continue
            if kind == "request/header":
                header = data.get("header")
                config = header.get("config") if isinstance(header, dict) else None
                if isinstance(config, dict):
                    model = _dsh_model(config.get("model")) or model
                    effort = _normalise_dsh_effort(config.get("reasoningEffort"))
                continue
            if kind == "request/context":
                model = _dsh_model(data.get("model")) or model
                continue
            if kind != "assistant/message":
                continue
            usage = data.get("usage")
            # `usage` is absent when the adapter reported no accounting, and a
            # message with no header before it has no model to attribute.
            if not isinstance(usage, dict) or model is None:
                continue
            day = _event_day_from_millis(event.get("time"))
            if day is None or day < since:
                continue
            breakdown = {
                "inputTokens": usage_schema._token_value(usage.get("inputTokens")),
                "outputTokens": usage_schema._token_value(usage.get("outputTokens")),
                "cacheCreationTokens": usage_schema._token_value(usage.get("cacheWriteTokens")),
                "cacheReadTokens": usage_schema._token_value(usage.get("cacheReadTokens")),
            }
            tokens = sum(breakdown.values())
            if tokens <= 0:
                continue
            # A copied or migrated session can appear in more than one file.
            # DSH's stable call identity is session + turn + step; later records
            # win so an older copy cannot replace a finalized observation.
            turn = data.get("turn")
            step = data.get("step")
            valid_step = all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in (turn, step)
            )
            identity = (
                (session_id or path, turn, step)
                if valid_step
                else (path, -1, line_number)
            )
            raw_time = event.get("time")
            raw_seq = event.get("seq")
            priority = (
                raw_time if isinstance(raw_time, (int, float)) else 0,
                raw_seq if isinstance(raw_seq, (int, float)) else 0,
                tokens,
            )
            raw_reasoning = usage.get("reasoningTokens")
            candidate = (
                priority, day, model, effort, breakdown,
                usage_schema._token_value(raw_reasoning),
                isinstance(raw_reasoning, (int, float))
                and not isinstance(raw_reasoning, bool),
            )
            if identity not in messages or priority >= messages[identity][0]:
                messages[identity] = candidate

    for (
        _priority, day, model, effort, breakdown, reasoning,
        reasoning_observed,
    ) in messages.values():
        tokens = sum(breakdown.values())
        bucket = daily.setdefault(day.isoformat(), {}).setdefault(
            "models", {}
        ).setdefault(model, {"totalTokens": 0, **{k: 0 for k in breakdown}})
        bucket["totalTokens"] += tokens
        for key, value in breakdown.items():
            bucket[key] += value
        if effort is not None:
            usage_telemetry._add_routing_bucket(
                daily, day, "efforts", effort, tokens,
                reasoning_tokens=reasoning,
                reasoning_observed=reasoning_observed,
            )
    if unreadable:
        print(
            f"dsh: {unreadable} session log(s) could not be read at all and were "
            f"skipped; install `zstd` (or run on Python 3.14+) so compressed "
            f"session logs can be read",
            file=sys.stderr,
        )
    if partial:
        # Usually one live session whose last frame is still being written, which
        # the next run picks up. A count that stays positive with no session
        # running means real damage, and those frames are not coming back.
        print(
            f"dsh: {partial} session log(s) did not decode to the end; a live "
            f"session's unfinished last frame is the usual cause",
            file=sys.stderr,
        )
    return _dsh_entries(daily)


def _dsh_session_logs(roots: Iterable[Path]) -> Iterator[Path]:
    """Every session log under the given trees.

    One session directory may hold both names once its harness home's compression
    setting has changed: dsh derives the artifact name from that setting, so a log
    written under the other one is a file it no longer opens, and the events in it
    are as real as the ones beside it. Reading both adds two halves of a session's
    history rather than counting anything twice.
    """
    for root in roots:
        if not root.is_dir():
            continue
        for name in DSH_SESSION_LOG_NAMES:
            yield from sorted(root.rglob(name))


def _dsh_model(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    return usage_schema._normalise_model(raw.strip())


def _dsh_entries(daily: dict[str, dict]) -> list[dict]:
    """Price each collected dsh day and shape it like every other daily entry."""
    pricing = load_pricing()
    unpriced_days: dict[str, int] = {}
    entries: list[dict] = []
    for day, payload in sorted(daily.items()):
        models = payload.get("models", {})
        if not models:
            continue
        usage_day = date.fromisoformat(day)
        cost = 0.0
        fully_priced = True
        for name, breakdown in models.items():
            if active_rate(name, usage_day, pricing) is None:
                fully_priced = False
                unpriced_days[name] = unpriced_days.get(name, 0) + 1
            cost += standard_cost(name, usage_day, breakdown, pricing)
        entry = {
            "date": day,
            "totalTokens": sum(m["totalTokens"] for m in models.values()),
            "totalCost": cost,
            "models": models,
            "costTrusted": True,
            "costSource": "official" if fully_priced else "unpriced",
        }
        if payload.get("routing"):
            entry["routing"] = payload["routing"]
        entries.append(entry)
    if unpriced_days:
        print(
            "dsh left unpriced (no official price entry, counted at zero): "
            + ", ".join(
                f"{name} ({days}d)" for name, days in sorted(unpriced_days.items())
            ),
            file=sys.stderr,
        )
    return entries
