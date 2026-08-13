#!/usr/bin/env python3
"""
Count Qodo-processed pull requests per user.

A PR is "processed" if Qodo reviewed it — i.e. it carries at least one
"Code Review by Qodo" comment. Qodo may review a single PR more than once
(e.g. on each push); this tool counts each PR exactly once regardless of how
many Qodo reviews it received.

It answers one question: how many PRs did Qodo process, per user, and how many
unique users is that?

It works entirely from GitHub's search index (the `"Code Review by Qodo"
in:comments` qualifier), so it makes one date-chunked search per org and never
fetches individual PR bodies. Authentication is handled by the `gh` CLI — just
make sure `gh auth status` shows you logged in with access to the org's repos.

Output: CSV files only, written into ./reports/ (created automatically).

Usage:
  # Default 90-day lookback, one org
  python3 qodo_usage_metrics.py --org acme-corp

  # Several orgs in one run (users are pooled across them)
  python3 qodo_usage_metrics.py --org acme-corp widgets-inc

  # Custom window
  python3 qodo_usage_metrics.py --org acme-corp --since 2025-05-12
  python3 qodo_usage_metrics.py --org acme-corp --days 30

  # Scope to specific repos within a single org
  python3 qodo_usage_metrics.py --org acme-corp --repos frontend-app backend-api

  # Anonymize users and/or repos for external sharing
  python3 qodo_usage_metrics.py --org acme-corp --anonymize
"""

import argparse
import csv
import json
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional

REPORTS_DIR = Path("reports")

# The stable marker Qodo writes into its review comment. Bot account names vary
# between deployments, so we match on this string rather than an account login.
QODO_MARKER = "Code Review by Qodo"

# GraphQL search returning only the fields the slim report needs.
_GQL_SEARCH_QUERY = (
    "query($q:String!,$cursor:String){"
    "search(query:$q,type:ISSUE,first:100,after:$cursor){"
    "issueCount "
    "pageInfo{hasNextPage endCursor}"
    "nodes{...on PullRequest{"
    "number "
    "repository{nameWithOwner} "
    "url "
    "author{login} "
    "createdAt mergedAt"
    "}}}}"
)

# gh/GitHub transient failures worth retrying: HTTP 5xx and HTTP/2 stream
# CANCEL/INTERNAL_ERROR frames emitted by GitHub's edge on expensive queries.
_TRANSIENT_HTTP = re.compile(r"HTTP 5\d\d|stream error.*(?:CANCEL|INTERNAL_ERROR)")

UNKNOWN_USER = "(unknown)"


# --------------------------------------------------------------------------- #
# gh transport
# --------------------------------------------------------------------------- #

def _rate_limit_reset_epoch() -> Optional[int]:
    """Return the Unix timestamp when the GitHub search rate limit resets."""
    try:
        out = subprocess.run(
            ["gh", "api", "rate_limit", "--jq", ".resources.search.reset"],
            capture_output=True, text=True, timeout=15,
        )
        return int(out.stdout.strip())
    except Exception:
        return None


def run_gh(args: List[str]) -> str:
    """Run `gh` and return stdout, retrying on rate limits and transient HTTP errors.

    Exits the process on a hard (non-transient) failure, so a broken run fails
    loudly rather than producing a partial report.
    """
    cmd = ["gh"] + args
    rate_retried = False
    http_retries = 0
    max_http_retries = 3
    while True:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        if result.returncode == 0:
            return result.stdout
        stderr = result.stderr
        if "rate limit" in stderr.lower() and not rate_retried:
            rate_retried = True
            reset = _rate_limit_reset_epoch()
            wait = max(0, reset - int(time.time())) + 5 if reset else 60
            print(f"\n  Rate limit hit — waiting {wait}s before retry...",
                  file=sys.stderr, flush=True)
            time.sleep(wait)
            continue
        m = _TRANSIENT_HTTP.search(stderr)
        if m and http_retries < max_http_retries:
            http_retries += 1
            wait = 5 * (3 ** (http_retries - 1))  # 5s, 15s, 45s
            print(f"\n  {m.group()} — retrying in {wait}s "
                  f"({http_retries}/{max_http_retries})...",
                  file=sys.stderr, flush=True)
            time.sleep(wait)
            continue
        sys.exit(f"`{' '.join(cmd)}` failed:\n{stderr}")


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #

