"""Unit tests for the gh transport (rate-limit handling) and marker query build.

No real network or subprocess: `subprocess.run` and `time.sleep` are faked, and
the GraphQL search is stubbed, so these exercise the retry/backoff control flow
and the search-query construction deterministically.
"""

import json
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qodo_usage_metrics as qum  # noqa: E402
from qodo_usage_metrics import (  # noqa: E402
    classify_rate_limit, markers_qualifier, search_reviewed_prs,
)


# --- rate-limit classification --------------------------------------------- #

def test_classify_secondary_beats_primary():
    # A secondary-limit message also contains "rate limit"; it must classify as
    # secondary (backoff), not primary (wait-to-reset).
    assert classify_rate_limit(
        "You have exceeded a secondary rate limit. Please wait...") == "secondary"
    assert classify_rate_limit(
        "was submitted too quickly. Please retry your request again later.") == "secondary"


def test_classify_primary_and_none():
    assert classify_rate_limit("API rate limit exceeded for user ID 123") == "primary"
    assert classify_rate_limit("HTTP 502 Bad Gateway") is None
    assert classify_rate_limit("") is None


# --- run_gh retry control flow --------------------------------------------- #

class _Result:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _install_fake_gh(monkeypatch, results, reset_epoch=None):
    """Fake subprocess.run to return `results` in order; record sleep durations.

    Returns the list that captures each time.sleep() duration so a test can
    assert on the backoff schedule. Pre-request spacing is disabled so only the
    retry/backoff sleeps are recorded.
    """
    monkeypatch.setattr(qum, "_REQUEST_SPACING_S", 0)
    seq = iter(results)
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        try:
            return next(seq)
        except StopIteration:  # pragma: no cover - defensive
            raise AssertionError("run_gh retried more times than the test expected")

    slept = []
    monkeypatch.setattr(qum.subprocess, "run", fake_run)
    monkeypatch.setattr(qum.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(qum, "_rate_limit_reset_epoch", lambda resource="graphql": reset_epoch)
    return slept, calls


def test_run_gh_returns_stdout_on_success(monkeypatch):
    _install_fake_gh(monkeypatch, [_Result(0, stdout="ok")])
    assert qum.run_gh(["api", "x"]) == "ok"


def test_run_gh_secondary_backs_off_from_60s_floor(monkeypatch):
    # Two secondary limits then success. Backoff must start at the 60s floor and
    # double — NOT be capped at a minute.
    slept, _ = _install_fake_gh(monkeypatch, [
        _Result(1, stderr="You have exceeded a secondary rate limit."),
        _Result(1, stderr="You have exceeded a secondary rate limit."),
        _Result(0, stdout="done"),
    ])
    assert qum.run_gh(["api", "graphql"]) == "done"
    assert slept == [60, 120]


def test_run_gh_secondary_backoff_is_capped(monkeypatch):
    # Enough secondary hits that the doubling would exceed the per-wait cap; each
    # wait must be clamped to _SECONDARY_BACKOFF_CAP_S.
    fails = [_Result(1, stderr="secondary rate limit")] * qum._MAX_SECONDARY_RETRIES
    slept, _ = _install_fake_gh(monkeypatch, fails + [_Result(0, stdout="done")])
    assert qum.run_gh(["api", "graphql"]) == "done"
    assert max(slept) <= qum._SECONDARY_BACKOFF_CAP_S
    assert slept[0] == 60


def test_run_gh_secondary_gives_up_loudly(monkeypatch):
    fails = [_Result(1, stderr="secondary rate limit")] * (qum._MAX_SECONDARY_RETRIES + 1)
    _install_fake_gh(monkeypatch, fails)
    with pytest.raises(SystemExit):
        qum.run_gh(["api", "graphql"])


def test_run_gh_primary_waits_until_reset(monkeypatch):
    # Primary limit: wait to the GraphQL bucket reset (here ~30s out), then retry.
    now = int(__import__("time").time())
    slept, _ = _install_fake_gh(monkeypatch, [
        _Result(1, stderr="API rate limit exceeded"),
        _Result(0, stdout="done"),
    ], reset_epoch=now + 30)
    assert qum.run_gh(["api", "graphql"]) == "done"
    assert len(slept) == 1 and 30 <= slept[0] <= 40  # reset delta + small buffer


def test_run_gh_primary_exits_when_reset_far_away(monkeypatch):
    # A reset beyond the cap must exit loudly rather than sleep in silence.
    now = int(__import__("time").time())
    _install_fake_gh(monkeypatch, [
        _Result(1, stderr="API rate limit exceeded"),
    ], reset_epoch=now + qum._PRIMARY_WAIT_CAP_S + 500)
    with pytest.raises(SystemExit):
        qum.run_gh(["api", "graphql"])


def test_run_gh_hard_failure_exits_immediately(monkeypatch):
    slept, calls = _install_fake_gh(monkeypatch, [
        _Result(1, stderr="something is broken and not retryable"),
    ])
    with pytest.raises(SystemExit):
        qum.run_gh(["api", "graphql"])
    assert calls["n"] == 1 and slept == []  # no retries, no sleeps


# --- marker query construction --------------------------------------------- #

def test_markers_qualifier_single_and_multi():
    assert markers_qualifier(["Code Review by Qodo"]) == '"Code Review by Qodo" in:comments'
    assert markers_qualifier(["A", "B"]) == '("A" OR "B") in:comments'


def test_markers_qualifier_rejects_quote_to_protect_the_count():
    # A quote (or newline) in a marker would break out of the search phrase and
    # silently change which PRs match — an over/undercount. It must fail loudly.
    with pytest.raises(qum.InvalidMarker):
        markers_qualifier(['Guide" OR "bug'])
    with pytest.raises(qum.InvalidMarker):
        markers_qualifier(["line1\nline2"])


def test_search_rejects_bad_marker_before_any_query(monkeypatch):
    # The guard must trip before any gh call is made, so a corrupt query is never
    # sent to GitHub in the first place.
    def boom(args):  # pragma: no cover - must never run
        raise AssertionError("run_gh should not be called with an invalid marker")

    monkeypatch.setattr(qum, "run_gh", boom)
    with pytest.raises(qum.InvalidMarker):
        list(search_reviewed_prs("acme", since=date(2026, 1, 1), until=date(2026, 1, 1),
                                  markers=['bad"marker']))


def test_search_unions_all_markers_in_one_query(monkeypatch):
    captured = {}

    def fake_run_gh(args):
        captured["q"] = next(a[2:] for a in args if a.startswith("q="))
        return json.dumps({"data": {"search": {
            "issueCount": 0,
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [],
        }}})

    monkeypatch.setattr(qum, "run_gh", fake_run_gh)
    list(search_reviewed_prs("acme", since=date(2026, 1, 1), until=date(2026, 1, 1),
                              markers=["Foo Marker", "Bar Marker"]))
    assert '("Foo Marker" OR "Bar Marker") in:comments' in captured["q"]


def test_search_defaults_to_both_qodo_markers(monkeypatch):
    captured = {}

    def fake_run_gh(args):
        captured["q"] = next(a[2:] for a in args if a.startswith("q="))
        return json.dumps({"data": {"search": {
            "issueCount": 0,
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [],
        }}})

    monkeypatch.setattr(qum, "run_gh", fake_run_gh)
    list(search_reviewed_prs("acme", since=date(2026, 1, 1), until=date(2026, 1, 1)))
    for marker in qum.DEFAULT_QODO_MARKERS:
        assert f'"{marker}"' in captured["q"]
