# AGENTS.md

Guidance for AI coding agents working in this repo.

## What this repo does

One job: report how many **active Qodo users** a GitHub org has — the distinct
developers whose pull requests Qodo reviewed in a time window — and list who
they are. That is the *only* metric this tool returns: it does **not** report
per-user or per-PR review counts. All logic lives in `qodo_usage_metrics.py`;
tests in `tests/`; CSV output in `reports/` (git-ignored).

## Run it

```bash
python3 qodo_usage_metrics.py --org <github-org>
```

- Requires the `gh` CLI authenticated (`gh auth status`). Private repos need the
  `repo` token scope, or they are silently omitted and the count is too low.
- Standard library only — no third-party dependencies, no virtualenv needed.
- The active-user count is the `Active Qodo users` line in the run summary, and
  equals the row count of `reports/…_active_users.csv`.

## Tests

```bash
python3 -m pytest tests/ -q
```

Pure logic (the active-user set, per-period breakouts, anonymization) is
unit-tested with no network. The `gh`/GitHub search transport is intentionally
not mocked beyond the search stub — don't add real network calls into tests.

## Guardrails

- **Active users are the only output.** The tool reports the distinct developers
  Qodo reviewed and nothing more — no per-user PR counts, no total PR count, no
  per-PR evidence file. If a change would surface a processed-/reviewed-PR count
  in any CSV column, the console summary, or the docs, it's out of scope: keep
  the output to active users. This is the source of truth for the repo.
- **CSV schema.** `…_active_users.csv` has a single `user` column (one row per
  active user). The `--by` breakouts are `period,active_users` (counts) and
  `period,user` (membership). No column carries a PR count.
- **Standard library only.** Don't add third-party dependencies.
- **A user counts once.** A PR Qodo reviewed multiple times, and a developer
  with many reviewed PRs, each collapse to one active user — the searcher
  de-dupes by PR and `active_users()` de-dupes by login. Keep that intact.
- **Keep it fast.** The tool works only from GitHub's search index and must
  never fetch individual PR bodies, comments, or diffs.
- **"Active user" = usage, not seats** — a developer whose PR Qodo reviewed in
  the window. Preserve that definition consistently across code and docs.
