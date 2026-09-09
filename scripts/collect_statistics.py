#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Collect versioned statistics without changing the legacy token ledger."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import Counter
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path

import multica_usage
import statistics_readers as readers
import usage_sources
from pricing import load_pricing
from statistics_multica import collect_runs, export_runs
from statistics_store import Journal, SOURCES, export_source
from usage_schema import SHANGHAI
from usage_store import _atomic_write_json

ROOT = Path(__file__).resolve().parents[1]


class MissingStatisticsJournal(ValueError):
    pass


class StatisticsRegression(ValueError):
    pass


def allowed_models(root=ROOT):
    return set(json.loads((root / "config/statistics-models.json").read_text())["models"]) | {"unknown"}


def multica_dsh_roots_readonly():
    # The legacy collector owns persistent profile binding. Statistics discovery
    # uses a temporary copy so a preview cannot create/change production config.
    with tempfile.TemporaryDirectory(prefix="statistics-binding-") as directory:
        binding = Path(directory) / "profile"
        original = usage_sources.MULTICA_DSH_PROFILE_FILE
        if original.exists():
            binding.write_bytes(original.read_bytes())
        return usage_sources.multica_dsh_session_roots(
            home=usage_sources.MULTICA_HOME,
            profile=os.environ.get("MULTICA_DSH_PROFILE") or usage_sources.MULTICA_DSH_PROFILE,
            binding_file=binding)


def collect(machine_dir: Path, *, cache_root: Path | None = None, now=None,
            with_multica=True, reconcile=False, root=ROOT) -> dict:
    from statistics_schema import validate
    now = now or datetime.now(SHANGHAI)
    role = machine_dir.name
    if role not in {"work", "personal", "devbox"}:
        raise ValueError("statistics require a durable role")
    cache_root = cache_root or Path.home() / ".cache/aether-ledger/statistics-v1"
    journal_path = cache_root / (role + ".sqlite3")
    published = machine_dir / "statistics.json"
    if published.exists() and not journal_path.exists():
        raise MissingStatisticsJournal("restore the private journal before collecting")
    journal = Journal(journal_path)
    try:
        pricing, models = load_pricing(root / "config/official-pricing.json"), allowed_models(root)
        if published.exists() and not any(journal.metadata(source) for source in SOURCES):
            raise MissingStatisticsJournal("private journal has no source metadata")
        prior = json.loads(published.read_text()) if published.exists() else {}
        prior_runs = sum(row["total"] for row in (prior.get("multica") or {}).get("days", {}).values())
        # Check before fetching: newly discovered runs must not hide a restored
        # journal that has lost previously published identities.
        if not reconcile and journal.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] < prior_runs:
            raise StatisticsRegression("run journal is behind the published snapshot")
        results = {}
        for source in SOURCES:
            meta = journal.metadata(source)
            since = meta["collectionStarted"] if meta else now.date().isoformat()
            try:
                if source == "codex":
                    reading = readers.codex([usage_sources.CODEX_SESSION_DIR, usage_sources.CODEX_SESSION_DIR.parent / "archived_sessions"], since=since)
                elif source == "codex-multica":
                    workspace_root = os.environ.get("MULTICA_TASK_WORKSPACES_ROOT", "").strip()
                    roots = usage_sources.multica_codex_session_roots(workspaces_root=Path(workspace_root) if workspace_root else usage_sources.MULTICA_TASK_WORKSPACES_ROOT)
                    reading = readers.codex(roots, since=since)
                elif source == "claude":
                    reading = readers.claude(Path.home() / ".claude/projects", since=since)
                elif source == "traex":
                    reading = readers.codex([Path.home() / ".trae/cli/sessions"], harness="traex", since=since)
                elif source == "dsh":
                    reading = readers.dsh(usage_sources.dsh_session_roots(), since=since)
                else:
                    reading = readers.dsh(multica_dsh_roots_readonly(), since=since)
            except Exception:
                reading = readers.Reading(failed=True, diagnostics=Counter(collectionFailed=1))
            journal.ingest(source, reading, now.date(), reconcile=reconcile)
            path = machine_dir / (source + ".json")
            ledger = json.loads(path.read_text()) if path.exists() else {}
            results[source] = export_source(journal, source, ledger, now.date(), models, pricing)
        if with_multica and multica_usage.CONFIG_FILE.exists():
            collect_runs(journal, role, multica_usage.load_runtime_roles(), now, public_models=models)
        snapshot = {"schemaVersion": 1, "role": role, "sources": results,
                    "multica": export_runs(journal, now.date())}
        validate(snapshot, models)
        if published.exists() and not reconcile:
            prior = json.loads(published.read_text())
            for source, old in prior.get("sources", {}).items():
                for day, row in old["days"].items():
                    new = snapshot["sources"].get(source, {}).get("days", {}).get(day)
                    if new is None or any(new["totals"][k] < row["totals"][k] for k in ("calls", "totalTokens")):
                        raise StatisticsRegression("private journal is behind the published snapshot")
            # A stable run identity may move from creation day to start day.
            # Daily decreases are legitimate; loss of retained runs is not.
            new_runs = sum(row["total"] for row in (snapshot["multica"] or {}).get("days", {}).values())
            if new_runs < prior_runs:
                raise StatisticsRegression("run journal is behind the published snapshot")
        _atomic_write_json(machine_dir / "statistics.json", snapshot)
        return snapshot
    finally:
        journal.close()


