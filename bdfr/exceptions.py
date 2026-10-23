#!/usr/bin/env python3
# -*- coding: utf-8 -*-


class BulkDownloaderException(Exception):
    pass


class ArcticShiftError(BulkDownloaderException):
    pass


class SiteDownloaderError(BulkDownloaderException):
    pass


class NotADownloadableLinkError(SiteDownloaderError):
    pass


class ResourceNotFound(SiteDownloaderError):
    pass


class ContentGone(ResourceNotFound):
    """The media is gone for good (the host answered 404 or 410, or the post was removed without its media list), so
    trying again with the same post data can't help"""

    pass
