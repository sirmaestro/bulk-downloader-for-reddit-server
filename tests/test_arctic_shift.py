#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bdfr.arctic_shift import ArcticShift, read_post_json, submission_id_from_link
from bdfr.exceptions import ArcticShiftError


def make_post(post_id: str, created_utc: int, url: str = None) -> dict:
    return {
        "id": post_id,
        "title": f"title {post_id}",
        "author": "someone",
        "subreddit": "test",
        "created_utc": created_utc,
        "url": url or f"https://i.redd.it/{post_id}.jpg",
    }


def make_response(data=None, status: int = 200, error: str = None, headers: dict = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.headers = headers or {}
    response.json.return_value = {"data": data, "error": error}
    return response


@pytest.fixture()
def arctic_shift() -> ArcticShift:
    client = ArcticShift()
    client.MIN_REQUEST_INTERVAL = 0
    client.session = MagicMock()
    return client


def test_submissions_fetch_pages_lazily(arctic_shift: ArcticShift):
    arctic_shift.session.get.side_effect = [
        make_response([make_post("c", 300), make_post("b", 200)]),
        make_response([make_post("b", 200), make_post("a", 100)]),
        make_response([]),
    ]
    generator = arctic_shift.submissions(author="someone")
    assert next(generator).id == "c"
    assert arctic_shift.session.get.call_count == 1
    assert [s.id for s in generator] == ["b", "a"]
    second_params = arctic_shift.session.get.call_args_list[1].kwargs["params"]
    assert second_params["before"] == 201
    assert second_params["limit"] == "auto"
    assert arctic_shift.request_count == 3


def test_submissions_stop_when_closed(arctic_shift: ArcticShift):
    arctic_shift.session.get.return_value = make_response([make_post("b", 200), make_post("a", 100)])
    for submission in arctic_shift.submissions(author="someone"):
        break
    assert arctic_shift.session.get.call_count == 1


def test_submissions_limit(arctic_shift: ArcticShift):
    arctic_shift.session.get.return_value = make_response([make_post("c", 300), make_post("b", 200)])
    assert [s.id for s in arctic_shift.submissions(subreddit="test", limit=2)] == ["c", "b"]
    assert arctic_shift.session.get.call_args.kwargs["params"]["limit"] == 2
    assert arctic_shift.session.get.call_count == 1


def test_short_fixed_page_ends_listing(arctic_shift: ArcticShift):
    arctic_shift.session.get.return_value = make_response([make_post("c", 300)])
    assert [s.id for s in arctic_shift.submissions(subreddit="test", limit=5)] == ["c"]
    assert arctic_shift.session.get.call_count == 1


@patch("bdfr.arctic_shift.time.sleep")
def test_retry_on_server_timeout(mock_sleep: MagicMock, arctic_shift: ArcticShift):
    arctic_shift.session.get.side_effect = [
        make_response(error="Timeout. Maybe slow down a bit"),
        make_response(status=429, headers={"x-ratelimit-reset": "5"}),
        make_response([make_post("a", 100)]),
    ]
    assert [s.id for s in arctic_shift.submissions(subreddit="test", limit=1)] == ["a"]
    assert [c.args[0] for c in mock_sleep.call_args_list] == [10, 6]


@patch("bdfr.arctic_shift.time.sleep")
def test_every_attempt_is_reported(mock_sleep: MagicMock, arctic_shift: ArcticShift):
    reported = []
    arctic_shift.on_request = reported.append
    arctic_shift.session.get.side_effect = [
        make_response(status=422, error="Timeout. Maybe slow down a bit"),
        make_response([make_post("a", 100)]),
        make_response([make_post("b", 100, "https://www.reddit.com/gallery/b")]),
    ]
    assert [s.id for s in arctic_shift.submissions(author="someone", limit=1)] == ["a"]
    gallery = {**make_post("b", 100, "https://www.reddit.com/gallery/b"), "bdfr_note": "removed"}
    assert [s.id for s in arctic_shift.submissions_from_posts([gallery])] == ["b"]
    assert [(r["endpoint"], r["attempt"], r["status"], r["error"], r["posts"]) for r in reported] == [
        ("posts/search", 1, 422, "Timeout. Maybe slow down a bit", None),
        ("posts/search", 2, 200, None, 1),
        ("posts/ids", 1, 200, None, 1),
    ]
    assert reported[0]["detail"] == "author=someone, limit=1"
    assert reported[2]["lookups"] == {"gallery (removed)": 1}


def test_bad_request_raises(arctic_shift: ArcticShift):
    arctic_shift.session.get.return_value = make_response(status=400, error="'limit' must be between 1 and 100")
    with pytest.raises(ArcticShiftError):
        list(arctic_shift.submissions(subreddit="test"))


def test_submission_does_not_fetch_from_reddit(arctic_shift: ArcticShift):
    submission = arctic_shift.to_submission(make_post("a", 100))
    assert submission.author == "someone"
    assert submission.subreddit == "test"
    assert submission.score is None
    with pytest.raises(AttributeError):
        _ = submission.gallery_data


def test_submissions_from_posts_only_looks_up_what_is_needed(arctic_shift: ArcticShift):
    gallery = make_post("g", 200, "https://www.reddit.com/gallery/g")
    full_gallery = dict(gallery, gallery_data={"items": []}, title="real title")
    arctic_shift.session.get.return_value = make_response([full_gallery])
    posts = [make_post("d", 100), gallery, {"id": "m"}]
    result = list(arctic_shift.submissions_from_posts(posts))
    assert [s.id for s in result] == ["g", "d"]
    assert result[0].title == "real title"
    assert arctic_shift.session.get.call_count == 1
    assert arctic_shift.session.get.call_args.kwargs["params"]["ids"] == "g,m"


def test_submissions_from_direct_posts_make_no_requests(arctic_shift: ArcticShift):
    assert [s.id for s in arctic_shift.submissions_from_posts([make_post("d", 100)])] == ["d"]
    arctic_shift.session.get.assert_not_called()


def test_submissions_from_posts_without_lookups_make_no_requests(arctic_shift: ArcticShift, caplog):
    caplog.set_level(logging.INFO)
    gallery = make_post("g", 200, "https://www.reddit.com/gallery/g")
    posts = [make_post("d", 100), gallery, {"id": "m"}]
    result = list(arctic_shift.submissions_from_posts(posts, lookups=False))
    assert [s.id for s in result] == ["g", "d"]  # the gallery as given; "m" has no data to download from
    arctic_shift.session.get.assert_not_called()
    assert arctic_shift.looked_up == {}
    assert "Not looking up 2 of 3 posts (archive lookups off):" in caplog.text
    assert "Submission m not found in the given data (archive lookups off), skipping" in caplog.text


@pytest.mark.parametrize(
    ("test_link", "expected"),
    (
        ("1xa496l", "1xa496l"),
        ("t3_1xa496l", "1xa496l"),
        ("https://www.reddit.com/r/test/comments/1xa496l/some_title/", "1xa496l"),
        ("https://www.reddit.com/gallery/1xa496l", "1xa496l"),
        ("https://redd.it/1xa496l", "1xa496l"),
        ("https://example.com/", None),
    ),
)
def test_submission_id_from_link(test_link: str, expected: str):
    assert submission_id_from_link(test_link) == expected


def test_read_post_json_formats(tmp_path: Path):
    exported = tmp_path / "export.json"
    exported.write_text(json.dumps({"format": "bdfr-posts", "posts": [make_post("a", 100), {"id": "bad id"}]}))
    listing = tmp_path / "listing.json"
    listing.write_text(json.dumps({"data": {"children": [{"kind": "t3", "data": {"name": "t3_b", "url": None}}]}}))
    plain = tmp_path / "plain.json"
    plain.write_text(json.dumps([{"id": "a", "score": 5}]))
    posts = {post["id"]: post for post in read_post_json([exported, listing, plain, tmp_path / "missing.json"])}
    assert set(posts) == {"a", "b"}
    assert posts["a"]["score"] == 5 and posts["a"]["title"] == "title a"
    assert "url" not in posts["b"]


def test_submissions_before(arctic_shift: ArcticShift):
    arctic_shift.session.get.side_effect = [
        make_response([make_post("b", 200), make_post("a", 100)]),
        make_response([]),
    ]
    assert [s.id for s in arctic_shift.submissions(author="someone", before=250)] == ["b", "a"]
    first, second = (call.kwargs["params"] for call in arctic_shift.session.get.call_args_list)
    assert first["before"] == 250
    assert second["before"] == 101
