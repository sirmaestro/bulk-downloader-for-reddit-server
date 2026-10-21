#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging

from bdfr.configuration import Configuration
from bdfr.post import Post
from bdfr.resource import Resource
from bdfr.site_downloaders.base_downloader import BaseDownloader

logger = logging.getLogger(__name__)


class DelayForReddit(BaseDownloader):
    def __init__(self, post: Post, args: Configuration):
        super().__init__(post, args)

    def find_resources(self) -> list[Resource]:
        media = DelayForReddit.retrieve_url(self.post.url)
        return [Resource(self.post, media.url, Resource.retry_download(media.url))]
