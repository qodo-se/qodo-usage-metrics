"""Unit tests for the pure aggregation + anonymization logic (no network)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qodo_usage_metrics import (  # noqa: E402
    aggregate_by_user, aggregate_by_period, aggregate_by_user_period,
    period_key, build_anon_maps, apply_anonymization,
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
