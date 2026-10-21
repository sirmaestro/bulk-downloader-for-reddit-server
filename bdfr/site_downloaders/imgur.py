#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import logging
import re
import urllib.request

from bdfr import __version__
from bdfr.configuration import Configuration
from bdfr.exceptions import ResourceNotFound, SiteDownloaderError
from bdfr.post import Post
from bdfr.resource import Resource
from bdfr.site_downloaders.base_downloader import BaseDownloader

logger = logging.getLogger(__name__.replace("site_downloaders", ""))

# Imgur answers every request from python-requests' (and urllib's) default User-Agent with 429 Too Many Requests
HEADERS = {"User-Agent": f"bdfr/{__version__} (personal archiving)"}


class Imgur(BaseDownloader):
    def __init__(self, post: Post, args: Configuration):
        super().__init__(post, args)
        self.raw_data = {}

    def find_resources(self) -> list[Resource]:
        if re.search(r".*/(i.|)imgur.com/[^/]*(.jpg|.jpeg|.png|.mp4)(\?1|)$", self.post.url):
            self.raw_data["link"] = self.post.url.replace("http://", "https://")
        else:
            try:
                self.raw_data = self._get_data(self.post.url)
            except Exception as e:
                if (
                    self.args.imgur_fix404
                    and "responded with 404" in str(e)
                    and re.search(r".*(.gif|.gifv)$", self.post.url)
                ):
                    logger.debug(f"{e}")
                    out = []
                    if re.search(r".*(.gifv)$", self.post.url):
                        request = urllib.request.Request(self.post.url[:-1], headers=HEADERS)
                        with urllib.request.urlopen(request, timeout=30) as response:
                            magic = response.read(3)
                        if not self.args.imgur_originals or magic == b"\xff\xd8\xff":
                            logger.debug(f"using {self.post.url[:-4]}mp4")
                            out.append(
                                Resource(
                                    self.post,
                                    self.post.url[:-4] + "mp4",
                                    Resource.retry_download(self.post.url[:-4] + "mp4", HEADERS),
                                )
                            )
                        else:
                            logger.debug(f"using {self.post.url[:-1]}")
                            out.append(
                                Resource(
                                    self.post, self.post.url[:-1], Resource.retry_download(self.post.url[:-1], HEADERS)
                                )
                            )
                    else:
                        logger.debug(f"using {self.post.url}")
                        out.append(Resource(self.post, self.post.url, Resource.retry_download(self.post.url, HEADERS)))
                    return out
                raise

        out = []
        if "is_album" in self.raw_data:
            for image in self.raw_data["images"]:
                if not self.args.imgur_originals and "mp4" in image:
                    out.append(Resource(self.post, image["mp4"], Resource.retry_download(image["mp4"], HEADERS)))
                else:
                    out.append(Resource(self.post, image["link"], Resource.retry_download(image["link"], HEADERS)))
        else:
            if not self.args.imgur_originals and "mp4" in self.raw_data:
                out.append(
                    Resource(self.post, self.raw_data["mp4"], Resource.retry_download(self.raw_data["mp4"], HEADERS))
                )
            else:
                out.append(
                    Resource(self.post, self.raw_data["link"], Resource.retry_download(self.raw_data["link"], HEADERS))
                )
        return out

    @staticmethod
    def _get_data(link: str) -> dict:
        try:
            if link.endswith("/"):
                link = link.removesuffix("/")
            if re.search(r".*/(.*?)(gallery/|a/)", link):
                imgur_id = re.match(r".*/(?:gallery/|a/)(.*?)(?:/.*)?$", link).group(1)
                link = f"https://api.imgur.com/3/album/{imgur_id}"
            else:
                imgur_id = re.match(r".*/(.*?)(?:_d)?(?:\..{0,})?$", link).group(1)
                link = f"https://api.imgur.com/3/image/{imgur_id}"
        except AttributeError:
            raise SiteDownloaderError(f"Could not extract Imgur ID from {link}")

        headers = {
            **HEADERS,
            "referer": "https://imgur.com/",
            "origin": "https://imgur.com",
            "content-type": "application/json",
            "Authorization": "Client-ID 546c25a59c58ad7",
        }
        try:
            res = Imgur.retrieve_url(link, headers=headers)
        except ResourceNotFound as e:
            raise SiteDownloaderError(f"Could not retrieve data: {e}")

        try:
            image_dict = json.loads(res.text)
        except json.JSONDecodeError as e:
            raise SiteDownloaderError(f"Could not parse received response as JSON: {e}")

        return image_dict["data"]
