#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import re
from unittest.mock import MagicMock, Mock, PropertyMock

import pytest
import requests

from bdfr.configuration import Configuration
from bdfr.resource import Resource
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


@pytest.mark.online
@pytest.mark.parametrize(
    ("test_url", "expected"),
    (
        ("https://redgifs.com/watch/frighteningvictorioussalamander", {"FrighteningVictoriousSalamander.mp4"}),
        ("https://redgifs.com/watch/springgreendecisivetaruca", {"SpringgreenDecisiveTaruca.mp4"}),
        ("https://www.redgifs.com/watch/palegoldenrodrawhalibut", {"PalegoldenrodRawHalibut.mp4"}),
        ("https://redgifs.com/watch/hollowintentsnowyowl", {"HollowIntentSnowyowl-large.jpg"}),
        (
            "https://www.redgifs.com/watch/lustrousstickywaxwing",
            {
                "EntireEnchantingHypsilophodon-large.jpg",
                "FancyMagnificentAdamsstaghornedbeetle-large.jpg",
                "LustrousStickyWaxwing-large.jpg",
                "ParchedWindyArmyworm-large.jpg",
                "ThunderousColorlessErmine-large.jpg",
                "UnripeUnkemptWoodpecker-large.jpg",
            },
        ),
        ("https://www.redgifs.com/watch/genuineprivateguillemot/", {"GenuinePrivateGuillemot.mp4"}),
    ),
)
def test_get_link(test_url: str, expected: set[str]):
    result = Redgifs._get_link(test_url)
    result = list(result)
    patterns = [r"https://thumbs\d\.redgifs\.com/" + e + r".*" for e in expected]
    assert all([re.match(p, r) for p in patterns] for r in result)


@pytest.mark.online
@pytest.mark.parametrize(
    ("test_url", "expected_hashes"),
    (
        ("https://redgifs.com/watch/frighteningvictorioussalamander", {"4007c35d9e1f4b67091b5f12cffda00a"}),
        ("https://redgifs.com/watch/springgreendecisivetaruca", {"8dac487ac49a1f18cc1b4dabe23f0869"}),
        ("https://redgifs.com/watch/leafysaltydungbeetle", {"076792c660b9c024c0471ef4759af8bd"}),
        ("https://www.redgifs.com/watch/palegoldenrodrawhalibut", {"46d5aa77fe80c6407de1ecc92801c10e"}),
        ("https://redgifs.com/watch/hollowintentsnowyowl", {"5ee51fa15e0a58e98f11dea6a6cca771"}),
        (
            "https://www.redgifs.com/watch/lustrousstickywaxwing",
            {
                "b461e55664f07bed8d2f41d8586728fa",
                "30ba079a8ed7d7adf17929dc3064c10f",
                "0d4f149d170d29fc2f015c1121bab18b",
                "53987d99cfd77fd65b5fdade3718f9f1",
                "fb2e7d972846b83bf4016447d3060d60",
                "44fb28f72ec9a5cca63fa4369ab4f672",
            },
        ),
    ),
)
def test_download_resource(test_url: str, expected_hashes: set[str]):
    mock_submission = Mock()
    mock_submission.url = test_url
    test_site = Redgifs(mock_submission, Configuration())
    results = test_site.find_resources()
    assert all([isinstance(res, Resource) for res in results])
    [res.download() for res in results]
    hashes = set([res.hash.hexdigest() for res in results])
    assert hashes == set(expected_hashes)


@pytest.mark.online
@pytest.mark.parametrize(
    ("test_url", "expected_link", "expected_hash"),
    (
        (
            "https://redgifs.com/watch/flippantmemorablebaiji",
            {"FlippantMemorableBaiji-mobile.mp4"},
            {"41a5fb4865367ede9f65fc78736f497a"},
        ),
        (
            "https://redgifs.com/watch/thirstyunfortunatewaterdragons",
            {"thirstyunfortunatewaterdragons-mobile.mp4"},
            {"1a51dad8fedb594bdd84f027b3cbe8af"},
        ),
        (
            "https://redgifs.com/watch/conventionalplainxenopterygii",
            {"conventionalplainxenopterygii-mobile.mp4"},
            {"2e1786b3337da85b80b050e2c289daa4"},
        ),
    ),
)
def test_hd_soft_fail(test_url: str, expected_link: set[str], expected_hash: set[str]):
    link = Redgifs._get_link(test_url)
    link = list(link)
    patterns = [r"https://thumbs\d\.redgifs\.com/" + e + r".*" for e in expected_link]
    assert all([re.match(p, r) for p in patterns] for r in link)
    mock_submission = Mock()
    mock_submission.url = test_url
    test_site = Redgifs(mock_submission, Configuration())
    results = test_site.find_resources()
    assert all([isinstance(res, Resource) for res in results])
    [res.download() for res in results]
    hashes = set([res.hash.hexdigest() for res in results])
    assert hashes == set(expected_hash)


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
