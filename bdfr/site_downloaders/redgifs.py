#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import re
import threading
from typing import Optional

import requests

from bdfr.configuration import Configuration
from bdfr.exceptions import ResourceNotFound, SiteDownloaderError
from bdfr.post import Post
from bdfr.resource import Resource
from bdfr.site_downloaders.base_downloader import BaseDownloader


class Redgifs(BaseDownloader):
    REQUEST_TIMEOUT = 10
    # Temporary API tokens last about a day, so one is shared by every post (and download thread) in a run
    _token: Optional[str] = None
    _token_lock = threading.Lock()

    def __init__(self, post: Post, args: Configuration):
        super().__init__(post, args)

    def find_resources(self) -> list[Resource]:
        media_urls = self._get_link(self.post.url)
        return [Resource(self.post, m, Resource.retry_download(m), None) for m in media_urls]

    @staticmethod
    def _get_id(url: str) -> str:
        try:
            if url.endswith("/"):
                url = url.removesuffix("/")
            redgif_id = re.match(r".*/(.*?)(?:#.*|\?.*|\..{0,})?$", url).group(1).lower()
            if redgif_id.endswith("-mobile"):
                redgif_id = redgif_id.removesuffix("-mobile")
        except AttributeError:
            raise SiteDownloaderError(f"Could not extract Redgifs ID from {url}")
        return redgif_id

    @staticmethod
    def _url_is_accessible(url: str, headers: dict, timeout: int) -> bool:
        # Only the status is needed; the body (the whole video) is downloaded later by the Resource
        try:
            with requests.get(url, headers=headers, timeout=timeout, stream=True) as response:
                return response.ok
        except requests.exceptions.RequestException as e:
            raise SiteDownloaderError(f"Could not access Redgifs URL {url}: {e}")

    @classmethod
    def _get_token(cls, renew: bool = False) -> str:
        with cls._token_lock:
            if renew or not cls._token:
                token = json.loads(cls.retrieve_url("https://api.redgifs.com/v2/auth/temporary").text)["token"]
                if not token:
                    raise SiteDownloaderError("Unable to retrieve Redgifs API token")
                cls._token = token
            return cls._token

    @staticmethod
    def _headers(token: str) -> dict:
        return {
            "referer": "https://www.redgifs.com/",
            "origin": "https://www.redgifs.com",
            "content-type": "application/json",
            "Authorization": f"Bearer {token}",
        }

    @staticmethod
    def _get_link(url: str) -> set[str]:
        redgif_id = Redgifs._get_id(url)

        headers = Redgifs._headers(Redgifs._get_token())
        try:
            content = Redgifs.retrieve_url(f"https://api.redgifs.com/v2/gifs/{redgif_id}", headers=headers)
        except ResourceNotFound as e:
            if "responded with 401" not in str(e) and "responded with 403" not in str(e):
                raise
            # The shared token expired; get a new one
            headers = Redgifs._headers(Redgifs._get_token(renew=True))
            content = Redgifs.retrieve_url(f"https://api.redgifs.com/v2/gifs/{redgif_id}", headers=headers)

        if content is None:
            raise SiteDownloaderError("Could not read the page source")

        try:
            response_json = json.loads(content.text)
        except json.JSONDecodeError as e:
            raise SiteDownloaderError(f"Received data was not valid JSON: {e}")

        out = set()
        try:
            if response_json["gif"]["type"] == 1:  # type 1 is a video
                # Some videos only have an SD version, without an "hd" key
                sd_url = response_json["gif"]["urls"]["sd"]
                hd_url = response_json["gif"]["urls"].get("hd") or sd_url
                try:
                    if Redgifs._url_is_accessible(hd_url, headers, Redgifs.REQUEST_TIMEOUT):
                        out.add(hd_url)
                    elif hd_url != sd_url and Redgifs._url_is_accessible(sd_url, headers, Redgifs.REQUEST_TIMEOUT):
                        out.add(sd_url)
                    else:
                        raise SiteDownloaderError(
                            f"Neither HD nor SD Redgifs media URLs were reachable: {hd_url} / {sd_url}"
                        )
                except SiteDownloaderError as e:
                    raise SiteDownloaderError(f"Failed to verify Redgifs media URL {hd_url}: {e}")
            elif response_json["gif"]["type"] == 2:  # type 2 is an image
                if response_json["gif"]["gallery"]:
                    content = Redgifs.retrieve_url(
                        f'https://api.redgifs.com/v2/gallery/{response_json["gif"]["gallery"]}'
                    )
                    response_json = json.loads(content.text)
                    out = {p["urls"]["hd"] for p in response_json["gifs"]}
                else:
                    urls = response_json["gif"]["urls"]
                    out.add(urls.get("hd") or urls["sd"])
            else:
                raise KeyError
        except (KeyError, AttributeError, TypeError):
            raise SiteDownloaderError(f"Failed to find JSON data in page: {content.text[:200]}")

        # Update subdomain if old one is returned
        out = {re.sub("thumbs2", "thumbs3", link) for link in out}
        out = {re.sub("thumbs3", "thumbs4", link) for link in out}
        return out