def search_processed_prs(
    org: str,
    since: date,
    until: Optional[date] = None,
    repos: Optional[List[str]] = None,
    chunk_days: int = 30,
) -> Iterator[dict]:
    """Yield one dict per Qodo-processed merged PR in [since, until] for `org`.

    Each PR is yielded at most once even if it matches multiple search chunks.
    Uses the `"Code Review by Qodo" in:comments` qualifier so only PRs Qodo
    actually reviewed come back — no per-PR fetch required.

    GitHub's Search API caps any single query at 1000 results; the run is
    date-chunked to stay under that, and a warning is printed if a chunk still
    hits the cap.
    """
    qualifiers = [f"repo:{org}/{r}" for r in repos] if repos else [f"org:{org}"]
    end = until if until is not None else date.today()
    seen = set()
    cursor = since
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=chunk_days), end)
        for qual in qualifiers:
            qual_label = qual.split(":", 1)[1]
            print(f"  Searching {qual_label}  {cursor} .. {chunk_end} ...",
                  end="", file=sys.stderr, flush=True)
            q = (
                f'{qual} is:pr is:merged "{QODO_MARKER}" in:comments '
                f"merged:{cursor.isoformat()}..{chunk_end.isoformat()}"
            )
            end_cursor = None
            chunk_count = 0
            issue_count = 0
            while True:
                gh_args = ["api", "graphql", "-f", f"query={_GQL_SEARCH_QUERY}",
                           "-f", f"q={q}"]
                if end_cursor:
                    gh_args += ["-f", f"cursor={end_cursor}"]
                else:
                    gh_args += ["-F", "cursor=null"]
                data = json.loads(run_gh(gh_args))
                search = data["data"]["search"]
                issue_count = search.get("issueCount", 0)
                for node in search["nodes"]:
                    if not node.get("number"):
                        continue  # non-PR hit from the shared issue index
                    owner, repo = node["repository"]["nameWithOwner"].split("/", 1)
                    key = (owner, repo, node["number"])
                    if key in seen:
                        continue
                    seen.add(key)
                    chunk_count += 1
                    yield {
                        "org": owner,
                        "repo": repo,
                        "pr_number": node["number"],
                        "pr_url": node.get("url", ""),
                        "user": (node.get("author") or {}).get("login") or UNKNOWN_USER,
                        "created_at": node.get("createdAt", ""),
                        "merged_at": node.get("mergedAt", ""),
                    }
                if not search["pageInfo"]["hasNextPage"]:
                    break
                end_cursor = search["pageInfo"]["endCursor"]
            print(f" {chunk_count} PRs", file=sys.stderr)
            if issue_count > chunk_count and issue_count >= 1000:
                print(f"  Warning: search cap hit for {cursor}..{chunk_end} "
                      f"({qual_label}): {chunk_count}/{issue_count} — some PRs may be "
                      f"missing. Re-run with a smaller --chunk-days.",
                      file=sys.stderr)
        cursor = chunk_end + timedelta(days=1)


# --------------------------------------------------------------------------- #
# Aggregation (pure — unit-tested)
# --------------------------------------------------------------------------- #

