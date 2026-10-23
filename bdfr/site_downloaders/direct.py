#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re

from bdfr.configuration import Configuration
from bdfr.exceptions import ContentGone
from bdfr.post import Post
from bdfr.resource import Resource
from bdfr.site_downloaders.base_downloader import BaseDownloader
from bdfr.site_downloaders.gallery import Gallery


class Direct(BaseDownloader):
    def __init__(self, post: Post, args: Configuration):
        super().__init__(post, args)

    def find_resources(self) -> list[Resource]:
        url = self.post.url
        # Some posts link an i.redd.it image without its extension, which the file name needs
        bare_image = re.match(r"https?://i\.redd\.it/(\w+)/?$", url)
        if bare_image:
            url = Gallery.find_image(bare_image.group(1))
            if not url:
                raise ContentGone(f"Image {self.post.url} not found on i.redd.it with any extension")
        return [Resource(self.post, url, Resource.retry_download(url))]
