"""Merge daily observations into privacy-safe cumulative stores."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

import usage_schema



def _canonical_model_totals(models: dict) -> dict[str, dict]:
    """Canonicalize one observation without discarding its token components."""
    token_keys = (
        "totalTokens", "inputTokens", "outputTokens",
        "cacheCreationTokens", "cacheReadTokens",
    )
    canonical: dict[str, dict] = {}
    for name, payload in models.items():
        model = usage_schema._normalise_model(name)
        if isinstance(payload, dict):
            values = {key: usage_schema._token_value(payload.get(key)) for key in token_keys}
        else:
            values = {"totalTokens": usage_schema._token_value(payload)}
        bucket = canonical.setdefault(model, {"totalTokens": 0})
        for key, value in values.items():
            include = value or (
                isinstance(payload, dict) and key in payload
            ) or key == "totalTokens"
            if include:
                bucket[key] = bucket.get(key, 0) + value
    return canonical


def _merge_routing(prev: dict, current: dict, *, replace: bool) -> dict:
    if replace:
        return current
    merged: dict[str, dict] = {}
    for dimension, buckets in prev.items():
        if not isinstance(buckets, dict):
            continue
        destination = merged.setdefault(dimension, {})
        for label, values in buckets.items():
            if not isinstance(values, dict):
                continue
            normalized = {
                key: value for key, value in values.items() if key != "turns"
            }
            if "calls" in values or "turns" in values:
                normalized["calls"] = max(
                    usage_schema._token_value(values.get("calls")),
                    usage_schema._token_value(values.get("turns")),
                )
            destination[label] = normalized
    for dimension, buckets in current.items():
        if not isinstance(buckets, dict):
            continue
        destination = merged.setdefault(dimension, {})
        for label, values in buckets.items():
            if not isinstance(values, dict):
                continue
            previous = destination.setdefault(label, {})
            if "calls" in values or "turns" in values:
                previous["calls"] = max(
                    usage_schema._token_value(previous.get("calls")),
                    usage_schema._token_value(values.get("calls")),
                    usage_schema._token_value(values.get("turns")),
                )
            total_tokens = values.get("totalTokens")
            if isinstance(total_tokens, int) and not isinstance(total_tokens, bool):
                previous["totalTokens"] = max(
                    0, total_tokens, usage_schema._token_value(previous.get("totalTokens"))
                )
            reasoning_calls = values.get("reasoningCalls")
            if isinstance(reasoning_calls, int) and not isinstance(
                reasoning_calls, bool
            ):
                prior_calls = usage_schema._token_value(previous.get("reasoningCalls"))
                if reasoning_calls >= prior_calls:
                    previous["reasoningCalls"] = max(0, reasoning_calls)
                    previous["reasoningOutputTokens"] = usage_schema._token_value(
                        values.get("reasoningOutputTokens")
                    )
            elif "reasoningOutputTokens" in values and "reasoningCalls" not in previous:
                previous["reasoningOutputTokens"] = max(
                    usage_schema._token_value(previous.get("reasoningOutputTokens")),
                    usage_schema._token_value(values.get("reasoningOutputTokens")),
                )
    return merged


def _merge_quota(prev: dict, current: dict, *, replace: bool) -> dict:
    if replace:
        return current
    windows = dict(prev.get("windows", {}))
    for minutes, percent in current.get("windows", {}).items():
        if isinstance(percent, (int, float)) and not isinstance(percent, bool):
            windows[minutes] = max(float(percent), float(windows.get(minutes, 0.0)))
    return {
        "windows": windows,
        "limitReached": bool(
            prev.get("limitReached", False) or current.get("limitReached", False)
        ),
    }


def merge_with_cumulative(
    daily: list[dict], store_path: Path, *, reconcile_since: date | None = None
) -> list[dict]:
    """Upsert daily entries into a local store; return merged list sorted by date.

    Why: ccusage / @ccusage/codex both read session JSONLs that get rotated by
    their host CLIs (cleanupPeriodDays etc). Without persisting locally, the
    cumulative total silently shrinks each day.

    `reconcile_since` lifts that high-water rule for dates on or after it, so the
    fetch wins even when it counts fewer tokens. See main() for when to reach for
    it — never on the scheduled path.
    """
    store: dict[str, dict] = {}
    if store_path.exists():
        store = json.loads(store_path.read_text())
    for entry in daily:
        prev = store.get(entry["date"], {"totalTokens": 0, "totalCost": 0.0})
        # An entry that never observed tokens (the image-only stub built for a day
        # whose session ccusage has already rotated away) carries zeros that mean
        # "unknown", not "none". Reconciliation is the one path that would write
        # them over real stored usage, so it never applies to those entries.
        reconciling = (
            reconcile_since is not None
            and entry.get("tokensObserved", True)
            and date.fromisoformat(entry["date"]) >= reconcile_since
        )
        # Keep whichever observation saw the MOST tokens, and carry ITS cost.
        # max() on TOKENS alone is what guards against ccusage's window shrinking
        # when the host CLIs rotate session JSONLs (cleanupPeriodDays) — deleted
        # usage must not vanish from the cumulative total. Cost must FOLLOW tokens,
        # not be max()'d independently. `>=` lets legacy, unmarked observations
        # refresh; official-to-official same-token observations are frozen below.
        # Only a genuine token regression (rotation) freezes the old pair here.
        take_entry = reconciling or entry["totalTokens"] >= prev["totalTokens"]
        if take_entry:
            merged = {"totalTokens": entry["totalTokens"], "totalCost": entry["totalCost"]}
            cost_source = entry.get("costSource")
        else:
            merged = {"totalTokens": prev["totalTokens"], "totalCost": prev["totalCost"]}
            cost_source = prev.get("costSource")
        # Once the stored observation uses this repository's official table, an
        # unchanged token high-water mark is immutable. A later incomplete price
        # lookup must not downgrade it to `unpriced`; --reconcile-since remains
        # the explicit operator path for intentional historical corrections.
        if (
            not reconciling
            and entry["totalTokens"] == prev["totalTokens"]
            and prev.get("costSource") == "official"
        ):
            merged["totalCost"] = prev["totalCost"]
            cost_source = "official"
        # Legacy callers may still provide the old trust marker. Preserve their
        # last known positive cost when an incomplete price lookup returns zero or
        # a known partial sum. Repository-owned pricing marks known rates
        # `official` and intentionally records missing rates as `unpriced` zero.
        if not reconciling and prev["totalCost"] and entry.get("costSource") is None and (
            not entry.get("costTrusted", True) or not merged["totalCost"]
        ):
            merged["totalCost"] = prev["totalCost"]
            cost_source = prev.get("costSource")
        # Carry per-model token breakdown when present (claude/codex/opencode all
        # supply it now). Each model needs the same high-water protection as the
        # row total: session rotation can shrink one model even while another
        # grows enough for the fresh row total to win.
        # While reconciling the fetch is authoritative even when it breaks the day
        # down into nothing, so an emptied map must not leave the old one behind
        # describing totals that no longer exist.
        if reconciling and "models" in entry:
            merged["models"] = _canonical_model_totals(entry["models"])
        elif entry.get("models"):
            merged_models = _canonical_model_totals(prev.get("models", {}))
            current_models = _canonical_model_totals(entry["models"])
            for model, current in current_models.items():
                previous = merged_models.get(model)
                current_tokens = current["totalTokens"]
                previous_tokens = previous["totalTokens"] if previous else 0
                if current_tokens >= previous_tokens:
                    merged_model = dict(current)
                    if previous:
                        for key in (
                            "inputTokens",
                            "outputTokens",
                            "cacheCreationTokens",
                            "cacheReadTokens",
                        ):
                            if key not in merged_model and key in previous:
                                merged_model[key] = previous[key]
                    merged_models[model] = merged_model
            merged["models"] = merged_models
        elif "models" in prev:
            merged["models"] = _canonical_model_totals(prev["models"])
        if entry.get("routing") or prev.get("routing"):
            merged["routing"] = _merge_routing(
                prev.get("routing", {}), entry.get("routing", {}),
                replace=reconciling,
            )
        if entry.get("quota") or prev.get("quota"):
            merged["quota"] = _merge_quota(
                prev.get("quota", {}), entry.get("quota", {}),
                replace=reconciling,
            )
        if not reconciling and merged.get("models"):
            # Historical stores may lack some model detail, so the breakdown can
            # remain smaller than the row total. It must never exceed that total:
            # component high waters are a stronger lower bound when different
            # models rotate out and grow between observations.
            model_sum = sum(
                model["totalTokens"] for model in merged["models"].values()
            )
            if model_sum > merged["totalTokens"]:
                # Rotation combined model observations that no single priced
                # row covers. Keep the known cost, but never call it complete.
                cost_source = "unpriced"
            merged["totalTokens"] = max(merged["totalTokens"], model_sum)
        if cost_source:
            merged["costSource"] = cost_source
        # imageCount: max() like other monotonic fields, so user/system cleanup
        # of ~/.codex/generated_images after imageCount was recorded doesn't lose data.
        new_img = entry.get("imageCount", 0)
        prev_img = prev.get("imageCount", 0)
        if new_img or prev_img:
            merged["imageCount"] = max(new_img, prev_img)
        store[entry["date"]] = merged
    if reconcile_since is not None:
        # Reconciliation can only correct days the fetch still returns. A day it
        # dropped entirely is either usage the upgrade folded away or a session
        # that simply rotated out of ccusage's window, and nothing in the fetch
        # tells those apart — so keep the stored value, which is the safe reading,
        # and name the days so the operator can judge them.
        observed = {e["date"] for e in daily if e.get("tokensObserved", True)}
        stale = sorted(
            d for d, v in store.items()
            if date.fromisoformat(d) >= reconcile_since
            and d not in observed
            and v.get("totalTokens", 0)
        )
        if stale:
            print(
                f"{store_path.name}: {len(stale)} day(s) on or after {reconcile_since} "
                f"were not in this fetch and keep their stored value: "
                f"{', '.join(stale)}",
                file=sys.stderr,
            )
    _atomic_write_json(store_path, store)
    return [{"date": d, **v} for d, v in sorted(store.items())]


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON via temp file + os.replace so a crash mid-write can't leave
    invalid JSON that blocks future runs.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
