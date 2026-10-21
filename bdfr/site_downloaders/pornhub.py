#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging

from bdfr.configuration import Configuration
from bdfr.exceptions import SiteDownloaderError
from bdfr.post import Post
from bdfr.resource import Resource
from bdfr.site_downloaders.youtube import Youtube

logger = logging.getLogger(__name__)


class PornHub(Youtube):
    def __init__(self, post: Post, args: Configuration):
        super().__init__(post, args)

    def find_resources(self) -> list[Resource]:
        ytdl_options = {
            "format": "bestvideo*+bestaudio/best",
            "nooverwrites": True,
        }
        if video_attributes := super().get_video_attributes(self.post.url):
            extension = video_attributes["ext"]
        else:
            raise SiteDownloaderError()

        out = Resource(
            self.post,
            self.post.url,
            super()._download_video(ytdl_options),
            extension,
        )
        return [out]
