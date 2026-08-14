# AGENTS.md

Guidance for AI coding agents working in this repo.

## What this repo does

One job: report how many **active Qodo users** a GitHub org has — the distinct
developers whose pull requests Qodo reviewed in a time window — plus a per-user
PR count. All logic lives in `qodo_usage_metrics.py`; tests in `tests/`; CSV
output in `reports/` (git-ignored).

## Run it

```bash
python3 qodo_usage_metrics.py --org <github-org>
```

- Requires the `gh` CLI authenticated (`gh auth status`). Private repos need the
  `repo` token scope, or they are silently omitted and the count is too low.
- Standard library only — no third-party dependencies, no virtualenv needed.
- The active-user count is the `Active Qodo users` line in the run summary, and
  equals the row count of `reports/…_by_user.csv`.

## Tests

```bash
python3 -m pytest tests/ -q
```

Pure logic (aggregation + anonymization) is unit-tested with no network. The
`gh`/GitHub search transport is intentionally not mocked — don't add network
calls into tests.

## Guardrails

- **Standard library only.** Don't add third-party dependencies.
- **CSV schema is a data contract.** The column names (`user`,
  `processed_prs`, `period`, `unique_users`, and the raw `RAW_COLUMNS`) are
  consumed downstream and asserted in tests — don't rename them. User-facing
  *labels* (README, run summary, help text) may say "active users"; the CSV
  schema stays as-is.
- **Count each PR once**, even if Qodo reviewed it multiple times — the searcher
  de-dupes by PR key; keep that intact.
- **Keep it fast.** The tool works only from GitHub's search index and must
  never fetch individual PR bodies, comments, or diffs.
- **"Active user" = usage, not seats** — a developer whose PR Qodo reviewed in
  the window. Preserve that definition consistently across code and docs.
