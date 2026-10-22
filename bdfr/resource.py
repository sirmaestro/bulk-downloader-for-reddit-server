#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import _hashlib
import contextlib
import email.utils
import hashlib
import logging
import os
import re
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import requests

from bdfr.exceptions import BulkDownloaderException
from bdfr.post import Post

logger = logging.getLogger(__name__)

# Placeholder images that hosts redirect deleted media to; they must not be saved as the media
REMOVED_PLACEHOLDERS = frozenset({"https://i.imgur.com/removed.png"})

_thread_sessions = threading.local()

# Downloads in progress are written to files named like this in the destination folder, then renamed into place
TEMP_PREFIX = ".bdfr-download-"
TEMP_SUFFIX = ".part"
CHUNK_BYTES = 1024 * 1024


class DownloadedFile:
    """A download function's result when it wrote the media to a file (`temp_dir` given) instead of returning it"""

    def __init__(self, path: Path, md5: _hashlib.HASH):
        self.path = path
        self.md5 = md5

    @staticmethod
    def create(directory: Path) -> tuple[Path, int]:
        """A new, empty temporary file in directory and its descriptor"""
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX, dir=directory)
        return Path(name), descriptor

    @classmethod
    def copy_from(cls, source: Path, directory: Path) -> "DownloadedFile":
        """Copies source to a temporary file in directory, hashing it on the way"""
        path, descriptor = cls.create(directory)
        md5 = hashlib.md5()
        try:
            with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
                while chunk := reader.read(CHUNK_BYTES):
                    md5.update(chunk)
                    writer.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return cls(path, md5)


def http_session() -> requests.Session:
    """This thread's session, so requests to a host it has already contacted reuse the connection instead of opening
    a new one (e.g. a site downloader's check of a media URL, then the download). Sessions aren't thread-safe, hence
    one per thread; it goes when the thread ends."""
    session = getattr(_thread_sessions, "session", None)
    if session is None:
        session = _thread_sessions.session = requests.Session()
    return session


class Resource:
    def __init__(self, source_submission: Post, url: str, download_function: Callable, extension: str = None):
        self.source_submission = source_submission
        self.content: Optional[bytes] = None
        # Or, when downloaded with download_parameters' temp_dir, the temporary file holding it (see save())
        self.file: Optional[Path] = None
        self.url = url
        self.hash: Optional[_hashlib.HASH] = None
        self.extension = extension
        self.download_function = download_function
        if not self.extension:
            self.extension = self._determine_extension()

    @staticmethod
    def retry_download(url: str, headers: Optional[dict] = None) -> Callable:
        if headers:
            return lambda global_params: Resource.http_download(url, {**global_params, "headers": headers})
        return lambda global_params: Resource.http_download(url, global_params)

    def download(self, download_parameters: Optional[dict] = None):
        if download_parameters is None:
            download_parameters = {}
        if not self.content and not self.file:
            try:
                content = self.download_function(download_parameters)
            except requests.exceptions.ConnectionError as e:
                raise BulkDownloaderException(f"Could not download resource: {e}")
            except BulkDownloaderException:
                raise
            if isinstance(content, DownloadedFile):
                self.file, self.hash = content.path, content.md5
            elif content:
                self.content = content
        if not self.hash and self.content:
            self.create_hash()

    def save(self, destination: Path):
        """Puts the download at destination: renames its temporary file there, or writes its content"""
        if self.file:
            os.replace(self.file, destination)
            self.file = None
        else:
            with destination.open("wb") as file:
                file.write(self.content)

    def discard(self):
        """Deletes the temporary file of a download that isn't kept (e.g. a duplicate)"""
        if self.file:
            with contextlib.suppress(OSError):
                self.file.unlink(missing_ok=True)
            self.file = None

    def create_hash(self):
        self.hash = hashlib.md5(self.content)

    def _determine_extension(self) -> Optional[str]:
        extension_pattern = re.compile(r".*(\..{3,5})$")
        stripped_url = urllib.parse.urlsplit(self.url).path
        match = re.search(extension_pattern, stripped_url)
        if match:
            return match.group(1)

    @staticmethod
    def http_download(url: str, download_parameters: dict) -> Union[bytes, DownloadedFile]:
        headers = download_parameters.get("headers")
        # Given a folder, the response is streamed into a temporary file there rather than held in memory
        temp_dir = download_parameters.get("temp_dir")
        fail_fast = download_parameters.get("fail_fast", False)
        current_wait_time = 60 if not fail_fast else 5
        if "max_wait_time" in download_parameters and not fail_fast:
            max_wait_time = download_parameters["max_wait_time"]
        else:
            max_wait_time = 300 if not fail_fast else 10
        while True:
            retry_after = None
            try:
                response = http_session().get(url, headers=headers, timeout=120, stream=temp_dir is not None)
                if response.history and response.url in REMOVED_PLACEHOLDERS:
                    response.close()
                    raise BulkDownloaderException(f"Removed from the host (redirected to {response.url})")
                if re.match(r"^2\d{2}", str(response.status_code)):
                    if temp_dir is None:
                        if response.content:
                            return response.content
                    elif downloaded := Resource._stream_to_file(response, Path(temp_dir)):
                        return downloaded
                    raise BulkDownloaderException(
                        f"Unrecoverable error requesting resource: HTTP Code {response.status_code} with no content"
                    )
                response.close()
                if response.status_code in (408, 429):
                    retry_after = Resource._retry_after(response)
                    raise requests.exceptions.ConnectionError(f"Response code {response.status_code}")
                else:
                    raise BulkDownloaderException(
                        f"Unrecoverable error requesting resource: HTTP Code {response.status_code}"
                    )
            except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
                if fail_fast and ("[Errno 11001]" in str(e) or "[WinError 10061]" in str(e)):
                    # getaddrinfo failed #No connection could be made because the target machine actively refused it
                    raise
                # Only waits when another attempt follows, and gives up at once when the host asks for a longer wait
                # (Retry-After) than allowed
                if current_wait_time > max_wait_time or (retry_after is not None and retry_after > max_wait_time):
                    logger.error(f"Max wait time exceeded for resource at url {url}")
                    raise
                wait = current_wait_time if retry_after is None else retry_after
                logger.warning(f"Error occured downloading from {url}, waiting {wait:g} seconds: {e}")
                time.sleep(wait)
                current_wait_time += 60 if not fail_fast else 5

    @staticmethod
    def _stream_to_file(response: requests.Response, directory: Path) -> Optional[DownloadedFile]:
        """Writes the response's body to a temporary file in directory, hashing it on the way; None if it's empty.
        The file is deleted if the body can't be read to its end."""
        path, descriptor = DownloadedFile.create(directory)
        md5, size = hashlib.md5(), 0
        try:
            with response, os.fdopen(descriptor, "wb") as file:
                for chunk in response.iter_content(CHUNK_BYTES):
                    md5.update(chunk)
                    file.write(chunk)
                    size += len(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        if not size:
            path.unlink(missing_ok=True)
            return None
        return DownloadedFile(path, md5)

    @staticmethod
    def _retry_after(response: requests.Response) -> Optional[float]:
        """The seconds a 408 or 429 response's Retry-After header asks to wait, if it gives any"""
        value = response.headers.get("Retry-After", "").strip()
        if value.isdigit():
            return float(value)
        try:
            return max(0.0, (email.utils.parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except TypeError, ValueError:
            return None
