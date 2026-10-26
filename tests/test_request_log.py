#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
from pathlib import Path

from bdfr.request_log import DAY, RequestLog

NOW = 1_790_000_000.0


def entry(age: float, purpose: str, status: int = 200, user: str = None, **fields) -> dict:
    return {
        "time": NOW - age,
        "purpose": purpose,
        "status": status,
        "user": user,
        "endpoint": "posts/search",
        "posts": 10 if status == 200 else None,
        **fields,
    }


def test_summary_counts_by_period_and_purpose(tmp_path: Path):
    log = RequestLog(tmp_path / "requests.jsonl", clock=lambda: NOW)
    log.record(entry(3 * DAY, "watchlist: whole archive", user="someone"))
    log.record(entry(3600, "watchlist: whole archive", status=422, user="someone", error="Timeout"))
    log.record(entry(60, "userscript posts: lookups", endpoint="posts/ids", lookups={"gallery (removed)": 2}))
    summary = log.summary()
    assert summary["totals"]["day"] == {"requests": 2, "failed": 1, "posts": 10}
    assert summary["totals"]["week"] == {"requests": 3, "failed": 1, "posts": 20}
    assert summary["purposes"][0] == {
        "purpose": "watchlist: whole archive",
        "day": {"requests": 1, "failed": 1, "posts": 0},
        "week": {"requests": 2, "failed": 1, "posts": 10},
        "month": {"requests": 2, "failed": 1, "posts": 10},
    }
    assert summary["lookups"] == [{"reason": "gallery (removed)", "day": 2, "week": 2, "month": 2}]
    assert summary["users"] == [{"user": "someone", "requests": 2, "failed": 1, "last": NOW - 3600}]
    assert summary["errors"] == [{"error": "Timeout", "count": 1}]
    assert [e["purpose"] for e in summary["recent"]][0] == "userscript posts: lookups"


def test_kept_across_restarts_for_keep_days(tmp_path: Path):
    path = tmp_path / "requests.jsonl"
    log = RequestLog(path, clock=lambda: NOW)
    log.record(entry(40 * DAY, "old"))
    log.record(entry(DAY, "recent"))
    with open(path, "a", encoding="utf-8") as file:
        file.write("not json\n")
    again = RequestLog(path, clock=lambda: NOW)
    assert [e["purpose"] for e in again.entries] == ["recent"]
    # Old and unreadable lines are dropped from the file too
    assert [json.loads(line)["purpose"] for line in path.read_text(encoding="utf-8").splitlines()] == ["recent"]
