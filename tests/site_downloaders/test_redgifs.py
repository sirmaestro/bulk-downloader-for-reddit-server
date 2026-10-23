#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
from unittest.mock import MagicMock, Mock, PropertyMock

import pytest
import requests

from bdfr.site_downloaders.redgifs import Redgifs


@pytest.mark.parametrize(
    ("test_url", "expected"),
    (
        ("https://redgifs.com/watch/frighteningvictorioussalamander", "frighteningvictorioussalamander"),
        ("https://www.redgifs.com/watch/genuineprivateguillemot/", "genuineprivateguillemot"),
        ("https://www.redgifs.com/watch/marriedcrushingcob?rel=u%3Akokiri.girl%3Bo%3Arecent", "marriedcrushingcob"),
        ("https://thumbs4.redgifs.com/DismalIgnorantDrongo.mp4", "dismalignorantdrongo"),
        ("https://thumbs4.redgifs.com/DismalIgnorantDrongo-mobile.mp4", "dismalignorantdrongo"),
        ("https://v3.redgifs.com/watch/newilliteratemeerkat#rel=user%3Atastynova", "newilliteratemeerkat"),
    ),
)
def test_get_id(test_url: str, expected: str):
    result = Redgifs._get_id(test_url)
    assert result == expected


@pytest.mark.parametrize(
    ("urls", "expected"),
    (
        # Some videos only have an SD version, without an "hd" key
        ({"sd": "https://media.redgifs.com/A-mobile.mp4"}, {"https://media.redgifs.com/A-mobile.mp4"}),
        (
            {"sd": "https://media.redgifs.com/A-mobile.mp4", "hd": "https://media.redgifs.com/A.mp4"},
            {"https://media.redgifs.com/A.mp4"},
        ),
    ),
)
def test_get_link_without_hd(urls: dict, expected: set[str], monkeypatch: pytest.MonkeyPatch):
    gif = {"gif": {"type": 1, "urls": urls}}
    monkeypatch.setattr(Redgifs, "_get_token", classmethod(lambda cls, renew=False: "token"))
    monkeypatch.setattr(Redgifs, "retrieve_url", staticmethod(lambda url, headers=None: Mock(text=json.dumps(gif))))
    monkeypatch.setattr(Redgifs, "_url_is_accessible", staticmethod(lambda url, headers, timeout: True))
    assert Redgifs._get_link("https://www.redgifs.com/watch/lumberinghomelynurseshark") == expected


@pytest.mark.parametrize(
    ("status", "accessible", "body_read"),
    (
        (206, True, True),  # the range was honoured: its byte is read, so the connection can be reused
        (200, True, False),  # the range was ignored: the whole video isn't read just to check the link
        (404, False, False),
    ),
)
def test_url_is_accessible_asks_for_one_byte(
    status: int, accessible: bool, body_read: bool, monkeypatch: pytest.MonkeyPatch
):
    response = MagicMock(status_code=status, ok=status < 400)
    response.__enter__.return_value = response
    content = PropertyMock(return_value=b"x")
    type(response).content = content
    get = Mock(return_value=response)
    monkeypatch.setattr(requests.Session, "get", get)
    assert Redgifs._url_is_accessible("https://media.redgifs.com/A.mp4", {"referer": "x"}, 10) is accessible
    assert get.call_args.kwargs["headers"] == {"referer": "x", "Range": "bytes=0-0"}
    assert get.call_args.kwargs["stream"] is True
    assert content.called is body_read


@pytest.mark.parametrize(
    ("gif", "gallery", "expected"),
    (
        # An image, with or without an HD version
        (
            {"type": 2, "gallery": None, "urls": {"hd": "https://media.redgifs.com/A-large.jpg"}},
            None,
            {"https://media.redgifs.com/A-large.jpg"},
        ),
        (
            {"type": 2, "gallery": None, "urls": {"sd": "https://media.redgifs.com/A-mobile.jpg"}},
            None,
            {"https://media.redgifs.com/A-mobile.jpg"},
        ),
        # A gallery: every image's HD version, with old thumbs2/thumbs3 hosts moved to thumbs4
        (
            {"type": 2, "gallery": "g1", "urls": {}},
            {
                "gifs": [
                    {"urls": {"hd": "https://thumbs2.redgifs.com/A-large.jpg"}},
                    {"urls": {"hd": "https://thumbs3.redgifs.com/B-large.jpg"}},
                ]
            },
            {"https://thumbs4.redgifs.com/A-large.jpg", "https://thumbs4.redgifs.com/B-large.jpg"},
        ),
    ),
)
def test_get_link_images(gif: dict, gallery, expected: set[str], monkeypatch: pytest.MonkeyPatch):
    def retrieve_url(url, headers=None):
        # Every API request carries the token, galleries' too
        assert headers and headers["Authorization"] == "Bearer token"
        if url == "https://api.redgifs.com/v2/gallery/g1":
            return Mock(text=json.dumps(gallery))
        return Mock(text=json.dumps({"gif": gif}))

    monkeypatch.setattr(Redgifs, "_get_token", classmethod(lambda cls, renew=False: "token"))
    monkeypatch.setattr(Redgifs, "retrieve_url", staticmethod(retrieve_url))
    assert Redgifs._get_link("https://www.redgifs.com/watch/lustrousstickywaxwing") == expected
