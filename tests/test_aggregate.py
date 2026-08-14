"""Unit tests for the pure aggregation + anonymization logic (no network).

The tool reports one thing — the distinct active users — so these tests assert
on that set and its per-period breakouts, and that no per-PR review count leaks
into any output.
"""

import json
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qodo_usage_metrics as qum  # noqa: E402
from qodo_usage_metrics import (  # noqa: E402
    active_users, active_users_by_period, active_users_per_period,
    period_key, anonymize_users, search_reviewed_prs,
)


def _row(user, created_at=""):
    return {"user": user, "created_at": created_at}


SAMPLE = [
    _row("alice"),
    _row("alice"),
    _row("bob"),
    _row("alice"),
    _row("carol"),
]


def test_active_users_are_distinct():
    # alice's three reviewed PRs collapse to a single active-user entry.
    assert active_users(SAMPLE) == ["alice", "bob", "carol"]


def test_active_users_sorted_alphabetically():
    assert active_users(SAMPLE) == sorted(active_users(SAMPLE))


def test_active_user_count_is_len_of_set():
    assert len(active_users(SAMPLE)) == 3


def test_duplicate_rows_collapse_to_one_user():
    # Even if the same PR's author appears many times, the user is counted once —
    # active users are a set, not a PR tally.
    doubled = SAMPLE + [_row("alice"), _row("alice")]
    assert active_users(doubled) == ["alice", "bob", "carol"]


def test_anonymize_is_stable_and_hides_identities():
    anon, user_map = anonymize_users(SAMPLE)
    # alice appears first -> user-01, applied consistently everywhere.
    assert user_map["alice"] == "user-01"
    assert all(r["user"].startswith("user-") for r in anon)
    # The active-user count is preserved under anonymization.
    assert len(active_users(anon)) == len(active_users(SAMPLE)) == 3
    assert active_users(anon) == ["user-01", "user-02", "user-03"]


# --- timeframe breakout ---------------------------------------------------- #

DATED = [
    _row("alice", created_at="2026-05-04T10:00:00Z"),  # Mon 2026-05-04
    _row("bob", created_at="2026-05-06T10:00:00Z"),    # Wed, same week
    _row("alice", created_at="2026-06-30T23:59:59Z"),  # June
    _row("carol", created_at=""),                      # unknown period
]


def test_period_key_month_and_week():
    assert period_key("2026-05-06T10:00:00Z", "month") == "2026-05"
    # Wednesday 2026-05-06 -> Monday of its ISO week is 2026-05-04
    assert period_key("2026-05-06T10:00:00Z", "week") == "2026-05-04"
    assert period_key("", "month") == "unknown"
    assert period_key("not-a-date", "week") == "unknown"


def test_active_users_by_period_month():
    out = active_users_by_period(DATED, "month")
    by_period = {r["period"]: r["active_users"] for r in out}
    assert by_period["2026-05"] == 2   # alice + bob
    assert by_period["2026-06"] == 1
    assert by_period["unknown"] == 1
    # chronological order, 'unknown' sorts last
    assert [r["period"] for r in out] == ["2026-05", "2026-06", "unknown"]
    # only period + active_users columns — no PR count leaks in
    assert all(set(r) == {"period", "active_users"} for r in out)


def test_active_users_by_period_week_groups_same_week():
    out = active_users_by_period(DATED, "week")
    by_period = {r["period"]: r["active_users"] for r in out}
    # alice(Mon) + bob(Wed) fall in the same ISO week -> one bucket of 2 users
    assert by_period["2026-05-04"] == 2


def test_active_users_per_period_lists_distinct_users():
    out = active_users_per_period(DATED, "month")
    pairs = {(r["period"], r["user"]) for r in out}
    assert ("2026-05", "alice") in pairs
    assert ("2026-05", "bob") in pairs
    assert ("2026-06", "alice") in pairs
    assert ("unknown", "carol") in pairs
    # rows are just (period, user) — no review counts
    assert all(set(r) == {"period", "user"} for r in out)


def test_unmerged_pr_makes_its_author_active_and_buckets_by_creation_date():
    # The whole point of the default mode: a PR Qodo reviewed but never merged
    # (no merge date) must still make its author active, bucketed by when it was
    # opened — not dropped, and not dumped into 'unknown'.
    rows = [_row("dave", created_at="2026-07-15T00:00:00Z")]
    assert active_users(rows) == ["dave"]
    assert active_users_by_period(rows, "month") == [
        {"period": "2026-07", "active_users": 1}
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
                "author": {"login": "alice"},
                "createdAt": f"{d}T12:00:00Z",
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
    # the cap, forcing recursive splits. Every PR must come back exactly once so
    # the derived active-user evidence is complete.
    monkeypatch.setattr(qum, "SEARCH_RESULT_CAP", 5)
    day_counts = {
        "2026-01-01": 3, "2026-01-02": 1, "2026-01-04": 4,
        "2026-01-06": 4, "2026-01-08": 2,
    }
    all_prs = _fake_gh_from_dataset(day_counts, monkeypatch)
    got = list(search_reviewed_prs(
        "acme", since=date(2026, 1, 1), until=date(2026, 1, 8), chunk_days=30))
    assert len(got) == len(all_prs) == sum(day_counts.values())      # complete
    assert {r["created_at"][:10] for r in got} == set(day_counts)    # right days


def test_single_day_over_cap_warns_but_still_yields(monkeypatch, capsys):
    # A single day over the cap can't be split further: the tool must yield the
    # cap's worth (all GitHub will return) AND warn loudly, rather than silently
    # returning a short count as if it were complete.
    monkeypatch.setattr(qum, "SEARCH_RESULT_CAP", 5)
    _fake_gh_from_dataset({"2026-02-03": 9}, monkeypatch)
    got = list(search_reviewed_prs(
        "acme", since=date(2026, 2, 3), until=date(2026, 2, 3), chunk_days=30))
    assert len(got) == 5  # truncated to the cap — the irreducible undercount
    assert "can't be split further" in capsys.readouterr().err


def test_window_under_cap_is_not_split(monkeypatch, capsys):
    # Below the cap there should be no split line at all — just one search.
    monkeypatch.setattr(qum, "SEARCH_RESULT_CAP", 1000)
    _fake_gh_from_dataset({"2026-03-01": 3, "2026-03-02": 2}, monkeypatch)
    got = list(search_reviewed_prs(
        "acme", since=date(2026, 3, 1), until=date(2026, 3, 2), chunk_days=30))
    assert len(got) == 5
    assert "splitting" not in capsys.readouterr().err
