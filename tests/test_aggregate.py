"""Unit tests for the pure aggregation + anonymization logic (no network)."""

import json
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qodo_usage_metrics as qum  # noqa: E402
from qodo_usage_metrics import (  # noqa: E402
    aggregate_by_user, aggregate_by_period, aggregate_by_user_period,
    period_key, build_anon_maps, apply_anonymization, search_processed_prs,
)


def _row(user, org, repo, number, created_at="", merged_at=""):
    return {
        "org": org, "repo": repo, "pr_number": number, "pr_url": "", "state": "open",
        "user": user, "created_at": created_at, "merged_at": merged_at,
    }


SAMPLE = [
    _row("alice", "acme", "frontend", 1),
    _row("alice", "acme", "frontend", 2),
    _row("bob", "acme", "backend", 3),
    _row("alice", "acme", "backend", 4),
    _row("carol", "widgets", "api", 5),
]


def test_by_user_counts_each_pr_once():
    counts = {r["user"]: r["processed_prs"] for r in aggregate_by_user(SAMPLE)}
    assert counts == {"alice": 3, "bob": 1, "carol": 1}


def test_by_user_sorted_desc_then_alpha():
    order = [r["user"] for r in aggregate_by_user(SAMPLE)]
    assert order == ["alice", "bob", "carol"]  # bob/carol tie -> alphabetical


def test_unique_user_count_is_row_count():
    assert len(aggregate_by_user(SAMPLE)) == 3


def test_duplicate_pr_rows_are_caller_responsibility():
    # aggregate_by_user() trusts its input is already de-duped by the searcher;
    # if the same PR appears twice it is counted twice. This guards the contract
    # so a future change to the searcher's dedup surfaces here.
    doubled = SAMPLE + [_row("alice", "acme", "frontend", 1)]
    counts = {r["user"]: r["processed_prs"] for r in aggregate_by_user(doubled)}
    assert counts["alice"] == 4


def test_anonymize_all_is_stable_and_hides_identities():
    user_map, org_map, repo_map = build_anon_maps(SAMPLE, "all")
    anon = apply_anonymization(SAMPLE, user_map, org_map, repo_map)
    # alice appears first -> user-01, and is applied consistently everywhere.
    assert user_map["alice"] == "user-01"
    assert all(r["user"].startswith("user-") for r in anon)
    assert all(r["org"].startswith("org-") for r in anon)
    assert all(r["repo"].startswith("repo-") for r in anon)
    assert all(r["pr_url"] == "" for r in anon)
    # Per-user counts are preserved under anonymization.
    assert aggregate_by_user(anon)[0]["processed_prs"] == 3


def test_anonymize_users_only_keeps_repos_visible():
    user_map, org_map, repo_map = build_anon_maps(SAMPLE, "users")
    anon = apply_anonymization(SAMPLE, user_map, org_map, repo_map)
    assert anon[0]["user"].startswith("user-")
    assert anon[0]["org"] == "acme"
    assert anon[0]["repo"] == "frontend"


# --- timeframe breakout ---------------------------------------------------- #

DATED = [
    _row("alice", "acme", "frontend", 1, created_at="2026-05-04T10:00:00Z"),  # Mon 2026-05-04
    _row("bob", "acme", "frontend", 2, created_at="2026-05-06T10:00:00Z"),    # Wed, same week
    _row("alice", "acme", "backend", 3, created_at="2026-06-30T23:59:59Z"),   # June
    _row("carol", "widgets", "api", 4, created_at=""),                        # unknown period
]


def test_period_key_month_and_week():
    assert period_key("2026-05-06T10:00:00Z", "month") == "2026-05"
    # Wednesday 2026-05-06 -> Monday of its ISO week is 2026-05-04
    assert period_key("2026-05-06T10:00:00Z", "week") == "2026-05-04"
    assert period_key("", "month") == "unknown"
    assert period_key("not-a-date", "week") == "unknown"


def test_aggregate_by_period_month_counts_and_unique_users():
    out = aggregate_by_period(DATED, "month")
    by_period = {r["period"]: r for r in out}
    assert by_period["2026-05"]["processed_prs"] == 2
    assert by_period["2026-05"]["unique_users"] == 2   # alice + bob
    assert by_period["2026-06"]["processed_prs"] == 1
    assert by_period["2026-06"]["unique_users"] == 1
    # chronological order, 'unknown' sorts last
    assert [r["period"] for r in out] == ["2026-05", "2026-06", "unknown"]


def test_aggregate_by_period_week_groups_same_week():
    out = aggregate_by_period(DATED, "week")
    by_period = {r["period"]: r for r in out}
    # alice(Mon) + bob(Wed) fall in the same ISO week -> one bucket of 2
    assert by_period["2026-05-04"]["processed_prs"] == 2
    assert by_period["2026-05-04"]["unique_users"] == 2


