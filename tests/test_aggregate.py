"""Unit tests for the pure aggregation + anonymization logic (no network)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qodo_usage_metrics import (  # noqa: E402
    aggregate_by_user, build_anon_maps, apply_anonymization,
)


def _row(user, org, repo, number):
    return {
        "org": org, "repo": repo, "pr_number": number, "pr_url": "",
        "user": user, "created_at": "", "merged_at": "",
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
