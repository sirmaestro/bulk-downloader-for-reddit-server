#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import re
from typing import Optional

from praw.models import Submission

from bdfr.configuration import Configuration
from bdfr.exceptions import NotADownloadableLinkError
from bdfr.resource import Resource
from bdfr.site_authenticator import SiteAuthenticator
from bdfr.site_downloaders.youtube import Youtube

logger = logging.getLogger(__name__)


class VReddit(Youtube):
    def __init__(self, post: Submission, args: Configuration):
        super().__init__(post, args)

    def find_resources(self, authenticator: Optional[SiteAuthenticator] = None) -> list[Resource]:
        ytdl_options = {
            "playlistend": 1,
            "nooverwrites": True,
        }
        video_url = self._find_video_url()
        download_function = self._download_video(ytdl_options, video_url)
        extension = self.get_video_attributes(video_url)["ext"]
        # The resource keeps the post's URL so existing downloads and stored URLs still match
        res = Resource(self.post, self.post.url, download_function, extension)
        return [res]

    def _find_video_url(self) -> str:
        """The video's HLS playlist, from which yt-dlp picks the best video and audio streams"""
        # v.redd.it serves the full playlist at a fixed path. It lists the same qualities as the hls_url in the post
        # data, but needs no post data and doesn't expire (hls_url is signed, so archived copies go stale).
        match = re.match(r"https?://v\.redd\.it/(\w+)", self.post.url)
        if match:
            return f"https://v.redd.it/{match.group(1)}/HLSPlaylist.m3u8"
        # Posts linking elsewhere, such as crossposts, only name the video in their data
        sources = [getattr(self.post, "secure_media", None), getattr(self.post, "media", None)]
        for parent in getattr(self.post, "crosspost_parent_list", None) or []:
            sources.extend((parent.get("secure_media"), parent.get("media")))
        for media in sources:
            try:
                return media["reddit_video"]["hls_url"]
            except (KeyError, TypeError):
                continue
        return self.post.url

    @staticmethod
    def get_video_attributes(url: str) -> dict:
        result = VReddit.get_video_data(url)
        if "ext" in result:
            return result
        else:
            try:
                result = result["entries"][0]
                return result
            except Exception as e:
                logger.exception(e)
                raise NotADownloadableLinkError(f"Video info extraction failed for {url}")