def aggregate_by_user(rows: List[dict]) -> List[dict]:
    """Roll processed-PR rows up into a per-user count.

    Every PR counts once (the searcher de-dupes by PR, so a PR Qodo reviewed
    multiple times is still one row here). Sorted with the largest counts first,
    ties broken alphabetically. The length of the returned list is the number of
    unique users.
    """
    by_user: Counter = Counter(r["user"] for r in rows)
    return [
        {"user": u, "processed_prs": c}
        for u, c in sorted(by_user.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


# --------------------------------------------------------------------------- #
# Anonymization (pure — unit-tested)
# --------------------------------------------------------------------------- #

def build_anon_maps(rows: List[dict], scope: str):
    """Build stable user/org/repo pseudonym maps for the given scope.

    scope: 'users' pseudonymizes the user column only; 'repos' pseudonymizes
    org + repo columns; 'all' does both. Names are assigned in first-appearance
    order so the mapping is deterministic for a given input ordering.
    """
    user_map: Dict[str, str] = {}
    org_map: Dict[str, str] = {}
    repo_map: Dict[str, str] = {}  # keyed by "org/repo"
    do_users = scope in ("users", "all")
    do_repos = scope in ("repos", "all")
    for r in rows:
        if do_users and r["user"] not in user_map:
            user_map[r["user"]] = f"user-{len(user_map) + 1:02d}"
        if do_repos:
            if r["org"] not in org_map:
                org_map[r["org"]] = f"org-{len(org_map) + 1:02d}"
            full = f"{r['org']}/{r['repo']}"
            if full not in repo_map:
                repo_map[full] = f"repo-{len(repo_map) + 1:02d}"
    return user_map, org_map, repo_map


def apply_anonymization(rows: List[dict], user_map, org_map, repo_map) -> List[dict]:
    """Return new rows with identifying columns replaced by their pseudonyms.

    Anonymized repos also drop the PR URL (it leaks the real org/repo/number).
    """
    out = []
    for r in rows:
        nr = dict(r)
        if user_map:
            nr["user"] = user_map.get(r["user"], r["user"])
        if repo_map:
            full = f"{r['org']}/{r['repo']}"
            nr["repo"] = repo_map.get(full, r["repo"])
            nr["org"] = org_map.get(r["org"], r["org"])
            nr["pr_url"] = ""
        out.append(nr)
    return out


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

RAW_COLUMNS = ["org", "repo", "pr_number", "pr_url", "user", "created_at", "merged_at"]


def _write_csv(path: Path, columns: List[str], rows: Iterable[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, restval="")
        writer.writeheader()
        writer.writerows(rows)


def write_all(rows: List[dict], stem: str, out_dir: Path) -> List[Path]:
    """Write the per-user count CSV plus the raw per-PR evidence CSV.

    Returns the written paths. by_user is the headline report; the raw file is
    the underlying processed-PR list, kept for traceability.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []

    by_user_path = out_dir / f"{stem}_by_user.csv"
    _write_csv(by_user_path, ["user", "processed_prs"], aggregate_by_user(rows))
    written.append(by_user_path)

    raw_path = out_dir / f"{stem}_processed_prs.csv"
    _write_csv(raw_path, RAW_COLUMNS,
               sorted(rows, key=lambda r: (r["org"], r["repo"], r["pr_number"])))
    written.append(raw_path)

    return written


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _output_stem(orgs: List[str], since: date, until: date, anonymize: Optional[str]) -> str:
    org_part = orgs[0] if len(orgs) == 1 else f"{len(orgs)}orgs"
    suffix = "_anon" if anonymize else ""
    return f"{org_part}_{since.isoformat()}_{until.isoformat()}{suffix}"


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--org", nargs="+", required=True, metavar="ORG",
                   help="One or more GitHub org logins (e.g. --org acme-corp widgets-inc)")
    window = p.add_mutually_exclusive_group()
    window.add_argument("--since", type=date.fromisoformat, metavar="YYYY-MM-DD",
                        help="Start date (inclusive)")
    window.add_argument("--days", type=int, default=90,
                        help="Lookback window in days (default: 90)")
    p.add_argument("--until", type=date.fromisoformat, metavar="YYYY-MM-DD",
                   help="End date (inclusive; defaults to today)")
    p.add_argument("--repos", nargs="+", metavar="REPO",
                   help="Limit to specific repos (only valid with a single --org)")
    p.add_argument("--chunk-days", type=int, default=30, metavar="N",
                   help="Date-window size per search query (default: 30). "
                        "Lower it if a run warns the 1000-result search cap was hit.")
    p.add_argument("--anonymize", nargs="?", const="all", default=None,
                   choices=["all", "users", "repos"], metavar="SCOPE",
                   help="Replace identifying data with stable pseudonyms. "
                        "SCOPE: 'users', 'repos', or omit to anonymize both. "
                        "Output filenames get an _anon suffix.")
    p.add_argument("--output-dir", type=Path, default=REPORTS_DIR, metavar="DIR",
                   help="Directory to write CSVs into (default: reports/)")
    args = p.parse_args()

    orgs = list(dict.fromkeys(args.org))  # de-dupe, preserve order
    if args.repos and len(orgs) > 1:
        p.error("--repos is only valid with a single --org")
    if args.repos:
        args.repos = list(dict.fromkeys(args.repos))

    until = args.until or date.today()
    since = args.since or (until - timedelta(days=args.days))
    if since > until:
        p.error(f"--since ({since}) is after --until ({until})")

    rows: List[dict] = []
    for org in orgs:
        print(f"\nOrg: {org}", file=sys.stderr)
        rows.extend(search_processed_prs(
            org, since, until, repos=args.repos, chunk_days=args.chunk_days))

    if args.anonymize:
        user_map, org_map, repo_map = build_anon_maps(rows, args.anonymize)
        rows = apply_anonymization(rows, user_map, org_map, repo_map)

    stem = _output_stem(orgs, since, until, args.anonymize)
    written = write_all(rows, stem, args.output_dir)

    by_user = aggregate_by_user(rows)
    print()
    print(f"Window:            {since} → {until}")
    print(f"Orgs:              {', '.join(orgs)}")
    print(f"Processed PRs:     {len(rows)}")
    print(f"Unique users:      {len(by_user)}")
    print("\nCSVs written:")
    for path in written:
        print(f"  {path}")


if __name__ == "__main__":
    main()
