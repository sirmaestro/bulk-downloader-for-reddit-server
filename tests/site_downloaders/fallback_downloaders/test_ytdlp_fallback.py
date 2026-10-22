#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import socket

import pytest
import yt_dlp

from bdfr.exceptions import NotADownloadableLinkError
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


def test_failed_extraction_gives_reason(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    # yt-dlp's reason is in the error the downloader logs; no traceback is logged
    def extract_info(self, url, download=True):
        raise yt_dlp.utils.DownloadError("ERROR: [Instagram] DWt7yZOgiME: Instagram sent an empty media response")

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", extract_info)
    with pytest.raises(NotADownloadableLinkError, match="Instagram sent an empty media response"):
        YtdlpFallback.get_video_attributes("https://www.instagram.com/reel/DWt7yZOgiME/")
    assert not caplog.records
