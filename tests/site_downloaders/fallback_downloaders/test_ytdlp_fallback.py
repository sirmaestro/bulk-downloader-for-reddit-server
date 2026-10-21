#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import socket
from unittest.mock import MagicMock

import pytest
import yt_dlp

from bdfr.configuration import Configuration
from bdfr.exceptions import NotADownloadableLinkError
from bdfr.resource import Resource
from bdfr.site_downloaders.fallback_downloaders.ytdlp_fallback import YtdlpFallback


@pytest.mark.parametrize(
    ("test_url", "expected"),
    (
        ("https://www.reddit.com/r/specializedtools/comments/n2nw5m/bamboo_splitter/", True),
        ("https://www.youtube.com/watch?v=P19nvJOmqCc", True),
        ("https://milesmatrix.bandcamp.com/album/la-boum/", True),
        ("https://www.example.com/test", False),
        ("https://onlyfans.com/littledaisy", False),
        ("https://www.fanvue.com/peonyneko/fv-2", False),
    ),
)
def test_can_handle_link(test_url: str, expected: bool, monkeypatch: pytest.MonkeyPatch):
    # Only yt-dlp's site extractors count (never its generic page reader), and no request is made
    def no_requests(*args, **kwargs):
        raise AssertionError("can_handle_link made a request")

    monkeypatch.setattr(socket, "getaddrinfo", no_requests)
    result = YtdlpFallback.can_handle_link(test_url)
    assert result == expected


@pytest.mark.online
@pytest.mark.parametrize("test_url", ("https://milesmatrix.bandcamp.com/album/la-boum/",))
def test_info_extraction_bad(test_url: str):
    with pytest.raises(NotADownloadableLinkError):
        YtdlpFallback.get_video_attributes(test_url)


@pytest.mark.online
@pytest.mark.slow
@pytest.mark.parametrize(
    ("test_url", "expected_hash"),
    (
        ("https://streamable.com/dt46y", "b7e465adaade5f2b6d8c2b4b7d0a2878"),
        ("https://streamable.com/t8sem", "49b2d1220c485455548f1edbc05d4ecf"),
        (
            "https://www.reddit.com/r/specializedtools/comments/n2nw5m/bamboo_splitter/",
            "6c6ff46e04b4e33a755ae2a9b5a45ac5",
        ),
        ("https://v.redd.it/9z1dnk3xr5k61", "226cee353421c7aefb05c92424cc8cdd"),
    ),
)
def test_find_resources(test_url: str, expected_hash: str):
    test_submission = MagicMock()
    test_submission.url = test_url
    downloader = YtdlpFallback(test_submission, Configuration())
    resources = downloader.find_resources()
    assert len(resources) == 1
    assert isinstance(resources[0], Resource)
    for res in resources:
        res.download()
    assert resources[0].hash.hexdigest() == expected_hash


def test_failed_extraction_gives_reason(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    # yt-dlp's reason is in the error the downloader logs; no traceback is logged
    def extract_info(self, url, download=True):
        raise yt_dlp.utils.DownloadError("ERROR: [Instagram] DWt7yZOgiME: Instagram sent an empty media response")

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", extract_info)
    with pytest.raises(NotADownloadableLinkError, match="Instagram sent an empty media response"):
        YtdlpFallback.get_video_attributes("https://www.instagram.com/reel/DWt7yZOgiME/")
    assert not caplog.records
