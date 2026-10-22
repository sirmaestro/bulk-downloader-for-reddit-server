#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import functools
import logging

from yt_dlp.extractor import gen_extractor_classes

from bdfr.configuration import Configuration
from bdfr.post import Post
from bdfr.resource import Resource
from bdfr.site_downloaders.fallback_downloaders.fallback_downloader import BaseFallbackDownloader
from bdfr.site_downloaders.youtube import Youtube

logger = logging.getLogger(__name__)


class YtdlpFallback(BaseFallbackDownloader, Youtube):
    def __init__(self, post: Post, args: Configuration):
        super().__init__(post, args)

    def find_resources(self) -> list[Resource]:
        info = super().get_video_attributes(self.post.url)
        return [Resource(self.post, self.post.url, super()._download_video({}, info=info), info["ext"])]

    @staticmethod
    @functools.cache
    def _site_extractors() -> tuple:
        # yt-dlp's generic extractor fetches any page and looks for embedded media; on the links posts carry that
        # aren't media (OnlyFans, Fanvue, link pages, ...) it only makes a request and fails, so it isn't used
        return tuple(ie for ie in gen_extractor_classes() if ie.ie_key() != "Generic")

    @staticmethod
    def can_handle_link(url: str) -> bool:
        # Decided from the URL alone, before the post is checked against the folder, so no request is made here;
        # find_resources() reads the media info once and reports why yt-dlp can't download it (login needed, ...)
        return any(ie.suitable(url) for ie in YtdlpFallback._site_extractors())
