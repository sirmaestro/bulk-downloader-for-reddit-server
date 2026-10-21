#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging

import requests

from bdfr.configuration import Configuration
from bdfr.exceptions import SiteDownloaderError
from bdfr.post import Post
from bdfr.resource import Resource
from bdfr.site_downloaders.base_downloader import BaseDownloader

logger = logging.getLogger(__name__)

# Reddit's removed_by_category values
REMOVED = {
    "deleted": "deleted by its author",
    "author": "deleted by its author",
    "moderator": "removed by a moderator",
    "reddit": "removed by Reddit",
    "automod_filtered": "removed by AutoModerator",
    "copyright_takedown": "removed for copyright",
    "content_takedown": "removed by Reddit",
}


class Gallery(BaseDownloader):
    def __init__(self, post: Post, args: Configuration):
        super().__init__(post, args)

    def find_resources(self) -> list[Resource]:
        try:
            image_urls = self._get_links(self.post.gallery_data["items"])
        except (AttributeError, TypeError):
            try:
                image_urls = self._get_links(self.post.crosspost_parent_list[0]["gallery_data"]["items"])
            except (AttributeError, IndexError, TypeError, KeyError):
                # Reddit drops the image list of removed posts
                removed = getattr(self.post, "removed_by_category", None)
                if removed:
                    raise SiteDownloaderError(REMOVED.get(removed, f"removed ({removed.replace('_', ' ')})"))
                logger.debug(f"Could not find gallery data in submission {self.post.id}")
                raise SiteDownloaderError("No images found in Reddit gallery")

        if not image_urls:
            raise SiteDownloaderError("No images found in Reddit gallery")
        return [Resource(self.post, url, Resource.retry_download(url)) for url in image_urls]

    @staticmethod
    def _get_links(id_dict: list[dict]) -> list[str]:
        out = []
        for item in id_dict:
            image_id = item["media_id"]
            possible_extensions = (".jpg", ".png", ".gif", ".gifv", ".jpeg")
            for extension in possible_extensions:
                test_url = f"https://i.redd.it/{image_id}{extension}"
                response = requests.head(test_url, timeout=30)
                if response.status_code == 200:
                    out.append(test_url)
                    break
        return out
