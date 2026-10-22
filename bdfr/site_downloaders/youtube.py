#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import copy
import logging
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Optional, Union

import yt_dlp

from bdfr.configuration import Configuration
from bdfr.exceptions import NotADownloadableLinkError, SiteDownloaderError
from bdfr.post import Post
from bdfr.resource import DownloadedFile, Resource
from bdfr.site_downloaders.base_downloader import BaseDownloader

logger = logging.getLogger(__name__)


class Youtube(BaseDownloader):
    def __init__(self, post: Post, args: Configuration):
        super().__init__(post, args)

    def find_resources(self) -> list[Resource]:
        ytdl_options = {
            "format": "bestvideo*+bestaudio/best",
            "playlistend": 1,
            "nooverwrites": True,
        }
        info = self.get_video_attributes(self.post.url)
        res = Resource(self.post, self.post.url, self._download_video(ytdl_options, info=info), info["ext"])
        return [res]

    def _download_video(self, ytdl_options: dict, url: Optional[str] = None, info: Optional[dict] = None) -> Callable:
        """The download function for a video. Given the video's details from get_video_attributes, it downloads from
        those instead of reading them again"""
        url = url or self.post.url
        yt_logger = logging.getLogger("youtube-dl")
        yt_logger.setLevel(logging.CRITICAL)
        ytdl_options["quiet"] = True
        ytdl_options["logger"] = yt_logger

        def download(download_parameters: dict) -> Union[bytes, DownloadedFile]:
            with tempfile.TemporaryDirectory() as temp_dir:
                download_path = Path(temp_dir).resolve()
                ytdl_options["outtmpl"] = str(download_path) + "/" + "test.%(ext)s"
                try:
                    with yt_dlp.YoutubeDL(ytdl_options) as ydl:
                        if info is None:
                            ydl.download([url])
                        else:
                            ydl.process_ie_result(copy.deepcopy(info), download=True)
                except yt_dlp.DownloadError as e:
                    raise SiteDownloaderError(f"Youtube download failed: {e}")

                downloaded_files = list(download_path.iterdir())
                if downloaded_files:
                    downloaded_file = downloaded_files[0]
                else:
                    raise NotADownloadableLinkError(f"No media exists in the URL {url}")
                if destination_dir := download_parameters.get("temp_dir"):
                    # Copied next to its destination, so it's renamed into place
                    return DownloadedFile.copy_from(downloaded_file, Path(destination_dir))
                with downloaded_file.open("rb") as file:
                    return file.read()

        return download

    @staticmethod
    def get_video_data(url: str) -> dict:
        yt_logger = logging.getLogger("youtube-dl")
        yt_logger.setLevel(logging.CRITICAL)
        # A profile or playlist link would otherwise have every entry read; it isn't one video, so one is enough
        with yt_dlp.YoutubeDL(
            {
                "logger": yt_logger,
                "noplaylist": True,
                "playlistend": 1,
            }
        ) as ydl:
            try:
                result = ydl.extract_info(url, download=False)
            except Exception as e:
                # The caller logs the failure; yt-dlp's traceback says nothing more than its message
                message = str(e).removeprefix("ERROR: ")
                raise NotADownloadableLinkError(f"Video info extraction failed for {url}: {message}")
        return result

    @staticmethod
    def get_video_attributes(url: str) -> dict:
        result = Youtube.get_video_data(url)
        if result is not None and "ext" in result:
            return result
        else:
            raise NotADownloadableLinkError(f"Video info extraction failed for {url}")
