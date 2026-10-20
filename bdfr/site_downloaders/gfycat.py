#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
from typing import Optional

from praw.models import Submission

from bdfr.configuration import Configuration
from bdfr.exceptions import SiteDownloaderError
from bdfr.resource import Resource
from bdfr.site_authenticator import SiteAuthenticator
from bdfr.site_downloaders.redgifs import Redgifs


class Gfycat(Redgifs):
    """Gfycat shut down in 2023; its clips that survive moved to Redgifs under the same ID in lower case"""

    def __init__(self, post: Submission, args: Configuration):
        super().__init__(post, args)

    def find_resources(self, authenticator: Optional[SiteAuthenticator] = None) -> list[Resource]:
        return super().find_resources(authenticator)

    @staticmethod
    def _get_link(url: str) -> set[str]:
        match = re.match(r".*/(?:ifr/|gifs/detail/)?([A-Za-z]+)(?:[-./?#].*)?$", url.rstrip("/"))
        if not match:
            raise SiteDownloaderError(f"Could not extract Gfycat ID from {url}")
        return Redgifs._get_link(f"https://www.redgifs.com/watch/{match.group(1).lower()}")
