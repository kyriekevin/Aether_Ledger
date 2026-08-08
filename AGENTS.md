# Repository guidance

- This repository is designed to be public. Never commit usernames, hostnames, absolute user
  paths, repository names, session identifiers, prompts, or raw session exports.
- Treat `data/{work,personal,devbox}/{claude,codex,opencode}.json` and `data/trail/**` as generated cumulative stores.
  Do not hand-edit them.
- Treat `assets/token-activity.svg` and `assets/token-composition.svg` as generated. Change
  `scripts/render_dashboard.py`, then regenerate the assets.
- Use `uv` to run Python scripts and tests.
- High-frequency data commits belong on `usage/YYYY-MM-DD`, using the Asia/Shanghai calendar day.
  Do not send automated usage commits directly to `main`.
- Completed day branches are squash-merged by `.github/workflows/daily-rollover.yml`. Preserve its
  failure-safe ordering: merge and push `main`, delete completed branches, then create today.
- Keep the README dashboard-first and minimal. Put setup, schemas, recovery, and implementation
  details in `docs/operations.md`.
- Use Conventional Commit subjects. Prefer `feat`, `fix`, `docs`, `test`, `refactor`, or `chore`
  with a focused scope such as `data`, `readme`, `dashboard`, or `automation`. Automated daily
  snapshots use `chore(data): finalize YYYY-MM-DD snapshot`.
- Before handing off changes, run:

  ```sh
  uv run python -m unittest discover -s tests -v
  uv run --script scripts/audit_public.py
  uv run --script scripts/render_dashboard.py --check
  python3 -m py_compile scripts/*.py
  git diff --check
  ```
