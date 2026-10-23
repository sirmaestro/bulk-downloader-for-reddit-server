#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import email.utils
import hashlib
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest
import requests

from bdfr.exceptions import BulkDownloaderException, ContentGone
from bdfr.resource import TEMP_PREFIX, Resource


@pytest.mark.parametrize(
    ("test_url", "expected"),
    (
        ("test.png", ".png"),
        ("another.mp4", ".mp4"),
        ("test.jpeg", ".jpeg"),
        ("http://www.random.com/resource.png", ".png"),
        ("https://www.resource.com/test/example.jpg", ".jpg"),
        ("hard.png.mp4", ".mp4"),
        ("https://preview.redd.it/7zkmr1wqqih61.png?width=237&format=png&auto=webp&s=19de214e634cbcad99", ".png"),
        ("test.jpg#test", ".jpg"),
        ("test.jpg?width=247#test", ".jpg"),
        ("https://www.test.com/test/test2/example.png?random=test#thing", ".png"),
    ),
)
def test_resource_get_extension(test_url: str, expected: str):
    test_resource = Resource(MagicMock(), test_url, lambda: None)
    result = test_resource._determine_extension()
    assert result == expected


def fake_response(url: str, status: int = 200, content: bytes = b"data", redirected: bool = False) -> MagicMock:
    response = MagicMock()
    response.url = url
    response.status_code = status
    response.content = content
    response.history = [MagicMock()] if redirected else []
    return response


def test_download_sends_given_headers(monkeypatch: pytest.MonkeyPatch):
    get = MagicMock(return_value=fake_response("https://i.imgur.com/a.jpg"))
    monkeypatch.setattr(requests.Session, "get", get)
    download = Resource.retry_download("https://i.imgur.com/a.jpg", {"User-Agent": "bdfr/test"})
    assert download({"max_wait_time": 120}) == b"data"
    assert get.call_args.kwargs["headers"] == {"User-Agent": "bdfr/test"}


def test_download_of_removed_placeholder_fails(monkeypatch: pytest.MonkeyPatch):
    # Imgur redirects deleted images to a placeholder image, which must not be saved as the image
    get = MagicMock(return_value=fake_response("https://i.imgur.com/removed.png", redirected=True))
    monkeypatch.setattr(requests.Session, "get", get)
    with pytest.raises(BulkDownloaderException, match="Removed from the host"):
        Resource.http_download("https://i.imgur.com/a.jpg", {})


@pytest.mark.parametrize(("status", "gone"), ((404, True), (410, True), (403, False), (500, False), (503, False)))
def test_only_404_and_410_mean_the_media_is_gone(monkeypatch: pytest.MonkeyPatch, status: int, gone: bool):
    get = MagicMock(return_value=fake_response("https://i.redd.it/a.jpg", status=status, content=b""))
    monkeypatch.setattr(requests.Session, "get", get)
    with pytest.raises(BulkDownloaderException, match=f"HTTP Code {status}") as raised:
        Resource.http_download("https://i.redd.it/a.jpg", {})
    assert isinstance(raised.value, ContentGone) is gone


def answers(*responses: MagicMock) -> MagicMock:
    return MagicMock(side_effect=list(responses))


def rate_limited(retry_after: Optional[str] = None) -> MagicMock:
    response = fake_response("https://i.imgur.com/a.jpg", status=429, content=b"")
    response.headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return response


@pytest.mark.parametrize(
    ("parameters", "attempts", "sleeps"),
    (
        # With the default --max-wait-time: 60 s, then 120 s, then it gives up without waiting again
        ({"max_wait_time": 120}, 3, [60, 120]),
        ({"fail_fast": True}, 3, [5, 10]),
    ),
)
def test_rate_limited_download_waits_only_before_another_attempt(
    monkeypatch: pytest.MonkeyPatch, parameters: dict, attempts: int, sleeps: list[int]
):
    get = answers(*[rate_limited() for _ in range(attempts + 1)])
    sleep = MagicMock()
    monkeypatch.setattr(requests.Session, "get", get)
    monkeypatch.setattr(time, "sleep", sleep)
    with pytest.raises(requests.exceptions.ConnectionError, match="429"):
        Resource.http_download("https://i.imgur.com/a.jpg", parameters)
    assert get.call_count == attempts
    assert [c.args[0] for c in sleep.call_args_list] == sleeps


