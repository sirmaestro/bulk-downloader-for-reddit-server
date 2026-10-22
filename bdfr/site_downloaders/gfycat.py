#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import urllib.parse

from bdfr.configuration import Configuration
from bdfr.exceptions import SiteDownloaderError
from bdfr.post import Post
from bdfr.resource import Resource
from bdfr.site_downloaders.redgifs import Redgifs


class Gfycat(Redgifs):
    """Gfycat shut down in 2023; its clips that survive moved to Redgifs under the same ID in lower case"""

    def __init__(self, post: Post, args: Configuration):
        super().__init__(post, args)

    def find_resources(self) -> list[Resource]:
        return super().find_resources()

    @staticmethod
    def _get_link(url: str) -> set[str]:
        # Only the path: a link without an ID (e.g. https://gfycat.com/) must not take the host name for one
        path = urllib.parse.urlsplit(url).path.rstrip("/")
        match = re.match(r".*/(?:ifr/|gifs/detail/)?([A-Za-z]+)(?:[-./?#].*)?$", path)
        if not match:
            raise SiteDownloaderError(f"Could not extract Gfycat ID from {url}")
        return Redgifs._get_link(f"https://www.redgifs.com/watch/{match.group(1).lower()}")
