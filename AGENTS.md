# Repository guidance

- This repository is designed to be public. Never commit usernames, hostnames, absolute user
  paths, repository names, session identifiers, prompts, or raw session exports.
- Treat `data/{work,personal,devbox}/{claude,codex,opencode}.json` and `data/trail/**` as generated cumulative stores.
  Do not hand-edit them.
- Treat `assets/token-activity.svg` and `assets/token-topology.svg` as generated. Change
  `scripts/render_dashboard.py`, then regenerate the assets.
- Use `uv` to run Python scripts and tests.
- High-frequency data commits belong on `usage/YYYY-MM-DD`, using the Asia/Shanghai calendar day.
  Do not send automated usage commits directly to `main`.
- Completed day branches are squash-merged by `.github/workflows/daily-rollover.yml`. Preserve its
  failure-safe ordering: merge and push `main`, delete completed branches, then create today.
- Keep the README dashboard-first and minimal. Put setup, schemas, recovery, and implementation
  details in `docs/operations.md`.
- Keep English and Chinese user-facing documentation semantically aligned in the same change.
- Use Conventional Commit subjects. Prefer `feat`, `fix`, `docs`, `test`, `refactor`, or `chore`
  with a focused scope such as `data`, `readme`, `dashboard`, or `automation`. Automated daily
  snapshots use `chore(data): finalize YYYY-MM-DD snapshot`.
- Land human changes through a pull request. Never commit or push directly to `main`; the daily
  rollover workflow is the only writer that pushes there.
- Commit with a no-reply email. This repository is public, so commit metadata is published too.
- Before handing off changes, run `make verify`. It bundles the unit tests, the public-data
  audit, the dashboard freshness check, script byte-compilation, and `git diff --check`.
  `.github/workflows/verify.yml` enforces the same set on every pull request, and additionally
  audits the incoming commits for personal email addresses. Run the checks locally anyway: a red
  CI run costs a round trip.
