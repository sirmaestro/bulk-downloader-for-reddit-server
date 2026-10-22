#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import urllib.request
from unittest.mock import MagicMock, Mock

import pytest
import requests

from bdfr.configuration import Configuration
from bdfr.exceptions import ResourceNotFound, SiteDownloaderError
from bdfr.site_downloaders.imgur import Imgur


@pytest.mark.parametrize(
    ("test_url", "api_data"),
    (
        ("https://i.imgur.com/4aLOj0L.jpg", None),
        ("https://imgur.com/4aLOj0L", {"link": "https://i.imgur.com/4aLOj0L.jpg"}),
        ("https://imgur.com/a/xWZsDDP", {"is_album": True, "images": [{"link": "https://i.imgur.com/ypa8YfS.png"}]}),
    ),
)
def test_requests_send_user_agent(test_url: str, api_data: dict, monkeypatch: pytest.MonkeyPatch):
    # Imgur answers python-requests' default User-Agent with 429 Too Many Requests
    def retrieve_url(url, cookies=None, headers=None):
        assert headers["User-Agent"].startswith("bdfr/")
        return Mock(text=json.dumps({"data": api_data}))

    get = Mock(return_value=Mock(url="", history=[], status_code=200, content=b"image"))
    monkeypatch.setattr(Imgur, "retrieve_url", staticmethod(retrieve_url))
    monkeypatch.setattr(requests.Session, "get", get)
    mock_submission = Mock()
    mock_submission.url = test_url
    config = Configuration()
    config.imgur_originals = True
    for res in Imgur(mock_submission, config).find_resources():
        res.download()
    assert get.call_args.kwargs["headers"]["User-Agent"].startswith("bdfr/")


@pytest.mark.parametrize(
    ("test_url", "api_url"),
    (
        ("https://imgur.com/a/xWZsDDP", "https://api.imgur.com/3/album/xWZsDDP"),
        ("https://imgur.com/gallery/IjJJdlC", "https://api.imgur.com/3/album/IjJJdlC"),
        ("https://imgur.com/gallery/IjJJdlC/", "https://api.imgur.com/3/album/IjJJdlC"),
        ("https://imgur.com/a/1qzfWtY/gifv", "https://api.imgur.com/3/album/1qzfWtY"),
        ("https://i.imgur.com/lFJai6i.gifv", "https://api.imgur.com/3/image/lFJai6i"),
        ("https://imgur.com/ubYwpbk.GIFV", "https://api.imgur.com/3/image/ubYwpbk"),
        ("https://i.imgur.com/2TtN68l_d.webp", "https://api.imgur.com/3/image/2TtN68l"),
        ("https://i.imgur.com/cbivYRW.jpg?3", "https://api.imgur.com/3/image/cbivYRW"),
    ),
)
def test_links_are_looked_up_by_id(test_url: str, api_url: str, monkeypatch: pytest.MonkeyPatch):
    asked = []

    def retrieve_url(url, cookies=None, headers=None):
        asked.append(url)
        return Mock(text=json.dumps({"data": {"link": "https://i.imgur.com/x.jpg"}}))

    monkeypatch.setattr(Imgur, "retrieve_url", staticmethod(retrieve_url))
    Imgur(Mock(url=test_url), Configuration()).find_resources()
    assert asked == [api_url]


@pytest.mark.parametrize(
    ("test_url", "expected"),
    (
        ("https://i.imgur.com/3SKrQfK.jpg?1", "https://i.imgur.com/3SKrQfK.jpg?1"),
        ("http://i.imgur.com/s9uXxlq.jpg", "https://i.imgur.com/s9uXxlq.jpg"),
    ),
)
def test_direct_image_links_need_no_lookup(test_url: str, expected: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(Imgur, "retrieve_url", staticmethod(Mock(side_effect=AssertionError("looked up"))))
    assert [res.url for res in Imgur(Mock(url=test_url), Configuration()).find_resources()] == [expected]


@pytest.mark.parametrize(
    ("originals", "expected"),
    (
        (False, ["https://i.imgur.com/a.mp4", "https://i.imgur.com/b.png"]),
        (True, ["https://i.imgur.com/a.gif", "https://i.imgur.com/b.png"]),
    ),
)
def test_album_takes_mp4_unless_originals(originals: bool, expected: list[str], monkeypatch: pytest.MonkeyPatch):
    album = {
        "is_album": True,
        "images": [
            {"link": "https://i.imgur.com/a.gif", "mp4": "https://i.imgur.com/a.mp4"},
            {"link": "https://i.imgur.com/b.png"},
        ],
    }
    monkeypatch.setattr(
        Imgur, "retrieve_url", staticmethod(lambda url, headers=None: Mock(text=json.dumps({"data": album})))
    )
    config = Configuration()
    config.imgur_originals = originals
    assert [res.url for res in Imgur(Mock(url="https://imgur.com/a/xWZsDDP"), config).find_resources()] == expected


@pytest.mark.parametrize(
    ("test_url", "originals", "magic", "expected"),
    (
        ("https://i.imgur.com/lFJai6i.gifv", False, b"GIF", "https://i.imgur.com/lFJai6i.mp4"),
        ("https://i.imgur.com/lFJai6i.gifv", True, b"GIF", "https://i.imgur.com/lFJai6i.gif"),
        # The .gif is really a JPEG: the mp4 is used even with --imgur-originals
        ("https://i.imgur.com/lFJai6i.gifv", True, b"\xff\xd8\xff", "https://i.imgur.com/lFJai6i.mp4"),
        ("https://i.imgur.com/lFJai6i.gif", True, None, "https://i.imgur.com/lFJai6i.gif"),
    ),
)
def test_fix404_downloads_gifs_the_api_lost(
    test_url: str, originals: bool, magic, expected: str, monkeypatch: pytest.MonkeyPatch
):
    def not_found(url, cookies=None, headers=None):
        raise ResourceNotFound(f"Server responded with 404 to {url}")

    urlopen = MagicMock()
    urlopen.return_value.__enter__.return_value.read.return_value = magic
    monkeypatch.setattr(Imgur, "retrieve_url", staticmethod(not_found))
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    config = Configuration()
    config.imgur_fix404 = True
    config.imgur_originals = originals
    assert [res.url for res in Imgur(Mock(url=test_url), config).find_resources()] == [expected]
    # Without --imgur-fix404 the post fails
    config.imgur_fix404 = False
    with pytest.raises(SiteDownloaderError, match="404"):
        Imgur(Mock(url=test_url), config).find_resources()
