#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from unittest.mock import MagicMock

import pytest

from bdfr.configuration import Configuration
from bdfr.exceptions import NotADownloadableLinkError
from bdfr.resource import Resource
from bdfr.site_downloaders.vreddit import VReddit


@pytest.mark.online
@pytest.mark.slow
@pytest.mark.parametrize(
    ("test_url", "expected_hash"),
    (("https://reddit.com/r/Unexpected/comments/z4xsuj/omg_thats_so_cute/", "1ffab5e5c0cc96db18108e4f37e8ca7f"),),
)
def test_find_resources_good(test_url: str, expected_hash: str):
    test_submission = MagicMock()
    test_submission.url = test_url
    downloader = VReddit(test_submission, Configuration())
    resources = downloader.find_resources()
    assert len(resources) == 1
    assert isinstance(resources[0], Resource)
    resources[0].download()
    assert resources[0].hash.hexdigest() == expected_hash


@pytest.mark.online
@pytest.mark.parametrize(
    "test_url",
    (
        "https://www.polygon.com/disney-plus/2020/5/14/21249881/gargoyles-animated-series-disney-plus-greg-weisman"
        "-interview-oj-simpson-goliath-chronicles",
    ),
)
def test_find_resources_bad(test_url: str):
    test_submission = MagicMock()
    test_submission.url = test_url
    downloader = VReddit(test_submission, Configuration())
    with pytest.raises(NotADownloadableLinkError):
        downloader.find_resources()


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