def main():
    from install_launchd import load_multica_environment
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=["work", "personal", "devbox"], required=True)
    parser.add_argument("--preview", action="store_true", help="use temporary journal/output; never activate production statistics")
    parser.add_argument("--no-multica", action="store_true")
    parser.add_argument("--reconcile", action="store_true", help="accept lower corrected fact bundles; manual use only")
    args = parser.parse_args()
    for key, value in load_multica_environment(Path.home()).items():
        os.environ.setdefault(key, value)
    multica_usage.MULTICA_PROFILE = os.environ.get("MULTICA_PROFILE")
    import usage_git
    lock = nullcontext(True) if args.preview else usage_git.repo_git_lock(usage_git.GIT_LOCK_WAIT_SECONDS)
    with lock as acquired:
        if not acquired:
            raise RuntimeError("statistics writer is busy")
        if not args.preview:
            expected = "usage/" + datetime.now(SHANGHAI).date().isoformat()
            if usage_git._current_branch() != expected:
                raise ValueError("production statistics require today's usage writer checkout")
        with tempfile.TemporaryDirectory(prefix="statistics-preview-") as directory:
            destination = Path(directory) / args.role if args.preview else ROOT / "data" / args.role
            if args.preview:
                destination.mkdir()
                for source in SOURCES:
                    path = ROOT / "data" / args.role / (source + ".json")
                    if path.exists():
                        (destination / path.name).write_bytes(path.read_bytes())
            snapshot = collect(destination, cache_root=Path(directory) / "private" if args.preview else None,
                               with_multica=not args.no_multica, reconcile=args.reconcile)
            for source, result in snapshot["sources"].items():
                latest = result["days"][result["lastAttempt"]]
                print(f"{source}: {result['status']} calls={latest['totals']['calls']} tokens={latest['totals']['totalTokens']} joint={latest['coverage']['jointCalls']}/{latest['totals']['calls']} identified={latest['totals']['identifiedCalls']}/{latest['totals']['calls']} reconciliation={latest['reconciliation']['state']} diagnostics={result['diagnostics']}")
            if snapshot["multica"]:
                print(f"multica: {snapshot['multica']['status']}")
    failed = any(s["status"] in {"partial", "failed"} for s in snapshot["sources"].values())
    failed = failed or bool(snapshot["multica"] and (snapshot["multica"]["status"] == "failed" or snapshot["multica"]["assignmentStatus"] == "failed"))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # CLI errors may contain private paths or issue IDs. Public diagnostics
        # report the failure class, never raw command output.
        print(f"statistics failed: {type(error).__name__}")
        raise SystemExit(1)
