#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import logging
import re
import time
from collections import Counter
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import requests

from bdfr import __version__, logs
from bdfr.exceptions import ArcticShiftError
from bdfr.post import Post

logger = logging.getLogger(__name__)


class ArcticShift:
    """Reads submissions from the Arctic Shift archive (https://arctic-shift.photon-reddit.com) instead of Reddit's API.

    Requests are kept to a minimum: listings are fetched one page at a time and only when the previous page has been
    used up, so stopping early (--stop-at-existing, --limit) means no further requests are made.
    """

    API_URL = "https://arctic-shift.photon-reddit.com/api"
    # The API's 'fields' filter rejects gallery_data, secure_media and other fields BDFR needs, so full posts are
    # fetched
    MAX_PAGE_SIZE = 100
    MAX_IDS_PER_REQUEST = 100
    MAX_RETRIES = 4
    MIN_REQUEST_INTERVAL = 1.0
    REDDIT_HOSTS = ("reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com", "np.reddit.com", "v.redd.it")

    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = f"bdfr/{__version__} (personal archiving)"
        self.request_count = 0
        # Posts looked up by ID with why each needed the archive, and listing pages fetched; for the run summary
        self.looked_up: dict[str, str] = {}
        self.listing_pages = 0
        # IDs of posts whose data came from the archive, and of posts looked up by ID that it doesn't have
        self.from_archive: set[str] = set()
        self.not_found: set[str] = set()
        self._last_request = 0.0

    def submissions(
        self, limit: Optional[int] = None, after: Optional[int] = None, before: Optional[int] = None, **query: str
    ) -> Iterator[Post]:
        """Yields submissions newest first for a query such as author=name or subreddit=name, created after `after`
        and before `before` (epoch seconds) when given"""
        params = dict(query, sort="desc")
        # 'auto' lets the server return as many posts as it can in one request, usually more than the fixed maximum
        params["limit"] = limit if limit and limit <= self.MAX_PAGE_SIZE else "auto"
        if after:
            params["after"] = after
        if before:
            params["before"] = before
        seen = set()
        while True:
            page = self._get("posts/search", dict(params))
            self.listing_pages += 1
            new_posts = [post for post in page if post.get("id") not in seen]
            if not new_posts:
                if page and params.get("before") == int(min(post["created_utc"] for post in page)) + 1:
                    # A whole page shared one timestamp; step past it instead of fetching it again
                    params["before"] -= 1
                    continue
                return
            for post in new_posts:
                seen.add(post["id"])
                self.from_archive.add(post["id"])
                yield self.to_submission(post)
                if limit and len(seen) >= limit:
                    return
            if isinstance(params["limit"], int) and len(page) < params["limit"]:
                return
            # Posts sharing the oldest timestamp may straddle pages, so overlap by a second and skip ones already seen
            params["before"] = int(min(post["created_utc"] for post in page)) + 1

    def submissions_from_posts(self, posts: Iterable[dict], needs_lookup=None, lookups: bool = True) -> Iterator[Post]:
        """Yields submissions for posts known only by ID or by partial data, newest first.

        Posts are looked up in batches, only when reached, and only if needs_lookup(post) says the data is not enough
        to download from on its own. needs_lookup returns why a post needs the archive, or None. Without lookups,
        nothing is requested: such posts are used as given (and fail to download if they can't be), or skipped when
        they lack the basic fields.
        """
        needs_lookup = needs_lookup or self.needs_lookup
        ordered = sorted(posts, key=lambda post: post.get("created_utc") or 0, reverse=True)
        for start in range(0, len(ordered), self.MAX_IDS_PER_REQUEST):
            batch = ordered[start : start + self.MAX_IDS_PER_REQUEST]
            reasons = {post["id"]: reason for post in batch if (reason := needs_lookup(post))}
            lookup_ids = list(reasons)
            found = {}
            if lookup_ids and not lookups:
                self._log_reasons(
                    f"Not looking up {len(reasons)} of {len(batch)} posts (archive lookups off):", reasons
                )
            elif lookup_ids:
                self._log_lookup(reasons, len(batch))
                found = {post["id"]: post for post in self._get("posts/ids", {"ids": ",".join(lookup_ids)})}
                missing = [post_id for post_id in lookup_ids if post_id not in found]
                if missing:
                    self.not_found.update(missing)
                    logger.info(f"Arctic Shift doesn't have {len(missing)} of them: {', '.join(missing)}")
            else:
                logger.info(f"All {len(batch)} posts have enough data to download; no Arctic Shift lookup needed")
            for post in batch:
                data = found.get(post["id"])
                if data is None:
                    if not all(post.get(key) for key in ("url", "title", "author", "subreddit", "created_utc")):
                        where = "in Arctic Shift" if lookups else "in the given data (archive lookups off)"
                        logger.warning(f"Submission {post['id']} not found {where}, skipping")
                        continue
                    if post["id"] in lookup_ids and lookups:
                        logger.debug(f"Submission {post['id']} not found in Arctic Shift, using the supplied data")
                    data = post
                else:
                    self.from_archive.add(post["id"])
                yield self.to_submission(data)

    def _log_lookup(self, reasons: dict[str, str], batch_size: int):
        self.looked_up.update(reasons)
        self._log_reasons(f"Looking up {len(reasons)} of {batch_size} posts in Arctic Shift:", reasons)

    @staticmethod
    def _log_reasons(heading: str, reasons: dict[str, str]):
        logger.info(heading)
        by_reason = {}
        for post_id, reason in reasons.items():
            by_reason.setdefault(reason, []).append(post_id)
        for reason, ids in by_reason.items():
            logger.info(f"  {reason} ({len(ids)}): {', '.join(ids)}")

    @classmethod
    def needs_lookup(cls, post: dict) -> Optional[str]:
        """Why a post needs its full data from the archive, or None. Posts linking straight to media can be
        downloaded from the supplied data alone, but galleries, videos, self posts and crossposts can't"""
        url = post.get("url")
        if not url:
            return "no URL in the post data"
        missing = [key for key in ("title", "author", "subreddit", "created_utc") if not post.get(key)]
        if missing:
            return f"post data lacks {', '.join(missing)}"
        parts = urlsplit(url)
        host = parts.netloc.lower()
        if host == "v.redd.it":
            # Downloaded from the playlist at a fixed path; see VReddit._find_video_url
            return None
        if host not in cls.REDDIT_HOSTS:
            return None
        if "/gallery/" in parts.path:
            # The userscript adds gallery_data, read from Reddit in the browser
            if post.get("gallery_data"):
                return None
            # bdfr_note: why the userscript couldn't read it from Reddit
            return f"gallery ({post['bdfr_note']})" if post.get("bdfr_note") else "gallery"
        if f"/comments/{post['id']}/" in parts.path + "/":
            return "self post"
        return "crosspost or link to another Reddit post"

    @staticmethod
    def to_submission(data: dict) -> Post:
        return Post(data)

    def _get(self, endpoint: str, params: dict) -> list[dict]:
        error = None
        for attempt in range(self.MAX_RETRIES + 1):
            if attempt:
                delay = self._retry_delay(error, attempt)
                logger.warning(f"Arctic Shift request failed ({error[0]}), retrying in {delay} seconds")
                time.sleep(delay)
            wait = self._last_request + self.MIN_REQUEST_INTERVAL - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                response = self.session.get(f"{self.API_URL}/{endpoint}", params=params, timeout=120)
            except requests.RequestException as e:
                error = (str(e), None)
                continue
            self.request_count += 1
            elapsed = time.monotonic() - self._last_request
            described = f"Arctic Shift request {self.request_count}: {endpoint} {self._describe_params(params)}"
            logger.log(logs.TRACE, f"Arctic Shift request {self.request_count} URL: {response.url}")
            try:
                payload = response.json()
            except ValueError:
                payload = None
            # An answer without "data" or "error" (e.g. a proxy's HTML error page) is not an empty result
            valid = isinstance(payload, dict) and ("data" in payload or "error" in payload)
            message = payload.get("error") if valid else None
            if response.status_code == 200 and valid and not message:
                data = payload.get("data") or []
                logger.info(f"{described} -> {len(data)} posts in {elapsed:.1f}s")
                return data
            if response.status_code == 200 and not valid:
                message = "the answer was not Arctic Shift's JSON"
            logger.info(f"{described} -> HTTP {response.status_code}{f' ({message})' if message else ''}")
            error = (message or f"HTTP {response.status_code}", response)
            # The server reports overload as an error message such as "Timeout. Maybe slow down a bit"
            retryable = (
                response.status_code in (429, 500, 502, 503, 504)
                or "timeout" in str(message).lower()
                or (response.status_code == 200 and not valid)
            )
            if not retryable:
                raise ArcticShiftError(f"Arctic Shift rejected the request: {error[0]}")
        raise ArcticShiftError(f"Arctic Shift request failed after {self.MAX_RETRIES + 1} attempts: {error[0]}")

    @staticmethod
    def _describe_params(params: dict) -> str:
        parts = []
        for key, value in params.items():
            if key == "ids":
                parts.append(f"{value.count(',') + 1} IDs")
            elif key in ("before", "after") and isinstance(value, int):
                parts.append(f"{key} {datetime.fromtimestamp(value):%Y-%m-%d %H:%M}")
            elif key != "sort":
                parts.append(f"{key}={value}")
        return f"({', '.join(parts)})"

    def summary(self) -> str:
        if not self.request_count:
            return "Made no requests to Arctic Shift"
        details = []
        if self.listing_pages:
            details.append(f"{self.listing_pages} listing page{'s' if self.listing_pages != 1 else ''}")
        if self.looked_up:
            counts = Counter(self.looked_up.values())
            details.append(
                f"{len(self.looked_up)} posts looked up: " + ", ".join(f"{n} {reason}" for reason, n in counts.items())
            )
        noun = "request" if self.request_count == 1 else "requests"
        return f"Made {self.request_count} {noun} to Arctic Shift ({'; '.join(details)})"

    @staticmethod
    def _retry_delay(error: tuple, attempt: int) -> int:
        response = error[1]
        if response is not None:
            reset = response.headers.get("x-ratelimit-reset") or response.headers.get("retry-after")
            if response.status_code == 429 and reset and reset.isdigit():
                return int(reset) + 1
        return 10 * 2 ** (attempt - 1)


