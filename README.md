# qodo-usage-metrics

**How many people are actually using Qodo?**

This tool prints one number: your **active Qodo users** — the distinct
developers whose pull requests Qodo reviewed in a time window.

```
============================================
  Active Qodo users:   42
============================================
```

## Getting started

You need Python 3.7+ (already on macOS/Linux) and the GitHub CLI. Three steps:

1. **Install the GitHub CLI and log in** (one time):

   ```bash
   brew install gh      # macOS — or see https://cli.github.com
   gh auth login
   ```

2. **Run it** on your GitHub org (replace `your-org`):

   ```bash
   python3 qodo_usage_metrics.py --org your-org
   ```

3. **Read the `Active Qodo users` line** it prints. That's your answer.

No install and no dependencies — just Python and the `gh` CLI you set up in
step 1.

> **Private repos:** if Qodo runs on private repos, grant your login the `repo`
> scope once — `gh auth refresh -s repo` — then re-run. Skip this and those
> repos are silently ignored, so the number comes out too low.

## Want more?

- **A different window:** add `--days 30` (default is 90).
- **Share it outside your company:** add `--anonymize` to replace names with
  pseudonyms.
- **Everything else** (date ranges, per-repo scope, monthly breakouts, CSV
  output): run `python3 qodo_usage_metrics.py --help`.

Full per-user detail is also written to CSV files in `reports/`.
