#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from unittest.mock import patch

import pytest

from bdfr.exceptions import SiteDownloaderError
from bdfr.site_downloaders.gfycat import Gfycat


@pytest.mark.parametrize(
    ("test_url", "expected_url"),
    (
        ("https://gfycat.com/definitivecaninecrayfish", "https://www.redgifs.com/watch/definitivecaninecrayfish"),
        ("https://gfycat.com/WearyComposedHairstreak", "https://www.redgifs.com/watch/wearycomposedhairstreak"),
        ("https://gfycat.com/WearyComposedHairstreak-some-title", "https://www.redgifs.com/watch/wearycomposedhairstreak"),
        ("https://gfycat.com/ifr/WearyComposedHairstreak", "https://www.redgifs.com/watch/wearycomposedhairstreak"),
        (
            "https://thumbs.gfycat.com/ComposedWholeBullfrog-size_restricted.gif",
            "https://www.redgifs.com/watch/composedwholebullfrog",
        ),
        ("https://giant.gfycat.com/ComposedWholeBullfrog.mp4", "https://www.redgifs.com/watch/composedwholebullfrog"),
    ),
)
def test_get_link_uses_redgifs(test_url: str, expected_url: str):
    with patch("bdfr.site_downloaders.gfycat.Redgifs._get_link", return_value={"media"}) as mock_get_link:
        assert Gfycat._get_link(test_url) == {"media"}
    mock_get_link.assert_called_once_with(expected_url)


def test_get_link_bad_url():
    with pytest.raises(SiteDownloaderError):
        Gfycat._get_link("https://gfycat.com/")