def test_rate_limited_download_waits_as_long_as_the_host_asks(monkeypatch: pytest.MonkeyPatch):
    get = answers(rate_limited("7"), fake_response("https://i.imgur.com/a.jpg"))
    sleep = MagicMock()
    monkeypatch.setattr(requests.Session, "get", get)
    monkeypatch.setattr(time, "sleep", sleep)
    assert Resource.http_download("https://i.imgur.com/a.jpg", {"max_wait_time": 120}) == b"data"
    assert [c.args[0] for c in sleep.call_args_list] == [7]


def test_rate_limited_download_gives_up_when_the_host_asks_for_too_long(monkeypatch: pytest.MonkeyPatch):
    later = email.utils.format_datetime(datetime.now(timezone.utc) + timedelta(hours=1), usegmt=True)
    get = answers(rate_limited(later))
    sleep = MagicMock()
    monkeypatch.setattr(requests.Session, "get", get)
    monkeypatch.setattr(time, "sleep", sleep)
    with pytest.raises(requests.exceptions.ConnectionError):
        Resource.http_download("https://i.imgur.com/a.jpg", {"max_wait_time": 120})
    assert get.call_count == 1
    sleep.assert_not_called()


def streamed_response(chunks: list, status: int = 200) -> MagicMock:
    """A streamed response giving chunks; an exception among them is raised when reached"""
    response = MagicMock(url="https://i.redd.it/a.jpg", status_code=status, history=[])

    def iter_content(_size):
        for chunk in chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    response.iter_content = iter_content
    response.__enter__.return_value = response
    return response


def temp_files(directory: Path) -> list[Path]:
    return list(directory.glob(f"{TEMP_PREFIX}*"))


def test_download_streams_to_a_temporary_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    get = MagicMock(return_value=streamed_response([b"ab", b"cd"]))
    monkeypatch.setattr(requests.Session, "get", get)
    resource = Resource(MagicMock(), "https://i.redd.it/a.jpg", Resource.retry_download("https://i.redd.it/a.jpg"))
    resource.download({"temp_dir": tmp_path})
    assert get.call_args.kwargs["stream"] is True
    assert resource.content is None
    assert resource.hash.hexdigest() == hashlib.md5(b"abcd").hexdigest()
    assert [p.read_bytes() for p in temp_files(tmp_path)] == [b"abcd"]
    resource.save(tmp_path / "a.jpg")
    assert (tmp_path / "a.jpg").read_bytes() == b"abcd" and temp_files(tmp_path) == []


def test_discarded_download_leaves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(requests.Session, "get", MagicMock(return_value=streamed_response([b"abcd"])))
    resource = Resource(MagicMock(), "https://i.redd.it/a.jpg", Resource.retry_download("https://i.redd.it/a.jpg"))
    resource.download({"temp_dir": tmp_path})
    resource.discard()
    assert list(tmp_path.iterdir()) == []


def test_broken_stream_is_retried_without_leftovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    broken = streamed_response([b"ab", requests.exceptions.ChunkedEncodingError("cut off")])
    monkeypatch.setattr(requests.Session, "get", MagicMock(side_effect=[broken, streamed_response([b"abcd"])]))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    downloaded = Resource.http_download("https://i.redd.it/a.jpg", {"temp_dir": tmp_path})
    assert [p.read_bytes() for p in temp_files(tmp_path)] == [b"abcd"] and downloaded.path.exists()


def test_empty_stream_fails_without_leftovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(requests.Session, "get", MagicMock(return_value=streamed_response([])))
    with pytest.raises(BulkDownloaderException, match="no content"):
        Resource.http_download("https://i.redd.it/a.jpg", {"temp_dir": tmp_path})
    assert temp_files(tmp_path) == []
