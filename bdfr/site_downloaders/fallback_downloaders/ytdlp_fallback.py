#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging

from bdfr.configuration import Configuration
from bdfr.exceptions import NotADownloadableLinkError
from bdfr.post import Post
from bdfr.resource import Resource
from bdfr.site_downloaders.fallback_downloaders.fallback_downloader import BaseFallbackDownloader
from bdfr.site_downloaders.youtube import Youtube

logger = logging.getLogger(__name__)


class YtdlpFallback(BaseFallbackDownloader, Youtube):
    def __init__(self, post: Post, args: Configuration):
        super().__init__(post, args)

    def find_resources(self) -> list[Resource]:
        out = Resource(
            self.post,
            self.post.url,
            super()._download_video({}),
            super().get_video_attributes(self.post.url)["ext"],
        )
        return [out]

    @staticmethod
    def can_handle_link(url: str) -> bool:
        try:
            attributes = YtdlpFallback.get_video_attributes(url)
        except NotADownloadableLinkError:
            return False
        if attributes:
            return True
