#!/usr/bin/env python3
"""
Count Qodo-processed pull requests per user.

A PR is "processed" if Qodo reviewed it — i.e. its comments carry a Qodo review
heading (e.g. "Code Review by Qodo" or "PR Reviewer Guide"). Qodo may review a
single PR more than once (e.g. on each push); this tool counts each PR exactly
once regardless of how many Qodo reviews it received.

It answers one question: how many PRs did Qodo process, per user, and how many
unique users is that?

It works entirely from GitHub's search index (a Qodo review marker matched
`in:comments`), so it makes one date-chunked search per org and never fetches
individual PR bodies. Authentication is handled by the `gh` CLI — just make sure
`gh auth status` shows you logged in with access to the org's repos.

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

  # Timeframe breakout: usage and unique users per month (or per week)
  python3 qodo_usage_metrics.py --org acme-corp --by month

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

# Stable phrases Qodo writes as the heading of its PR review comment. Bot account
# names vary between deployments, so we match on comment text rather than an
# account login. Qodo's review surfaces use different headings:
#   - "Code Review by Qodo" — the newer agentic / Qodo Merge review
#   - "PR Reviewer Guide"   — the classic Qodo Merge / PR-Agent `/review` output
# A PR counts if ANY marker appears in its comments (the markers are unioned into
# one search and the PR is de-duped), so the number reflects reviews from either
# surface. Deployments whose heading differs can override the set with --marker.
DEFAULT_QODO_MARKERS = ("Code Review by Qodo", "PR Reviewer Guide")


def markers_qualifier(markers: List[str]) -> str:
    """Build the search term matching any of `markers` in a PR's comments.

    One quoted phrase for a single marker; a parenthesised `OR` group for
    several. GitHub unions the phrases within one query (verified to equal the
    union of the per-marker result counts), so a single search returns PRs
    carrying any marker and the searcher then de-dupes by PR.
    """
    quoted = [f'"{m}"' for m in markers]
    if len(quoted) == 1:
        return f"{quoted[0]} in:comments"
    return f'({" OR ".join(quoted)}) in:comments'

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
    "state "
    "author{login} "
    "createdAt mergedAt"
    "}}}}"
)

# gh/GitHub transient failures worth retrying: HTTP 5xx and HTTP/2 stream
# CANCEL/INTERNAL_ERROR frames emitted by GitHub's edge on expensive queries.
_TRANSIENT_HTTP = re.compile(r"HTTP 5\d\d|stream error.*(?:CANCEL|INTERNAL_ERROR)")

# GitHub throttles in two unrelated ways that need opposite handling:
#   * primary   — the GraphQL point quota is exhausted; the only cure is to wait
#                 until the bucket resets (a fixed time, not a guess).
#   * secondary — an anti-abuse / request-velocity limit; GitHub's guidance is to
#                 back off "at least one minute" and grow exponentially from there.
# Secondary messages also contain the substring "rate limit", so they must be
# tested first. See classify_rate_limit / run_gh.
_SECONDARY_RATE_LIMIT = re.compile(
    r"secondary rate limit|exceeded a secondary|abuse detection|"
    r"you have triggered|retry your request|submitted too quickly", re.I)
_PRIMARY_RATE_LIMIT = re.compile(r"rate limit", re.I)

# Retry tuning.
_REQUEST_SPACING_S = 0.5        # preventive pause before each gh call (velocity)
_MAX_TRANSIENT_RETRIES = 3      # timeouts + HTTP 5xx / stream errors: 5s,15s,45s
_MAX_PRIMARY_RETRIES = 3        # quota-reset waits
_MAX_SECONDARY_RETRIES = 5      # anti-abuse backoffs
_SECONDARY_BACKOFF_BASE_S = 60  # GitHub's documented floor for secondary limits
_SECONDARY_BACKOFF_CAP_S = 300  # never sleep more than this on one backoff
_PRIMARY_WAIT_CAP_S = 900       # beyond this, exit loudly rather than sleep silently


def classify_rate_limit(stderr: str) -> Optional[str]:
    """Classify a gh stderr string as a 'secondary', 'primary', or non-rate error.

    Returns 'secondary', 'primary', or None. Secondary (anti-abuse) messages also
    contain "rate limit", so they are matched first; the two need opposite
    handling (backoff vs wait-to-reset), so distinguishing them matters.
    """
    if _SECONDARY_RATE_LIMIT.search(stderr):
        return "secondary"
    if _PRIMARY_RATE_LIMIT.search(stderr):
        return "primary"
    return None


UNKNOWN_USER = "(unknown)"

# GitHub's Search API returns at most this many results for any single query,
# no matter how you paginate. A date window with more matches than this is
# truncated, so the tool splits such a window in half and re-searches each side
# until every sub-window is under the cap (see search_processed_prs).
SEARCH_RESULT_CAP = 1000


# --------------------------------------------------------------------------- #
# gh transport
# --------------------------------------------------------------------------- #

def _rate_limit_reset_epoch(resource: str = "graphql") -> Optional[int]:
    """Return the Unix timestamp when the given GitHub rate-limit bucket resets.

    Defaults to the `graphql` bucket because every search this tool makes goes
    through the GraphQL API — the REST `search` bucket is a different, unrelated
    quota. Querying `/rate_limit` does not itself consume any quota.
    """
    try:
        out = subprocess.run(
            ["gh", "api", "rate_limit", "--jq", f".resources.{resource}.reset"],
            capture_output=True, text=True, timeout=15,
        )
        return int(out.stdout.strip())
    except Exception:
        return None


def run_gh(args: List[str]) -> str:
    """Run `gh` and return stdout, retrying on rate limits and transient errors.

    Three failure modes, three treatments (see the module constants):
      * transient (timeout, HTTP 5xx, HTTP/2 stream CANCEL/INTERNAL_ERROR):
        short exponential backoff (5s/15s/45s), then give up loudly.
      * primary rate limit (GraphQL quota exhausted): wait until the GraphQL
        bucket resets — capped, so an hour-away reset exits loudly with the reset
        time instead of sleeping in silence.
      * secondary rate limit (anti-abuse / velocity): exponential backoff from
        GitHub's documented 60s floor (60s/120s/240s…), capped per wait. GitHub
        may send a Retry-After header here, but `gh api` does not surface
        response headers, so we fall back to backoff.

    A small pre-request pause paces successive calls to avoid tripping the
    velocity-based secondary limit in the first place. Exits the process on a
    hard failure or once a retry budget is exhausted, so a broken run fails
    loudly rather than emitting a partial report.
    """
    cmd = ["gh"] + args
    transient_retries = 0
    primary_retries = 0
    secondary_retries = 0
    while True:
        if _REQUEST_SPACING_S:
            time.sleep(_REQUEST_SPACING_S)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    encoding="utf-8", timeout=120)
        except subprocess.TimeoutExpired:
            # A hung request is a transient failure like a 5xx — retry on the
            # same backoff, then give up loudly rather than blocking forever.
            if transient_retries < _MAX_TRANSIENT_RETRIES:
                transient_retries += 1
                wait = 5 * (3 ** (transient_retries - 1))  # 5s, 15s, 45s
                print(f"\n  request timed out — retrying in {wait}s "
                      f"({transient_retries}/{_MAX_TRANSIENT_RETRIES})...",
                      file=sys.stderr, flush=True)
                time.sleep(wait)
                continue
            sys.exit(f"`{' '.join(cmd)}` timed out after {_MAX_TRANSIENT_RETRIES} retries")
        if result.returncode == 0:
            return result.stdout
        stderr = result.stderr

        kind = classify_rate_limit(stderr)
        if kind == "primary" and primary_retries < _MAX_PRIMARY_RETRIES:
            primary_retries += 1
            reset = _rate_limit_reset_epoch("graphql")
            if reset:
                wait = max(0, reset - int(time.time())) + 5
                if wait > _PRIMARY_WAIT_CAP_S:
                    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(reset))
                    sys.exit(f"GitHub GraphQL rate limit exhausted; it resets at "
                             f"{when} (~{wait}s away). Re-run after that.")
            else:
                wait = _SECONDARY_BACKOFF_BASE_S
            print(f"\n  Primary rate limit hit — waiting {wait}s for the GraphQL "
                  f"quota to reset ({primary_retries}/{_MAX_PRIMARY_RETRIES})...",
                  file=sys.stderr, flush=True)
            time.sleep(wait)
            continue
        if kind == "secondary" and secondary_retries < _MAX_SECONDARY_RETRIES:
            secondary_retries += 1
            wait = min(_SECONDARY_BACKOFF_BASE_S * (2 ** (secondary_retries - 1)),
                       _SECONDARY_BACKOFF_CAP_S)
            print(f"\n  Secondary rate limit hit — backing off {wait}s "
                  f"({secondary_retries}/{_MAX_SECONDARY_RETRIES})...",
                  file=sys.stderr, flush=True)
            time.sleep(wait)
            continue

        m = _TRANSIENT_HTTP.search(stderr)
        if m and transient_retries < _MAX_TRANSIENT_RETRIES:
            transient_retries += 1
            wait = 5 * (3 ** (transient_retries - 1))  # 5s, 15s, 45s
            print(f"\n  {m.group()} — retrying in {wait}s "
                  f"({transient_retries}/{_MAX_TRANSIENT_RETRIES})...",
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
    merged_only: bool = False,
    markers: Optional[List[str]] = None,
) -> Iterator[dict]:
    """Yield one dict per Qodo-processed PR created in [since, until] for `org`.

    A PR is "processed" if Qodo reviewed it — a `<marker> in:comments` qualifier
    returns exactly those, no per-PR fetch required. `markers` defaults to
    DEFAULT_QODO_MARKERS and a PR matching any of them counts once. By
    default every reviewed PR is yielded regardless of state (open, merged, or
    closed-unmerged) so the unique-user count reflects everyone Qodo reviewed;
    pass `merged_only=True` to restrict to merged PRs.

    The window filters on PR *creation* date (`created:`), which every PR has —
    unlike merge date, which unmerged PRs lack. Each PR is yielded at most once
    even if it matches multiple search windows.

    GitHub's Search API caps any single query at SEARCH_RESULT_CAP (1000)
    results. `chunk_days` sets the *initial* window size, but a window whose
    match count still exceeds the cap is **automatically split in half and
    re-searched**, recursively, until every sub-window is under the cap — so the
    count is complete regardless of `chunk_days`. The only irreducible case is a
    single calendar day with more than 1000 reviewed PRs (which date-chunking
    can't split further); that alone prints a loud warning.
    """
    qualifiers = [f"repo:{org}/{r}" for r in repos] if repos else [f"org:{org}"]
    end = until if until is not None else date.today()
    merged_qual = "is:merged " if merged_only else ""
    marker_qual = markers_qualifier(list(markers) if markers else list(DEFAULT_QODO_MARKERS))
    seen = set()

    def _row(node: dict) -> Optional[dict]:
        """Turn a raw PR node into a row, deduping by PR. None if not-a-PR or seen."""
        if not node.get("number"):
            return None  # non-PR hit from the shared issue index
        owner, repo = node["repository"]["nameWithOwner"].split("/", 1)
        key = (owner, repo, node["number"])
        if key in seen:
            return None
        seen.add(key)
        return {
            "org": owner,
            "repo": repo,
            "pr_number": node["number"],
            "pr_url": node.get("url", ""),
            "state": (node.get("state") or "").lower(),
            "user": (node.get("author") or {}).get("login") or UNKNOWN_USER,
            "created_at": node.get("createdAt", ""),
            "merged_at": node.get("mergedAt", ""),
        }

    def _search_window(qual: str, qual_label: str, w_start: date, w_end: date,
                       depth: int) -> Iterator[dict]:
        """Yield rows for one date window, auto-splitting if it hits the cap."""
        indent = "  " + "  " * depth
        q = (
            f'{qual} is:pr {merged_qual}{marker_qual} '
            f"created:{w_start.isoformat()}..{w_end.isoformat()}"
        )
        # Fetch page one first, purely to learn the true total match count
        # (issueCount is the real total, not capped at 1000). If the window is
        # over the cap we split instead of yielding a truncated slice, so we
        # discard this page's nodes here — they reappear, complete, in the
        # sub-windows (and `seen` would dedup them regardless).
        gh_args = ["api", "graphql", "-f", f"query={_GQL_SEARCH_QUERY}",
                   "-f", f"q={q}", "-F", "cursor=null"]
        search = json.loads(run_gh(gh_args))["data"]["search"]
        issue_count = search.get("issueCount", 0)

        if issue_count > SEARCH_RESULT_CAP and w_start < w_end:
            print(f"{indent}Searching {qual_label}  {w_start} .. {w_end} ... "
                  f"{issue_count} hits > {SEARCH_RESULT_CAP} cap — splitting",
                  file=sys.stderr, flush=True)
            mid = w_start + timedelta(days=(w_end - w_start).days // 2)
            yield from _search_window(qual, qual_label, w_start, mid, depth + 1)
            yield from _search_window(qual, qual_label, mid + timedelta(days=1),
                                      w_end, depth + 1)
            return

        print(f"{indent}Searching {qual_label}  {w_start} .. {w_end} ...",
              end="", file=sys.stderr, flush=True)
        count = 0
        while True:
            for node in search["nodes"]:
                row = _row(node)
                if row is not None:
                    count += 1
                    yield row
            if not search["pageInfo"]["hasNextPage"]:
                break
            end_cursor = search["pageInfo"]["endCursor"]
            gh_args = ["api", "graphql", "-f", f"query={_GQL_SEARCH_QUERY}",
                       "-f", f"q={q}", "-f", f"cursor={end_cursor}"]
            search = json.loads(run_gh(gh_args))["data"]["search"]
        print(f" {count} PRs", file=sys.stderr)
        if issue_count > SEARCH_RESULT_CAP:
            # A single day still over the cap: date-chunking can't split further.
            # (Exactly SEARCH_RESULT_CAP is fine — those results are all
            # retrievable; only a strictly larger total truncates.) This is the
            # one residual undercount, so fail loudly rather than quietly
            # returning a truncated slice.
            print(f"{indent}Warning: {w_start} alone has {issue_count} matches "
                  f"(> {SEARCH_RESULT_CAP} cap) for {qual_label}; a single day "
                  f"can't be split further, so some PRs are missing from it.",
                  file=sys.stderr)

    cursor = since
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=chunk_days), end)
        for qual in qualifiers:
            qual_label = qual.split(":", 1)[1]
            yield from _search_window(qual, qual_label, cursor, chunk_end, 0)
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


UNKNOWN_PERIOD = "unknown"


def period_key(iso_ts: str, by: str) -> str:
    """Bucket an ISO-8601 timestamp into a period label.

    `by` is 'month' -> 'YYYY-MM', or 'week' -> the Monday of that ISO week as
    'YYYY-MM-DD'. Blank/unparseable timestamps bucket to 'unknown'.
    """
    if not iso_ts:
        return UNKNOWN_PERIOD
    try:
        d = date.fromisoformat(iso_ts[:10])
    except ValueError:
        return UNKNOWN_PERIOD
    if by == "month":
        return f"{d.year:04d}-{d.month:02d}"
    if by == "week":
        monday = d - timedelta(days=d.weekday())
        return monday.isoformat()
    raise ValueError(f"unknown period: {by!r}")


def aggregate_by_period(rows: List[dict], by: str) -> List[dict]:
    """Per-period processed-PR and unique-user counts, ordered chronologically.

    PRs are bucketed by creation date (every PR has one; unmerged PRs have no
    merge date). 'unknown' (if any) sorts last.
    """
    prs: Counter = Counter()
    users: Dict[str, set] = {}
    for r in rows:
        p = period_key(r.get("created_at", ""), by)
        prs[p] += 1
        users.setdefault(p, set()).add(r["user"])
    return [
        {"period": p, "processed_prs": prs[p], "unique_users": len(users[p])}
        for p in sorted(prs, key=lambda p: (p == UNKNOWN_PERIOD, p))
    ]


def aggregate_by_user_period(rows: List[dict], by: str) -> List[dict]:
    """Per-user, per-period processed-PR counts.

    Ordered by period (chronological, 'unknown' last), then by count descending,
    then user alphabetically. PRs are bucketed by creation date.
    """
    counts: Counter = Counter()
    for r in rows:
        counts[(period_key(r.get("created_at", ""), by), r["user"])] += 1
    return [
        {"period": p, "user": u, "processed_prs": c}
        for (p, u), c in sorted(
            counts.items(),
            key=lambda kv: (kv[0][0] == UNKNOWN_PERIOD, kv[0][0], -kv[1], kv[0][1]),
        )
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

RAW_COLUMNS = ["org", "repo", "pr_number", "pr_url", "state", "user", "created_at", "merged_at"]


def _write_csv(path: Path, columns: List[str], rows: Iterable[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, restval="")
        writer.writeheader()
        writer.writerows(rows)


def write_all(rows: List[dict], stem: str, out_dir: Path,
              by: Optional[str] = None) -> List[Path]:
    """Write the per-user count CSV plus the raw per-PR evidence CSV.

    When `by` is 'month' or 'week', also writes per-period breakouts:
    `_by_{by}.csv` (period totals + unique users) and `_by_user_{by}.csv`
    (per-user counts per period).

    Returns the written paths. by_user is the headline report; the raw file is
    the underlying processed-PR list, kept for traceability.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []

    by_user_path = out_dir / f"{stem}_by_user.csv"
    _write_csv(by_user_path, ["user", "processed_prs"], aggregate_by_user(rows))
    written.append(by_user_path)

    if by:
        period_path = out_dir / f"{stem}_by_{by}.csv"
        _write_csv(period_path, ["period", "processed_prs", "unique_users"],
                   aggregate_by_period(rows, by))
        written.append(period_path)

        user_period_path = out_dir / f"{stem}_by_user_{by}.csv"
        _write_csv(user_period_path, ["period", "user", "processed_prs"],
                   aggregate_by_user_period(rows, by))
        written.append(user_period_path)

    raw_path = out_dir / f"{stem}_processed_prs.csv"
    _write_csv(raw_path, RAW_COLUMNS,
               sorted(rows, key=lambda r: (r["org"], r["repo"], r["pr_number"])))
    written.append(raw_path)

    return written


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def check_token_scope() -> None:
    """Warn (best-effort) if the gh token can't read private repos.

    A classic token without the `repo` scope silently omits private repos from
    search results — an undercount with no error. Fine-grained tokens don't
    expose an X-OAuth-Scopes header, so we can only warn when we can see it.
    """
    try:
        out = subprocess.run(["gh", "api", "-i", "user"],
                             capture_output=True, text=True, timeout=15)
    except Exception:
        # Best-effort advisory only: degrade silently on any failure (timeout,
        # network blip, unexpected output). A scope hint must never crash the
        # run, and the zero-result warning already names token scope as a cause.
        return
    for line in out.stdout.splitlines():
        if line.lower().startswith("x-oauth-scopes:"):
            scopes = [s.strip() for s in line.split(":", 1)[1].split(",")]
            if "repo" not in scopes:
                print("  Warning: your gh token lacks the 'repo' scope; private "
                      "repos are silently omitted from search results (undercount). "
                      "Add it with: gh auth refresh -s repo",
                      file=sys.stderr)
            return


def validate_orgs(orgs: List[str], repos: Optional[List[str]] = None) -> None:
    """Fail loudly on an org login that can't be resolved, warn on non-orgs.

    A typo'd or inaccessible org otherwise yields a clean-looking "0 processed
    PRs" report with exit 0 — the worst failure mode for a shared tool.

    The non-Organization warning is suppressed when `repos` is given: a
    `--repos` run searches with `repo:owner/name` qualifiers, which resolve for
    user-owned repos too, so results are not necessarily empty in that case.
    """
    for org in orgs:
        try:
            out = subprocess.run(["gh", "api", f"users/{org}", "-q", ".type"],
                                 capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            # Pre-flight probe only: don't block an otherwise-legitimate run on a
            # flaky network here. A real connectivity problem resurfaces loudly
            # in run_gh() once searching starts.
            print(f"  Warning: validating org '{org}' timed out; skipping the "
                  f"pre-flight existence check for it.", file=sys.stderr)
            continue
        if out.returncode != 0:
            stderr = out.stderr.strip()
            # A 404 is the definitive "this org login doesn't resolve for your
            # auth" signal — the typo/no-access case this pre-flight exists to
            # catch — so fail loudly. Any other non-zero (HTTP 5xx, rate limit,
            # network/stream error) is transient or ambiguous: don't turn it
            # into a fatal, misleading "typo" error before the run even starts.
            # Warn and let run_gh() surface it, with retries, once search begins.
            if re.search(r"HTTP 404|Not Found", stderr):
                sys.exit(
                    f"Org '{org}' could not be resolved with your gh auth "
                    f"(typo, or no access). Check the name and `gh auth status`.\n"
                    f"{stderr}")
            print(f"  Warning: couldn't complete the pre-flight check for org "
                  f"'{org}' (likely transient); continuing. If the run finds no "
                  f"PRs, re-verify the org name.\n    {stderr}", file=sys.stderr)
            continue
        kind = out.stdout.strip()
        if kind != "Organization" and not repos:
            print(f"  Warning: '{org}' is a {kind}, not an Organization. The "
                  f"`org:` search qualifier only matches organizations, so "
                  f"results for it will be empty. Scope to repos with --repos.",
                  file=sys.stderr)


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
                   help="Initial date-window size per search query (default: 30). "
                        "Windows over GitHub's 1000-result cap are split "
                        "automatically, so this only tunes performance, not "
                        "completeness.")
    p.add_argument("--merged-only", action="store_true",
                   help="Count only merged PRs. Default counts every PR Qodo "
                        "reviewed regardless of state (open/merged/closed).")
    p.add_argument("--marker", nargs="+", metavar="TEXT", default=None,
                   help="Comment heading(s) that identify a Qodo review; a PR "
                        "counts if any appears in its comments. Default: "
                        + " and ".join(f'\"{m}\"' for m in DEFAULT_QODO_MARKERS)
                        + ". Override if your Qodo deployment writes a different "
                          "heading.")
    p.add_argument("--anonymize", nargs="?", const="all", default=None,
                   choices=["all", "users", "repos"], metavar="SCOPE",
                   help="Replace identifying data with stable pseudonyms. "
                        "SCOPE: 'users', 'repos', or omit to anonymize both. "
                        "Output filenames get an _anon suffix.")
    p.add_argument("--by", choices=["month", "week"], default=None, metavar="PERIOD",
                   help="Also emit a timeframe breakout of processed PRs and unique "
                        "users per PERIOD ('month' or 'week', bucketed by creation date).")
    p.add_argument("--output-dir", type=Path, default=REPORTS_DIR, metavar="DIR",
                   help="Directory to write CSVs into (default: reports/)")
    args = p.parse_args()

    orgs = list(dict.fromkeys(args.org))  # de-dupe, preserve order
    if args.repos and len(orgs) > 1:
        p.error("--repos is only valid with a single --org")
    if args.repos:
        args.repos = list(dict.fromkeys(args.repos))

    if args.chunk_days < 1:
        p.error(f"--chunk-days must be >= 1 (got {args.chunk_days})")

    until = args.until or date.today()
    since = args.since or (until - timedelta(days=args.days))
    if since > until:
        p.error(f"--since ({since}) is after --until ({until})")

    markers = list(dict.fromkeys(args.marker)) if args.marker else list(DEFAULT_QODO_MARKERS)

    validate_orgs(orgs, args.repos)
    check_token_scope()

    rows: List[dict] = []
    for org in orgs:
        print(f"\nOrg: {org}", file=sys.stderr)
        rows.extend(search_processed_prs(
            org, since, until, repos=args.repos, chunk_days=args.chunk_days,
            merged_only=args.merged_only, markers=markers))

    if args.anonymize:
        user_map, org_map, repo_map = build_anon_maps(rows, args.anonymize)
        rows = apply_anonymization(rows, user_map, org_map, repo_map)

    stem = _output_stem(orgs, since, until, args.anonymize)
    written = write_all(rows, stem, args.output_dir, by=args.by)

    by_user = aggregate_by_user(rows)
    print()
    print(f"Window (by PR creation date): {since} → {until}")
    print(f"Orgs:              {', '.join(orgs)}")
    print(f"Markers:           {', '.join(markers)}")
    print(f"Scope:             {'merged PRs only' if args.merged_only else 'all reviewed PRs (any state)'}")
    print(f"Processed PRs:     {len(rows)}")
    print(f"Unique users:      {len(by_user)}")
    if args.by:
        print(f"\nBy {args.by} (bucketed by creation date):")
        for row in aggregate_by_period(rows, args.by):
            print(f"  {row['period']:<10} {row['processed_prs']:>4} PRs   "
                  f"{row['unique_users']:>3} users")
    print("\nCSVs written:")
    for path in written:
        print(f"  {path}")

    if not rows:
        print("\n  WARNING: 0 matching PRs found — this is probably not what you "
              "want. Likely causes:\n"
              "    - wrong --org (typo) or no access to its private repos\n"
              "    - gh token missing the 'repo' scope (see any warning above)\n"
              "    - the window (--since / --until / --days) has no reviewed PRs\n"
              f"    - this Qodo deployment's review heading isn't one of "
              f"{', '.join(repr(m) for m in markers)} (override with --marker)",
              file=sys.stderr)


if __name__ == "__main__":
    main()
