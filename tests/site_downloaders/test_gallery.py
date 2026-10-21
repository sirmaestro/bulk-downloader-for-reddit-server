#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from unittest.mock import MagicMock

import pytest

from bdfr.configuration import Configuration
from bdfr.exceptions import SiteDownloaderError
from bdfr.site_downloaders.gallery import Gallery


@pytest.mark.online
@pytest.mark.parametrize(
    ("test_ids", "expected"),
    (
        (
            [
                {"media_id": "18nzv9ch0hn61"},
                {"media_id": "jqkizcch0hn61"},
                {"media_id": "k0fnqzbh0hn61"},
                {"media_id": "m3gamzbh0hn61"},
            ],
            {
                "https://i.redd.it/18nzv9ch0hn61.jpg",
                "https://i.redd.it/jqkizcch0hn61.jpg",
                "https://i.redd.it/k0fnqzbh0hn61.jpg",
                "https://i.redd.it/m3gamzbh0hn61.jpg",
            },
        ),
        (
            [
                {"media_id": "04vxj25uqih61"},
                {"media_id": "0fnx83kpqih61"},
                {"media_id": "7zkmr1wqqih61"},
                {"media_id": "u37k5gxrqih61"},
            ],
            {
                "https://i.redd.it/04vxj25uqih61.png",
                "https://i.redd.it/0fnx83kpqih61.png",
                "https://i.redd.it/7zkmr1wqqih61.png",
                "https://i.redd.it/u37k5gxrqih61.png",
            },
        ),
    ),
)
def test_gallery_get_links(test_ids: list[dict], expected: set[str]):
    results = Gallery._get_links(test_ids)
    assert set(results) == expected


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
