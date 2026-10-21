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
