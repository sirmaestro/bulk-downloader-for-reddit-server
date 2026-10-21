#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from bdfr.configuration import Configuration
from bdfr.post import Post
from bdfr.resource import Resource
from bdfr.site_downloaders.base_downloader import BaseDownloader


class Direct(BaseDownloader):
    def __init__(self, post: Post, args: Configuration):
        super().__init__(post, args)

    def find_resources(self) -> list[Resource]:
        return [Resource(self.post, self.post.url, Resource.retry_download(self.post.url))]
