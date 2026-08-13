# qodo-usage-metrics

Counts Qodo usage as processed pull requests, per user. It answers **one**
question, cheaply and shareably:

> How many pull requests did Qodo process, **per user** — and how many
> **unique users** is that?

It just counts **processed PRs per user** and writes plain CSVs.

## What "processed" means

A PR is **processed** if Qodo reviewed it — it carries at least one
`Code Review by Qodo` comment. Qodo can review a single PR more than once
(for example on each new push); **each PR is counted exactly once** regardless
of how many Qodo reviews it received.

## Why it's fast

It relies entirely on GitHub's search index — the `"Code Review by Qodo"
in:comments` search qualifier returns exactly the processed PRs, with the
author attached. So the tool makes one date-chunked search per org and
**never fetches individual PR bodies, comments, or diffs.** A run over a large
org is typically seconds-to-minutes.

## Prerequisites

- Python 3.7+
- The [`gh` CLI](https://cli.github.com/), installed and authenticated with
  access to the target org(s):

```bash
gh auth status
```

Reading private repos needs the `repo` token scope; without it, private repos
are silently omitted from search results. Add it with:

```bash
gh auth refresh -s repo
```

## Usage

```bash
# Default 90-day lookback, one org
python3 qodo_usage_metrics.py --org acme-corp

# Several orgs in one run (makes the by-org breakdown meaningful)
python3 qodo_usage_metrics.py --org acme-corp widgets-inc

# Custom window
python3 qodo_usage_metrics.py --org acme-corp --since 2025-05-12
python3 qodo_usage_metrics.py --org acme-corp --days 30
python3 qodo_usage_metrics.py --org acme-corp --since 2025-05-12 --until 2025-08-12

# Scope to specific repos within a single org
python3 qodo_usage_metrics.py --org acme-corp --repos frontend-app backend-api

# Anonymize for external sharing (stable pseudonyms; _anon filename suffix)
python3 qodo_usage_metrics.py --org acme-corp --anonymize          # users + repos
python3 qodo_usage_metrics.py --org acme-corp --anonymize users    # users only
python3 qodo_usage_metrics.py --org acme-corp --anonymize repos    # repos only
```

### Options

| Flag | Description |
|---|---|
| `--org` | One or more GitHub org logins (required). Users are pooled across all orgs given. |
| `--since` | Start date `YYYY-MM-DD` (inclusive). Mutually exclusive with `--days`. |
| `--days` | Lookback window in days (default: `90`). |
| `--until` | End date `YYYY-MM-DD` (inclusive; defaults to today). |
| `--repos` | Limit to specific repos — only valid with a single `--org`. |
| `--by {month,week}` | Also emit a timeframe breakout — processed PRs and unique users per period (bucketed by merge date; weeks start Monday). |
| `--chunk-days` | Date-window size per search query (default: `30`). Lower it if a run warns the 1000-result search cap was hit. |
| `--anonymize [SCOPE]` | Replace identifying data with stable pseudonyms. `SCOPE`: `users`, `repos`, or omit for both. Anonymized repos also drop the PR URL. |
| `--output-dir` | Directory to write CSVs into (default: `reports/`). |

## Output

All files are written into `reports/` (created automatically, git-ignored).
The filename stem is `{org}_{since}_{until}` (`{N}orgs_…` for multi-org runs,
plus an `_anon` suffix when anonymized).

| File | Rows | Columns |
|---|---|---|
| `…_by_user.csv` | one per user | `user, processed_prs` |
| `…_processed_prs.csv` | one per processed PR | `org, repo, pr_number, pr_url, user, created_at, merged_at` |

`by_user.csv` is the headline report — processed PRs per user, sorted with the
largest counts first; its row count is the number of **unique users** (also
printed in the run summary). `processed_prs.csv` is the underlying per-PR list,
kept for traceability. Every count treats a PR as a single unit, regardless of
how many times Qodo reviewed it.

With `--by month` (or `--by week`) two more files are written, giving the same
usage/user counts over time (bucketed by merge date, ordered chronologically):

| File | Rows | Columns |
|---|---|---|
| `…_by_{month,week}.csv` | one per period | `period, processed_prs, unique_users` |
| `…_by_user_{month,week}.csv` | one per user × period | `period, user, processed_prs` |

The `period` is `YYYY-MM` for months, or the Monday of the ISO week (`YYYY-MM-DD`)
for weeks. Unique-user counts are per period, so they do not sum to the overall
unique-user total (a user active in two months counts in both).

## Tests

Pure aggregation and anonymization logic is unit-tested without any network:

```bash
python3 -m pytest tests/ -q
```