def test_aggregate_by_user_period():
    out = aggregate_by_user_period(DATED, "month")
    triples = {(r["period"], r["user"]): r["processed_prs"] for r in out}
    assert triples[("2026-05", "alice")] == 1
    assert triples[("2026-05", "bob")] == 1
    assert triples[("2026-06", "alice")] == 1
    assert triples[("unknown", "carol")] == 1


def test_unmerged_pr_counts_and_buckets_by_creation_date():
    # The whole point of the default mode: a PR Qodo reviewed but never merged
    # (no merge date) must still be counted, and bucketed by when it was opened
    # — not dropped, and not dumped into 'unknown'.
    rows = [_row("dave", "acme", "x", 9, created_at="2026-07-15T00:00:00Z", merged_at="")]
    assert aggregate_by_user(rows) == [{"user": "dave", "processed_prs": 1}]
    assert aggregate_by_period(rows, "month") == [
        {"period": "2026-07", "processed_prs": 1, "unique_users": 1}
    ]


# --- auto-subdivide on the 1000-result search cap -------------------------- #

_CREATED_RE = re.compile(r"created:(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})")


def _fake_gh_from_dataset(day_counts, monkeypatch):
    """Install a fake run_gh that serves PRs from `day_counts` (date -> N).

    Every query returns, in a single page, the PRs whose creation date falls in
    the query's `created:A..B` window, with the true total as issueCount — so
    the code under test sees the real cap signal and must split to stay whole.
    """
    prs = []
    n = 0
    for d in sorted(day_counts):
        for _ in range(day_counts[d]):
            n += 1
            prs.append({
                "number": n,
                "repository": {"nameWithOwner": "acme/repo"},
                "url": f"https://github.com/acme/repo/pull/{n}",
                "state": "MERGED",
                "author": {"login": "alice"},
                "createdAt": f"{d}T12:00:00Z",
                "mergedAt": f"{d}T13:00:00Z",
            })

    def fake_run_gh(args):
        q = next(a[2:] for a in args if a.startswith("q="))
        lo, hi = _CREATED_RE.search(q).groups()
        in_window = [p for p in prs if lo <= p["createdAt"][:10] <= hi]
        # Faithful to GitHub: issueCount is the true total, but no query can
        # return more than the cap — so a window over the cap comes back
        # TRUNCATED. Without this, a window served whole would let the code
        # recover every PR even if subdivision were broken, so the test would
        # pass for the wrong reason.
        return json.dumps({"data": {"search": {
            "issueCount": len(in_window),
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": in_window[:qum.SEARCH_RESULT_CAP],
        }}})

    monkeypatch.setattr(qum, "run_gh", fake_run_gh)
    return prs


def test_auto_subdivide_recovers_every_pr(monkeypatch):
    # Cap of 5, one big initial window. Multiple days push several windows over
    # the cap, forcing recursive splits. Every PR must come back exactly once.
    monkeypatch.setattr(qum, "SEARCH_RESULT_CAP", 5)
    day_counts = {
        "2026-01-01": 3, "2026-01-02": 1, "2026-01-04": 4,
        "2026-01-06": 4, "2026-01-08": 2,
    }
    all_prs = _fake_gh_from_dataset(day_counts, monkeypatch)
    got = list(search_processed_prs(
        "acme", since=date(2026, 1, 1), until=date(2026, 1, 8), chunk_days=30))
    assert len(got) == len(all_prs) == sum(day_counts.values())      # complete
    assert len({(r["org"], r["repo"], r["pr_number"]) for r in got}) == len(got)  # no dupes
    assert {r["created_at"][:10] for r in got} == set(day_counts)    # right days


def test_single_day_over_cap_warns_but_still_yields(monkeypatch, capsys):
    # A single day over the cap can't be split further: the tool must yield the
    # cap's worth (all GitHub will return) AND warn loudly, rather than silently
    # returning a short count as if it were complete.
    monkeypatch.setattr(qum, "SEARCH_RESULT_CAP", 5)
    _fake_gh_from_dataset({"2026-02-03": 9}, monkeypatch)
    got = list(search_processed_prs(
        "acme", since=date(2026, 2, 3), until=date(2026, 2, 3), chunk_days=30))
    assert len(got) == 5  # truncated to the cap — the irreducible undercount
    assert "can't be split further" in capsys.readouterr().err


def test_window_under_cap_is_not_split(monkeypatch, capsys):
    # Below the cap there should be no split line at all — just one search.
    monkeypatch.setattr(qum, "SEARCH_RESULT_CAP", 1000)
    _fake_gh_from_dataset({"2026-03-01": 3, "2026-03-02": 2}, monkeypatch)
    got = list(search_processed_prs(
        "acme", since=date(2026, 3, 1), until=date(2026, 3, 2), chunk_days=30))
    assert len(got) == 5
    assert "splitting" not in capsys.readouterr().err
