#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import hashlib
from pathlib import Path

from bdfr.configuration import Configuration
from bdfr.post import Post
from bdfr.site_downloaders.self_post import SelfPost

POST = {
    "id": "abc1234",
    "url": "https://www.reddit.com/r/test/comments/abc1234/title/",
    "selftext": "Some text",
    "subreddit": "test",
    "author": "someone",
}


def test_self_post_text():
    (resource,) = SelfPost(Post(POST), Configuration()).find_resources()
    expected = (
        "## [t3_abc1234](https://www.reddit.com/r/test/comments/abc1234/title/)\nSome text\n\n---\n\n"
        "submitted to [r/test](https://www.reddit.com/r/test) by [u/someone](https://www.reddit.com/user/someone)"
    )
    assert resource.content.decode("utf-8") == expected
    assert resource.extension == ".txt"
    assert resource.hash.hexdigest() == hashlib.md5(expected.encode("utf-8")).hexdigest()


def test_deleted_author():
    (resource,) = SelfPost(Post(dict(POST, author="[deleted]")), Configuration()).find_resources()
    assert resource.content.decode("utf-8").endswith("by [u/DELETED](https://www.reddit.com/user/DELETED)")


def test_saved_as_written_without_downloading(tmp_path: Path):
    # The downloader asks for a temporary file in the folder; the text is already there, so it's written as is
    (resource,) = SelfPost(Post(POST), Configuration()).find_resources()
    resource.download({"temp_dir": tmp_path})
    resource.save(tmp_path / "abc1234.txt")
    assert [p.name for p in tmp_path.iterdir()] == ["abc1234.txt"]
    assert (tmp_path / "abc1234.txt").read_bytes() == resource.content
