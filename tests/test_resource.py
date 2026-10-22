#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from unittest.mock import MagicMock

import pytest
import requests

from bdfr.exceptions import BulkDownloaderException
from bdfr.resource import Resource


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


@pytest.mark.online
@pytest.mark.parametrize(
    ("test_url", "expected_hash"),
    (("https://www.iana.org/static/_img/2022/iana-logo-homepage.svg", "3914c6f1ab9dda5363d0225934ff90da"),),
)
def test_download_online_resource(test_url: str, expected_hash: str):
    test_resource = Resource(MagicMock(), test_url, Resource.retry_download(test_url))
    test_resource.download()
    assert test_resource.hash.hexdigest() == expected_hash


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