def read_post_json(paths: Iterable[str]) -> list[dict]:
    """Reads post lists exported by scripts/tampermonkey/bdfr-export.user.js. Also accepts a plain list of posts,
    Arctic Shift responses ({"data": [...]}) and Reddit listings ({"data": {"children": [...]}})"""
    posts = {}
    for path in paths:
        path = Path(path).expanduser()
        try:
            with path.open(encoding="utf-8") as file:
                content = json.load(file)
        except (OSError, ValueError) as e:
            logger.error(f"Could not read post JSON {path}: {e}")
            continue
        if isinstance(content, dict):
            content = content.get("posts") or content.get("data") or []
        if isinstance(content, dict):
            content = content.get("children") or []
        count = 0
        for post in content:
            if isinstance(post, dict) and isinstance(post.get("data"), dict):
                post = post["data"]
            post_id = _normalise_id(post) if isinstance(post, dict) else None
            if not post_id:
                continue
            post = {key: value for key, value in post.items() if value is not None}
            post["id"] = post_id
            posts[post_id] = {**posts.get(post_id, {}), **post}
            count += 1
        logger.info(f"Read {count} posts from {path}")
    return list(posts.values())


def submission_id_from_link(link: str) -> Optional[str]:
    if re.fullmatch(r"(t3_)?\w+", link):
        return link.removeprefix("t3_")
    match = re.search(r"(?:/comments/|/gallery/|redd\.it/)(\w+)", link)
    return match.group(1) if match else None


def _normalise_id(post: dict) -> Optional[str]:
    post_id = str(post.get("id") or post.get("name") or "")
    post_id = post_id.removeprefix("t3_")
    return post_id if re.fullmatch(r"[a-z0-9]+", post_id) else None
