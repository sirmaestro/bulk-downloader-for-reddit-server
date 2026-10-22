#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bdfr.retries import HOUR, RetryList


class Clock:
    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def retries(tmp_path: Path) -> RetryList:
    return RetryList(MagicMock(), tmp_path / "failed_posts.json", Clock())


def failed(post_id: str) -> tuple[dict, str]:
    return {"id": post_id, "url": f"https://i.example/{post_id}.jpg"}, "could not download: HTTP 503"


def test_failed_posts_are_kept_until_a_job_gets_them(retries: RetryList, tmp_path: Path):
    retries.note_job("someone", {"a": "failed", "b": "new"}, {"a": failed("a")})
    entry = retries.entries["someone/a"]
    assert entry["attempts"] == 1 and entry["next_try"] == retries.clock.now + HOUR
    assert entry["post"]["id"] == "a" and "503" in entry["error"]
    # Kept across restarts
    assert RetryList(MagicMock(), tmp_path / "failed_posts.json").entries == retries.entries
    # The same post in another folder is another entry
    retries.note_job("other", {"a": "existing"}, {})
    assert "someone/a" in retries.entries
    retries.note_job("someone", {"a": "existing"}, {})
    assert retries.entries == {}
    assert json.loads((tmp_path / "failed_posts.json").read_text(encoding="utf-8")) == {"posts": {}}


def test_due_posts_are_queued_once_per_folder(retries: RetryList):
    retries.note_job("someone", {"a": "failed", "b": "failed"}, {"a": failed("a"), "b": failed("b")})
    retries.note_job("other", {"c": "failed"}, {"c": failed("c")})
    retries.tick()
    retries.server.submit.assert_not_called()

    retries.clock.now += HOUR
    retries.tick()
    calls = retries.server.submit.call_args_list
    assert sorted((c.args[0]["folder"], [p["id"] for p in c.args[0]["posts"]]) for c in calls) == [
        ("other", ["c"]),
        ("someone", ["a", "b"]),
    ]
    assert all(c.kwargs["scheduled"] and c.kwargs["retry"] for c in calls)
    # Not queued again while their job is waiting or running
    retries.tick()
    assert len(retries.server.submit.call_args_list) == 2

    # The job finishes: a downloaded, b failed again and waits longer
    retries.note_job("someone", {"a": "new", "b": "failed"}, {"b": failed("b")})
    for c in calls:
        c.kwargs["on_finish"](None)
    assert list(retries.entries) == ["someone/b", "other/c"]
    assert retries.entries["someone/b"]["attempts"] == 2
    assert retries.entries["someone/b"]["next_try"] == retries.clock.now + 6 * HOUR


def test_gives_up_after_the_last_retry(retries: RetryList):
    for _ in RetryList.BACKOFF_HOURS:
        retries.note_job("someone", {"a": "failed"}, {"a": failed("a")})
    assert retries.snapshot() == {"waiting": 1, "given_up": 0}
    retries.note_job("someone", {"a": "failed"}, {"a": failed("a")})
    assert retries.entries["someone/a"]["next_try"] is None
    assert retries.snapshot() == {"waiting": 0, "given_up": 1}
    retries.clock.now += 365 * 24 * HOUR
    retries.tick()
    retries.server.submit.assert_not_called()
    # Still dropped once a job downloads it after all
    retries.note_job("someone", {"a": "new"}, {})
    assert retries.snapshot() == {"waiting": 0, "given_up": 0}


def test_unreadable_list_starts_empty(tmp_path: Path):
    path = tmp_path / "failed_posts.json"
    path.write_text("{not json", encoding="utf-8")
    assert RetryList(MagicMock(), path).entries == {}


def test_posts_a_retry_job_did_not_report_wait_for_the_next_try(retries: RetryList):
    retries.note_job("someone", {"a": "failed"}, {"a": failed("a")})
    retries.clock.now += HOUR
    retries.tick()
    # The job stopped before reaching the post
    retries.server.submit.call_args.kwargs["on_finish"](None)
    assert retries.entries["someone/a"]["attempts"] == 2
    retries.tick()
    assert retries.server.submit.call_count == 1


def test_giving_up_is_notified(retries: RetryList):
    for _ in range(len(RetryList.BACKOFF_HOURS) + 1):
        retries.note_job("someone", {"a": "failed"}, {"a": failed("a")})
    retries.server.notify.assert_called_once()
    assert "Giving up on post a in someone" in retries.server.notify.call_args.args[0]
