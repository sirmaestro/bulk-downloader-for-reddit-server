#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
from abc import ABC, abstractmethod
from typing import Optional

import requests

from bdfr.configuration import Configuration
from bdfr.exceptions import ResourceNotFound, SiteDownloaderError
from bdfr.post import Post
from bdfr.resource import Resource, http_session

logger = logging.getLogger(__name__)


class BaseDownloader(ABC):
    def __init__(self, post: Post, args: Configuration, typical_extension: Optional[str] = None):
        self.post = post
        self.args = args
        self.typical_extension = typical_extension

    @abstractmethod
    def find_resources(self) -> list[Resource]:
        """Return list of all un-downloaded Resources from submission"""
        raise NotImplementedError

    @staticmethod
    def retrieve_url(url: str, cookies: dict = None, headers: dict = None) -> requests.Response:
        try:
            # The thread's session, so the media download that usually follows reuses the connection
            res = http_session().get(url, cookies=cookies, headers=headers, timeout=60)
        except requests.exceptions.RequestException as e:
            logger.exception(e)
            raise SiteDownloaderError(f"Failed to get page {url}")
        if res.status_code != 200:
            raise ResourceNotFound(f"Server responded with {res.status_code} to {url}")
        return res
