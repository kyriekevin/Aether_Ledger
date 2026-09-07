"""Adapt ccusage reports and price local harness usage."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Iterator

import usage_schema
import usage_sources
from pricing import (
    active_rate, ccusage_config_file, load_pricing, official_cost_from_ccusage,
    standard_cost, token_breakdown,
)


CCUSAGE_RUNNER = [sys.executable, str(Path(__file__).with_name("ccusage_runtime.py"))]

CCUSAGE_CMD = [*CCUSAGE_RUNNER, "daily", "--json", "--by-agent"]

CCUSAGE_TIMEOUT_SECONDS = 180

CODEX_IMAGE_GEN_DIR = Path.home() / ".codex" / "generated_images"

TRAEX_CODEX_HOME = Path.home() / ".trae" / "cli"

CCUSAGE_CODEX_CMD = [*CCUSAGE_RUNNER, "codex", "daily", "--json"]

_SESSION_MODEL_RE = re.compile(r'("model"\s*:\s*")([^"]+)(")')

_KNOWN_UNPRICED_PREFIXES = ("openrouter-", "seed-", "doubao-", "qwen-")


def count_codex_image_files_per_day() -> dict[str, int]:
    """Count Codex-generated PNG files per local date (by mtime).

    Why: ccusage's codex daily JSON only captures LLM token_count events. The
    built-in image_gen tool (gpt-image-2) doesn't surface as a model, so its
    cost is silently dropped. PNGs on disk are 1:1 with billable generations.
    """
    counts: dict[str, int] = {}
    if not CODEX_IMAGE_GEN_DIR.exists():
        return counts
    for path in CODEX_IMAGE_GEN_DIR.rglob("*.png"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        d = datetime.fromtimestamp(mtime).date().isoformat()
        counts[d] = counts.get(d, 0) + 1
    return counts


def _breakdown_tokens(m: dict) -> int:
    """Total tokens on one modelBreakdowns entry; ccusage gives no rolled-up field."""
    return (
        m.get("inputTokens", 0)
        + m.get("outputTokens", 0)
        + m.get("cacheCreationTokens", 0)
        + m.get("cacheReadTokens", 0)
    )


def fetch_daily_since(since: date) -> tuple[list[dict], list[dict], list[dict]]:
    """一次 ccusage daily --by-agent，拆出 (cc_daily, cx_daily, op_daily)。

    v20+ schema:
      - 日期字段 `period`(v18 是 `date`)
      - 每日行 `agents[]` 按实际调用端给出独立的 modelBreakdowns；模型名称
        不参与 agent 归类，因此 Claude Code 路由到非 Claude 模型也不会漏记。
      - 每个 modelBreakdowns[] 给到 per-model cost + 四类 tokens,但没有
        per-model totalTokens,需要把四类相加。

    Codex 桶额外带:
      - filesystem-derived imageCount (ccusage 无法捕获 gpt-image-2 计费)
      - per-model {totalTokens} 映射,供下游 debug
      - raw API-equiv cost,/fast 乘数和 image_gen 价格留给 pusher 在展示侧应用。

    其余 ccusage 支持的 agent 不属于本仓库的三个 store，保持忽略。
    """
    config_day = datetime.now(usage_schema.SHANGHAI).date()
    pricing = load_pricing()
    with ccusage_config_file(config_day) as config_path:
        out = subprocess.run(
            CCUSAGE_CMD + [
                "--offline", "--config", str(config_path),
                "--since", since.strftime("%Y%m%d"),
            ],
            capture_output=True, text=True, check=True,
            timeout=CCUSAGE_TIMEOUT_SECONDS,
        )
    raw = json.loads(out.stdout).get("daily", [])
    fs_image_counts = count_codex_image_files_per_day()

    cc_daily: list[dict] = []
    cx_daily: list[dict] = []
    op_daily: list[dict] = []
    seen_codex_dates: set[str] = set()
    destinations = {"claude": cc_daily, "codex": cx_daily, "opencode": op_daily}
    for row in raw:
        d = row["period"]
        for agent_row in row.get("agents", []):
            agent = agent_row.get("agent")
            destination = destinations.get(agent)
            if destination is None:
                continue
            tokens = 0
            cost = 0.0
            models: dict[str, dict] = {}
            fully_priced = True
            usage_day = date.fromisoformat(d)
            for m in agent_row.get("modelBreakdowns", []):
                model_tokens = _breakdown_tokens(m)
                breakdown = token_breakdown(m)
                model_name = m["modelName"]
                tokens += model_tokens
                if model_tokens and active_rate(model_name, usage_day, pricing) is None:
                    fully_priced = False
                cost += official_cost_from_ccusage(
                    model_name, usage_day, config_day, breakdown,
                    m.get("cost", 0.0), pricing,
                )
                models[model_name] = {"totalTokens": model_tokens, **breakdown}
            if not (tokens or cost or models):
                continue
            entry = {
                "date": d, "totalTokens": tokens, "totalCost": cost,
                "models": models, "costTrusted": True,
                "costSource": "official" if fully_priced else "unpriced",
            }
            if agent == "codex":
                entry["imageCount"] = fs_image_counts.get(d, 0)
                seen_codex_dates.add(d)
            destination.append(entry)

    # Edge case: PNG 存在但 ccusage 那天的 session 已被 rotate — 补一个 stub 让
    # imageCount 仍能进入 cumulative store。
    for d, cnt in fs_image_counts.items():
        if d in seen_codex_dates:
            continue
        try:
            if date.fromisoformat(d) < since:
                continue
        except ValueError:
            continue
        cx_daily.append({
            "date": d, "totalTokens": 0, "totalCost": 0.0,
            "models": {}, "imageCount": cnt,
            # Says nothing about that day's tokens — ccusage no longer has the
            # session at all. Only imageCount is real, so reconciliation, which
            # otherwise lets a fetch overwrite history downward, must not read
            # these zeros as "the day turned out to be empty".
            "tokensObserved": False,
        })

    return cc_daily, cx_daily, op_daily


@contextmanager
def _lowercased_codex_home(source: Path) -> Iterator[Path]:
    """Yield a throwaway CODEX_HOME whose session model names are normalised.

    ccusage reads the session JSONLs itself and prices them internally, so we
    cannot fix a model name after the fact — the only lever we have is the bytes
    ccusage reads. This mirrors `source/sessions` into a tempdir, rewriting just
    the `"model":"…"` field on every line via _normalise_model (lowercase +
    alias resolution), and hands back the tempdir root for use as CODEX_HOME. The
    real `source` tree is never written.

    A source with no sessions (a machine that never ran traex) yields an empty
    mirror, which ccusage reports as no usage — the same as pointing it straight at
    the empty tree. The tempdir is removed on exit regardless.
    """
    sessions = source / "sessions"
    with tempfile.TemporaryDirectory(prefix="traex-lc-") as tmp:
        mirror_root = Path(tmp)
        if sessions.is_dir():
            mirror_sessions = mirror_root / "sessions"
            for src in sessions.rglob("*.jsonl"):
                dest = mirror_sessions / src.relative_to(sessions)
                dest.parent.mkdir(parents=True, exist_ok=True)
                text = src.read_text(encoding="utf-8")
                dest.write_text(
                    _SESSION_MODEL_RE.sub(
                        lambda m: m.group(1) + usage_schema._normalise_model(m.group(2)) + m.group(3),
                        text,
                    ),
                    encoding="utf-8",
                )
        yield mirror_root


def fetch_multica_codex_daily(
    since: date, sessions: Path | Iterable[Path] | None = None
) -> list[dict]:
    """Multica-orchestrated Codex usage from shared and private rollout trees.

    The rollouts are byte-identical to the ones under ~/.codex/sessions — same CLI,
    same account, same models — so they are read with the same Codex reader, into
    a store of their own. See multica_codex_session_roots and _sync for why the
    combined Multica source keeps a separate high-water mark.
    """
    roots = usage_sources.multica_codex_session_roots() if sessions is None else (
        [sessions] if isinstance(sessions, Path) else list(sessions)
    )
    if not usage_sources._codex_session_files(roots):
        return []
    with usage_sources._codex_home_over(roots) as home:
        return fetch_codex_home_daily(
            since, home, trust_row_cost=True, label="multica codex"
        )


def fetch_codex_home_daily(
    since: date,
    codex_home: Path,
    *,
    lowercase_models: bool = False,
    trust_row_cost: bool = False,
    label: str = "traex",
) -> list[dict]:
    """`ccusage codex daily` against an alternate CODEX_HOME → per-day entries.

    Used for traex, whose sessions live under ~/.trae/cli in the very format the
    Codex reader expects, and for Multica's relocated Codex rollouts. The
    invocation is read-only and touches nothing under the real ~/.codex; we only
    override CODEX_HOME for this one child process. `label` names the caller in
    the unpriced-model notice below.

    `lowercase_models` runs ccusage against a normalised mirror of the sessions
    (see _lowercased_codex_home) so TRAE CLI's capitalised model names match
    ccusage's case-sensitive lowercase price keys and its opaque Claude aliases
    (openrouter-*) resolve to real Opus slugs. Without it every real-name model
    (GPT-5.x, Gemini, DeepSeek) and every alias prices to nothing.

    The `codex daily` schema differs from the unified `daily` fetch_daily_since
    parses: dates arrive under `date` (not `period`), each row carries per-model
    {totalTokens, …} plus a single row-level `costUSD` (there is no per-model
    cost), and every model here is already a Codex-family model, so no agent
    classification is needed — the whole row is one bucket.

    ccusage's row-level costUSD is ignored by default because it cannot separate
    known official models from unknown aliases on mixed days. The per-model token
    buckets are priced directly from config/official-pricing.json; traex records no
    Fast tier, so that path uses the official standard rate. Unknown models are
    deliberately zero and logged when they match a known internal-slug family.

    `trust_row_cost` opts a caller back into ccusage's number on rows where the
    objection above does not apply: every model on the row has an official rate,
    and that rate did not change between the usage day and the day ccusage was
    handed our table. Real Codex sessions need it. ccusage keeps the per-request
    detail this aggregate drops — which calls ran on the priority tier, which ones
    crossed a model's long-context threshold — and neither can be reconstructed
    from a day's totals, so the standard-rate sum silently under-bills them. The
    larger of the two is taken, exactly as official_cost_from_ccusage does for the
    unified fetch.
    """
    if lowercase_models:
        with _lowercased_codex_home(codex_home) as mirror:
            return fetch_codex_home_daily(
                since, mirror, trust_row_cost=trust_row_cost, label=label
            )

    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    config_day = datetime.now(usage_schema.SHANGHAI).date()
    pricing = load_pricing()
    with ccusage_config_file(config_day) as config_path:
        out = subprocess.run(
            CCUSAGE_CODEX_CMD + [
                "--offline", "--config", str(config_path),
                "--since", since.strftime("%Y%m%d"),
            ],
            capture_output=True, text=True, check=True,
            timeout=CCUSAGE_TIMEOUT_SECONDS, env=env,
        )
    raw = json.loads(out.stdout).get("daily", [])
    daily: list[dict] = []
    for row in raw:
        d = row["date"]
        tokens = row.get("totalTokens", 0)
        cost = 0.0
        models = {}
        fully_priced = True
        repriced = False
        usage_day = date.fromisoformat(d)
        for name, raw_model in row.get("models", {}).items():
            breakdown = token_breakdown(raw_model)
            model_tokens = raw_model.get("totalTokens", sum(breakdown.values()))
            models[name] = {"totalTokens": model_tokens, **breakdown}
            canonical = usage_schema._normalise_model(name)
            if model_tokens and (
                active_rate(canonical, usage_day, pricing) is None
                or sum(breakdown.values()) != model_tokens
            ):
                fully_priced = False
            cost += standard_cost(canonical, usage_day, breakdown, pricing)
            if active_rate(canonical, usage_day, pricing) != active_rate(
                canonical, config_day, pricing
            ):
                # ccusage priced this row against config_day's table, so its
                # number does not describe usage_day's rates.
                repriced = True
        if trust_row_cost and fully_priced and not repriced:
            cost = max(float(row.get("costUSD", 0.0) or 0.0), cost)
        # Tokens are accurate regardless of whether the official table knows the
        # model. Unknown shares remain visible in models and contribute zero cost.
        if tokens or models:
            daily.append({
                "date": d, "totalTokens": tokens, "totalCost": cost,
                "models": models, "costTrusted": True,
                "costSource": "official" if fully_priced else "unpriced",
            })
    # Surface slugs we knowingly leave unpriced (Seed/Doubao/Qwen) so a new one
    # gets noticed rather than silently billing zero. A leftover openrouter-* here
    # means _ALIAS_PREFIXES missed a variant and needs a new entry. Model names in
    # `daily` are already normalised on the mirror path, so this only fires for
    # slugs the normaliser did not resolve.
    unpriced_days: dict[str, int] = {}
    for entry in daily:
        for name in entry["models"]:
            if name.startswith(_KNOWN_UNPRICED_PREFIXES):
                unpriced_days[name] = unpriced_days.get(name, 0) + 1
    if unpriced_days:
        print(
            f"{label} left unpriced (no official price entry, counted at zero): "
            + ", ".join(f"{name} ({days}d)" for name, days in sorted(unpriced_days.items())),
            file=sys.stderr,
        )
    return daily
