#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from unittest.mock import MagicMock

import pytest

from bdfr.configuration import Configuration
from bdfr.exceptions import ContentGone, NotADownloadableLinkError, SiteDownloaderError
from bdfr.post import Post
from bdfr.site_downloaders.vreddit import VReddit
from bdfr.site_downloaders.youtube import Youtube


@pytest.mark.parametrize(
    ("post_data", "expected"),
    (
        # The fixed playlist path is preferred: same qualities, and it doesn't expire like the signed hls_url
        (
            {"secure_media": {"reddit_video": {"hls_url": "https://v.redd.it/a/HLS.m3u8?x=1"}}},
            "https://v.redd.it/abc123/HLSPlaylist.m3u8",
        ),
        (
            {
                "url": "https://www.reddit.com/r/test/comments/def456/x/",
                "secure_media": None,
                "crosspost_parent_list": [{"media": {"reddit_video": {"hls_url": "https://v.redd.it/b/p"}}}],
            },
            "https://v.redd.it/b/p",
        ),
        ({"secure_media": {}, "media": {}}, "https://v.redd.it/abc123/HLSPlaylist.m3u8"),
    ),
)
def test_find_video_url(post_data: dict, expected: str):
    post = MagicMock(spec=["url", "secure_media", "media", "crosspost_parent_list"])
    post.url = "https://v.redd.it/abc123"
    post.media = None
    post.crosspost_parent_list = None
    for key, value in post_data.items():
        setattr(post, key, value)
    assert VReddit(post, Configuration())._find_video_url() == expected


@pytest.mark.parametrize(
    ("url", "message", "gone"),
    (
        ("https://v.redd.it/i0waudehxe3h1", "HTTP Error 403: Forbidden (caused by <HTTPError 403: Forbidden>)", True),
        ("https://v.redd.it/i0waudehxe3h1", "HTTP Error 404: Not Found", True),
        # Other failures, and 403s from elsewhere, may pass: tried again later
        ("https://v.redd.it/i0waudehxe3h1", "HTTP Error 500: Internal Server Error", False),
        ("https://v.redd.it/i0waudehxe3h1", "Unable to download webpage: timed out", False),
    ),
)
def test_deleted_video_is_gone(url: str, message: str, gone: bool, monkeypatch: pytest.MonkeyPatch):
    def fail(video_url):
        raise NotADownloadableLinkError(f"Video info extraction failed for {video_url}: {message}")

    monkeypatch.setattr(Youtube, "get_video_data", staticmethod(fail))
    post = Post({"id": "1tnwp48", "url": url})
    with pytest.raises(SiteDownloaderError) as raised:
        VReddit(post, Configuration()).find_resources()
    assert isinstance(raised.value, ContentGone) is gone
