#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from unittest.mock import MagicMock

import pytest

from bdfr.configuration import Configuration
from bdfr.exceptions import SiteDownloaderError
from bdfr.resource import http_session
from bdfr.site_downloaders.gallery import Gallery


@pytest.mark.parametrize(
    ("removed", "message"),
    (("reddit", "removed by Reddit"), ("deleted", "deleted by its author"), (None, "No images found")),
)
def test_gallery_without_data_says_why(removed, message):
    post = MagicMock(spec=["id", "gallery_data", "crosspost_parent_list", "removed_by_category"])
    post.id = "abc1234"
    post.gallery_data = None
    post.crosspost_parent_list = None
    post.removed_by_category = removed
    with pytest.raises(SiteDownloaderError, match=message):
        Gallery(post, Configuration()).find_resources()


@pytest.mark.parametrize(
    ("answers", "expected", "tried"),
    (
        ({".jpg": 200}, "https://i.redd.it/abc.jpg", [".jpg"]),
        # .jpg is always tried first, then the others in a fixed order (stored URLs depend on it)
        ({".png": 200}, "https://i.redd.it/abc.png", [".jpg", ".png"]),
        ({".jpeg": 200, ".gif": 200}, "https://i.redd.it/abc.gif", [".jpg", ".png", ".gif"]),
        ({}, None, [".jpg", ".png", ".gif", ".gifv", ".jpeg"]),
    ),
)
def test_find_image_tries_jpg_first(answers: dict, expected, tried: list[str], monkeypatch: pytest.MonkeyPatch):
    asked = []

    def head(url, **kwargs):
        asked.append(url)
        return MagicMock(status_code=answers.get(url[url.rindex(".") :], 404))

    monkeypatch.setattr(http_session(), "head", head)
    assert Gallery.find_image("abc") == expected
    assert asked == [f"https://i.redd.it/abc{extension}" for extension in tried]
