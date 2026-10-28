#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Local HTTP server behind `bdfr serve`, which runs downloads requested by the Tampermonkey script
(scripts/tampermonkey/bdfr-export.user.js). It listens on 127.0.0.1 unless given --host (e.g. in Docker), and every
request needs the token."""

import configparser
import copy
import itertools
import json
import logging
import os
import queue
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

import platformdirs

from bdfr import __version__, logs
from bdfr.arctic_shift import Outage
from bdfr.configuration import Configuration
from bdfr.downloader import (
    Downloader,
    duplicate_post_key,
    incomplete_post_key,
    post_ids_in_names,
    post_link,
    unavailable_post_key,
)
from bdfr.hash_store import HashStore

logger = logging.getLogger(__name__)

DEFAULT_PORT = 7635
MAX_BODY_BYTES = 20 * 1024 * 1024
MAX_POSTS_PER_JOB = 5000
MAX_FINISHED_JOBS = 50
# serve_log.txt rotates at this size, keeping the config's backup_log_count old files
SERVE_LOG_BYTES = 10 * 1024 * 1024
# One folder name directly under the root, so a request can't write anywhere else
FOLDER_PATTERN = re.compile(r"[\w\-. ]{1,100}")
USER_PATTERN = re.compile(r"[\w-]{1,30}")
POST_ID_PATTERN = re.compile(r"[a-z0-9]{3,13}")  # also keeps gallery indexes (_1, _2) from matching
RESULT_LABELS = {True: "existing", False: "new", None: "skipped"}
LABELS = ("new", "existing", "duplicate", "skipped", "failed", "unavailable")
# GET /log: the most lines returned, and the most of the file's end read to find them
MAX_LOG_LINES = 2000
MAX_LOG_READ_BYTES = 4 * 1024 * 1024
# A serve_log.txt line (bdfr/logs.py's FORMAT): time, level, then "job <id>, post <id>: " when either is set
LOG_LINE = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (\w+) +(?:(?:job (\w+))?(?:, )?(?:post (\w+))?: (?=\S))?(.*)")
# The dashboard's files (bdfr/dashboard/), served without the token since they hold no data: the page asks for the
# token and sends it with its own requests
DASHBOARD_FILES = {
    "/": ("dashboard.html", "text/html; charset=utf-8"),
    "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
    "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
}
DASHBOARD_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


POST_PATHS = (
    "/jobs",
    "/known",
    "/watchlist/gone",
    "/watchlist/reddit",
    "/watchlist/continue",
    "/watchlist/stop",
    "/watchlist/archive",
    "/watchlist/hidden",
    "/retries/stop",
    "/sync",
)
# What the dashboard's Folders tab counts as videos and images (anything else, e.g. text from self posts, as other)
VIDEO_SUFFIXES = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif", ".heic", ".tiff"}


class Job:
    def __init__(
        self,
        job_id: str,
        folder: str,
        posts: Optional[list[dict]] = None,
        user: Optional[str] = None,
        full_archive: bool = False,
        before: Optional[int] = None,
        after: Optional[int] = None,
        retry: bool = False,
        scheduled: bool = False,
        limit_bytes: Optional[int] = None,
        sync: bool = False,
    ):
        self.id = job_id
        self.folder = folder
        self.posts = posts
        self.user = user
        # With a user: read their whole archive, not only until already-downloaded posts
        self.full_archive = full_archive
        # With a user: only read the listing below this time (the oldest post on the page), since the page's posts
        # are already in hand
        self.before = before
        # With a user: read every post after this time, to the end (no stopping at downloaded posts), so gaps between
        # downloaded posts are filled
        self.after = after
        # Posts from the failed-post retry list (bdfr/retries.py), downloaded without archive lookups
        self.retry = retry
        # Posts the userscript's background sync read from a watched user's Reddit listing (not a page's Download)
        self.sync = sync
        # Queued by serve itself (the watchlist's archive checks, retries), so it waits behind the userscript's jobs
        self.scheduled = scheduled
        # With a user: stop the listing once this many bytes were written (the user's storage limit, or the disk's)
        self.limit_bytes = limit_bytes
        # Called with the job once it has finished, whatever the outcome (used by the scheduler)
        self.on_finish: Optional[Callable[["Job"], None]] = None
        self.state = "queued"
        self.error: Optional[str] = None
        self.counts = {label: 0 for label in LABELS}
        self.results: dict[str, str] = {}
        # When the newest post the job went through was posted (used by the scheduler to notice quiet users)
        self.newest_post: Optional[float] = None
        # Bytes written; why the listing stopped early ("limit": limit_bytes, "dead": its media was gone), and the
        # oldest listing post it went through, where a whole-archive read stopped at its limit carries on
        self.bytes_written = 0
        self.stop_reason: Optional[str] = None
        self.oldest_post: Optional[float] = None
        # The user's archive was off when the job ran, so it wasn't read (a scheduled job then records nothing)
        self.archive_skipped = False
        self.created = time.time()
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        self._lock = threading.Lock()

    def record(self, submission_id: str, result: Optional[bool], label: Optional[str] = None):
        label = label or RESULT_LABELS[result]
        with self._lock:
            self.counts[label] += 1
            self.results[submission_id] = label

    @property
    def origin(self) -> str:
        """Where the job came from, for the dashboard: "retry", "watchlist" (the scheduler) or "userscript" """
        if self.retry:
            return "retry"
        return "watchlist" if self.scheduled else "userscript"

    @property
    def purpose(self) -> str:
        """Why the job asks Arctic Shift for anything, for the record of requests and the dashboard"""
        if self.retry:
            return "retry of failed posts"
        if self.scheduled and self.user:
            if self.full_archive:
                return "watchlist: whole archive"
            return "watchlist: weekly check"
        if self.scheduled:
            return "watchlist: removed gallery lookups"
        if self.user:
            return "Download: whole archive" if self.full_archive else "Download: new posts in archive"
        return "userscript sync: lookups" if self.sync else "userscript posts: lookups"

    def summary(self) -> dict:
        """The job without its per-post results, for GET /jobs"""
        details = self.to_dict()
        del details["results"]
        details.update(
            user=self.user,
            scheduled=self.scheduled,
            origin=self.origin,
            purpose=self.purpose,
            created=self.created,
            started=self.started,
            finished=self.finished,
        )
        return details

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "folder": self.folder,
                "mode": "user" if self.user else "posts",
                "total": None if self.user else len(self.posts),
                "retry": self.retry,
                "state": self.state,
                "error": self.error,
                "counts": dict(self.counts),
                "bytes_written": self.bytes_written,
                "stop_reason": self.stop_reason,
                "results": dict(self.results),
            }


class DownloadServer:
    def __init__(self, base_args: Configuration, root: Path, token: str):
        self.base_args = base_args
        self.root = root
        self.token = token
        self.jobs: dict[str, Job] = {}
        self.current: Optional[Job] = None
        # The weekly baseline for watched users (bdfr/scheduler.py), when serve runs one
        self.scheduler = None
        # Posts that failed, tried again later (bdfr/retries.py), when serve keeps a retry list
        self.retries = None
        # Where problems are sent (bdfr/notifier.py), with --notify
        self.notifier = None
        # Every request jobs make to Arctic Shift (bdfr/request_log.py), when serve keeps that record
        self.requests = None
        # Whether Arctic Shift is down, shared by every job, so an outage pauses them all after one failed request
        self.archive_outage = Outage()
        # The userscript's background sync: when a browser last looked for due users (GET /watchlist with
        # X-BDFR-Sync), which browser, and its latest run's report (POST /sync)
        self.sync_checked: Optional[float] = None
        self.sync_browser: Optional[str] = None
        self.last_sync: Optional[dict] = None
        # serve_log.txt, which GET /log reads, when serve logs to a file
        self.log_path: Optional[Path] = None
        self.started = time.time()
        # (priority, order, job): the userscript's jobs (0) run before the scheduler's (1), each in arrival order
        self._queue: "queue.PriorityQueue[tuple[int, int, Job]]" = queue.PriorityQueue()
        self._order = itertools.count()
        self._lock = threading.Lock()
        threading.Thread(target=self._worker, name="bdfr-serve-worker", daemon=True).start()

    def _folder(self, payload) -> str:
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object")
        folder = str(payload.get("folder", "")).strip()
        if not FOLDER_PATTERN.fullmatch(folder) or folder.strip(".") == "":
            raise ValueError("'folder' must be a single folder name of letters, digits, spaces, '-', '_' or '.'")
        if self.scheduler is not None:
            # An old account's page names its own folder, which was merged into the alias's folder
            folder = self.scheduler.folder_for(folder)
        return folder

    def known_posts(self, payload: dict) -> dict:
        """Which of the given post IDs already have files in the folder, read from the file names. Works with any file
        scheme containing {POSTID}, wherever it is (e.g. old `{REDDITOR}_{TITLE}_{POSTID}` and newer
        `{DATE}_{POSTID}_{TITLE}` names in the same folder). Posts given with their link ('links': {ID: URL}) are also
        known when that link is in the folder's hash store, whatever the file scheme."""
        folder = self._folder(payload)
        ids = payload.get("ids")
        if not isinstance(ids, list) or len(ids) > MAX_POSTS_PER_JOB:
            raise ValueError(f"'ids' must be a list of up to {MAX_POSTS_PER_JOB} post IDs")
        supported = "{POSTID}" in self.base_args.file_scheme
        wanted = {i for i in ids if isinstance(i, str) and POST_ID_PATTERN.fullmatch(i)}
        found = set()
        for _dirpath, _dirnames, filenames in os.walk(self.root / folder) if supported else ():
            # The ID is a whole "_"-separated part of the name; matching only the requested IDs keeps title words from
            # counting
            names = (name for name in filenames if not HashStore._is_own_file(name))
            found.update(wanted.intersection(post_ids_in_names(names)))
        # Posts that failed after writing some of their files aren't known from their names
        found -= self._stored_keys(self.root / folder, {incomplete_post_key(i): i for i in found})
        # Posts recorded as all duplicates
        duplicates = self._stored_keys(self.root / folder, {duplicate_post_key(i): i for i in wanted - found})
        # Posts whose link was downloaded before, by any post (reposts); see Downloader._remember_post_link
        links = payload.get("links")
        by_link = set()
        if isinstance(links, dict):
            candidates = {i: url for i, url in links.items() if i in wanted and i not in found and isinstance(url, str)}
            by_link = self._known_links(self.root / folder, candidates)
        known = [i for i in ids if i in found or i in by_link]
        # Posts that can't be downloaded (media gone for good, or no downloader for the link)
        rest = wanted - found - duplicates - by_link
        unavailable = self._stored_keys(self.root / folder, {unavailable_post_key(i): i for i in rest})
        return {
            "known": known,
            "known_by_link": [i for i in ids if i in by_link],
            "duplicates": [i for i in ids if i in duplicates and i not in by_link],
            "unavailable": [i for i in ids if i in unavailable],
            "supported": supported,
        }

    @staticmethod
    def _known_links(directory: Path, links: dict[str, str]) -> set[str]:
        """Which of the posts (ID -> link) have a link in the folder's hash store. The store is read with its own
        read-only connection, so a job writing to it at the same time isn't disturbed."""
        wanted: dict[str, set[str]] = {}
        for post_id, url in links.items():
            wanted.setdefault(post_link(url), set()).add(post_id)
        return {post_id for url in DownloadServer._stored_urls(directory, list(wanted)) for post_id in wanted[url]}

    @staticmethod
    def _stored_keys(directory: Path, keys: dict[str, str]) -> set[str]:
        """The values of keys (URL-table key -> post ID) whose key is in the folder's hash store"""
        return {keys[key] for key in DownloadServer._stored_urls(directory, list(keys))}

    @staticmethod
    def removed_galleries(directory: Path) -> list[str]:
        """IDs of the folder's removed galleries whose post data had no image list (see unavailable_post_key, kind
        "nodata"), which the archive may have"""
        prefix = unavailable_post_key("")
        db_path = directory / "hashes.sqlite"
        json_path = directory / "hash_url_list.json"
        try:
            if db_path.exists():
                connection = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=5)
                try:
                    query = "SELECT url FROM hashes_url WHERE url LIKE ? AND hash LIKE 'nodata:%'"
                    keys = [row[0] for row in connection.execute(query, (prefix + "%",))]
                finally:
                    connection.close()
            elif json_path.exists():
                stored = json.loads(json_path.read_text(encoding="utf-8"))
                keys = [key for key, value in stored.items() if key.startswith(prefix) and value.startswith("nodata:")]
            else:
                return []
        except (sqlite3.Error, OSError, ValueError, AttributeError) as e:
            logger.warning(f"Could not read removed galleries in {directory}: {e}")
            return []
        return sorted(key[len(prefix) :] for key in keys)

    @staticmethod
    def _stored_urls(directory: Path, urls: list[str]) -> set[str]:
        """Which of the URLs are in the folder's hash store (hashes.sqlite, or hash_url_list.json)"""
        if not urls:
            return set()
        stored: set[str] = set()
        db_path = directory / "hashes.sqlite"
        json_path = directory / "hash_url_list.json"
        try:
            if db_path.exists():
                connection = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=5)
                try:
                    for start in range(0, len(urls), 500):
                        chunk = urls[start : start + 500]
                        query = f"SELECT url FROM hashes_url WHERE url IN ({','.join('?' * len(chunk))})"
                        stored.update(row[0] for row in connection.execute(query, chunk))
                finally:
                    connection.close()
            elif json_path.exists():
                stored = set(json.loads(json_path.read_text(encoding="utf-8"))).intersection(urls)
        except (sqlite3.Error, OSError, ValueError) as e:
            logger.warning(f"Could not read stored links in {directory}: {e}")
            return set()
        return stored

    def submit(
        self,
        payload: dict,
        on_finish: Optional[Callable[[Job], None]] = None,
        scheduled: bool = False,
        retry: bool = False,
    ) -> Job:
        """Queues a job. Scheduled jobs (the watchlist's archive checks, which can take an hour, and retries of failed
        posts) wait until no other job is queued, so the userscript's syncs and Download clicks don't wait behind
        them."""
        folder = self._folder(payload)
        posts, user = payload.get("posts"), payload.get("user")
        if posts is None and user is None:
            raise ValueError("Give 'posts', 'user' or both")
        if posts is not None:
            # With a user, the posts are the ones on the page, sent along for the data the archive may lack
            if not isinstance(posts, list) or len(posts) > MAX_POSTS_PER_JOB or (not posts and user is None):
                raise ValueError(f"'posts' must be a list of 1 to {MAX_POSTS_PER_JOB} posts")
            posts = [post for post in posts if isinstance(post, dict) and post.get("id")]
        if user is not None and (not isinstance(user, str) or not USER_PATTERN.fullmatch(user)):
            raise ValueError("'user' must be a Reddit username")
        before, after = payload.get("before"), payload.get("after")
        for name, value in (("before", before), ("after", after)):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
                raise ValueError(f"'{name}' must be a time in seconds since 1970")
        if after is not None and user is None:
            raise ValueError("'after' needs a 'user'")
        full = payload.get("archive") == "all"
        limit_bytes = payload.get("limit_bytes")
        if limit_bytes is not None and (
            not isinstance(limit_bytes, int) or isinstance(limit_bytes, bool) or limit_bytes <= 0 or user is None
        ):
            raise ValueError("'limit_bytes' must be a positive number of bytes, with a 'user'")
        sync = payload.get("source") == "sync" and user is None
        job = Job(secrets.token_hex(6), folder, posts, user, full, before, after, retry, scheduled, limit_bytes, sync)
        job.on_finish = on_finish
        if posts and self.scheduler is not None and not retry and not scheduled:
            # Posts read from Reddit by the userscript wake hibernating watched users (the scheduler's own jobs, queued
            # while it holds its lock, have no such posts)
            self.scheduler.note_posts(posts)
        with self._lock:
            self.jobs[job.id] = job
            finished = [j for j in self.jobs.values() if j.finished]
            for old in sorted(finished, key=lambda j: j.finished)[:-MAX_FINISHED_JOBS]:
                del self.jobs[old.id]
        self._queue.put((1 if scheduled else 0, next(self._order), job))
        with logs.job_context(job.id):
            logger.info(f"Queued: {self._describe(job)}")
        return job

    def note_gone(self, payload: dict) -> dict:
        """POST /watchlist/gone: the userscript found a watched user suspended or deleted on Reddit ("gone":
        "suspended" | "deleted"), or there after all (null)"""
        if self.scheduler is None:
            raise ValueError("serve has no watchlist")
        user = payload.get("user") if isinstance(payload, dict) else None
        if not isinstance(user, str) or not USER_PATTERN.fullmatch(user):
            raise ValueError("'user' must be a Reddit username")
        self.scheduler.note_gone(user, payload.get("gone"))
        return {"user": user, "gone": payload.get("gone")}

    def _watched_user(self, payload) -> str:
        if self.scheduler is None:
            raise ValueError("serve has no watchlist")
        user = payload.get("user") if isinstance(payload, dict) else None
        if not isinstance(user, str) or not USER_PATTERN.fullmatch(user):
            raise ValueError("'user' must be a Reddit username")
        return user

    def note_reddit_read(self, payload: dict) -> dict:
        """POST /watchlist/reddit: the userscript read a watched user's Reddit listing to its end ("oldest": the
        time of its oldest post), so their whole archive may be read now"""
        user = self._watched_user(payload)
        oldest = payload.get("oldest")
        if oldest is not None and (isinstance(oldest, bool) or not isinstance(oldest, (int, float))):
            raise ValueError("'oldest' must be a time in seconds since 1970")
        return self.scheduler.note_reddit_read(user, oldest)

    def continue_archive(self, payload: dict) -> dict:
        """POST /watchlist/continue: {"user", "limit_gb"} raises the storage limit of a user whose whole-archive read
        stopped at it, so it carries on (the dashboard's Continue)"""
        user = self._watched_user(payload)
        limit = payload.get("limit_gb")
        if isinstance(limit, bool) or not isinstance(limit, (int, float)) or limit <= 0:
            raise ValueError("'limit_gb' must be a positive number")
        return self.scheduler.continue_archive(user, limit)

    def stop_archive(self, payload: dict) -> dict:
        """POST /watchlist/stop: {"user"} stops a user's whole-archive read for good, leaving the weekly checks (the
        dashboard's Stop; Continue undoes it)"""
        return self.scheduler.stop_archive(self._watched_user(payload))

    def set_archive(self, payload: dict) -> dict:
        """POST /watchlist/archive: {"user", "archive": true | false} turns a user's archive on or off (the dashboard's
        toggle): off, nothing reads it for them and only the userscript's posts are downloaded"""
        user = self._watched_user(payload)
        if not isinstance(payload.get("archive"), bool):
            raise ValueError("'archive' must be true or false")
        return self.scheduler.set_archive(user, payload["archive"])

    def note_hidden(self, payload: dict) -> dict:
        """POST /watchlist/hidden: {"user", "hidden": true | false} whether the userscript can read a watched user's
        posts on Reddit (hidden: their listing answers 403, or lists nothing, while the account is there)"""
        user = self._watched_user(payload)
        if not isinstance(payload.get("hidden"), bool):
            raise ValueError("'hidden' must be true or false")
        return self.scheduler.note_hidden(user, payload["hidden"])

    def stop_retrying(self, payload: dict) -> dict:
        """POST /retries/stop: {"folder", "id"} gives up on a post on the retry list (the dashboard's Stop)"""
        if self.retries is None:
            raise ValueError("serve keeps no retry list")
        folder, post_id = (payload.get(key) if isinstance(payload, dict) else None for key in ("folder", "id"))
        if not isinstance(folder, str) or not isinstance(post_id, str):
            raise ValueError("Give the post's 'folder' and 'id'")
        return self.retries.stop(folder, post_id)

    def watchlist(self) -> dict:
        """The watched users with their baseline state, for GET /watchlist (the userscript's background sync)"""
        if self.scheduler is None:
            return {"users": [], "enabled": False}
        return self.scheduler.snapshot()

    # Checks for due users further apart than this mean no browser had old.reddit open (they come every 15 minutes)
    SYNC_GAP_SECONDS = 3600

    def note_sync_check(self, browser: str):
        """GET /watchlist from the userscript's sync (every 15 minutes while an old.reddit tab is open): logged only
        when it comes after a gap, so the log shows when no browser was syncing"""
        browser = self._browser(browser)
        now, previous = time.time(), self.sync_checked
        if previous is None:
            logger.info(f"Userscript sync: first check since serve started ({browser})")
        elif now - previous > self.SYNC_GAP_SECONDS:
            logger.info(
                f"Userscript sync: checking again after {self._span(now - previous)} without a check ({browser})"
            )
        self.sync_checked, self.sync_browser = now, browser

    def note_sync(self, payload: dict) -> dict:
        """POST /sync: the userscript's report of a sync run that read at least one user, logged in one line"""
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object")
        counts = {}
        for key in ("users", "sent", "failed", "gone", "seconds"):
            value = payload.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"'{key}' must be a number")
            counts[key] = value
        browser = self._browser(payload.get("browser"))
        parts = [
            f"read {counts['users']} user{'s' if counts['users'] != 1 else ''}",
            f"sent {counts['sent']} new posts",
        ]
        if counts["failed"]:
            parts.append(f"{counts['failed']} failed")
        if counts["gone"]:
            parts.append(f"{counts['gone']} suspended or deleted")
        how = "Sync now" if payload.get("forced") else "background"
        stopped = "; stopped before the end" if payload.get("stopped") else ""
        logger.info(
            f"Userscript sync ({how}, {browser}): {', '.join(parts)} in {self._duration(counts['seconds'])}{stopped}"
        )
        self.last_sync = {
            "time": time.time(),
            "browser": browser,
            "forced": bool(payload.get("forced")),
            "stopped": bool(payload.get("stopped")),
            **counts,
        }
        return self.last_sync

    @staticmethod
    def _browser(value) -> str:
        """The browser the userscript runs in, as it describes itself (e.g. "Firefox on Windows"), made safe to log"""
        value = re.sub(r"[^\w .()/-]", "", str(value or ""))[:60].strip()
        return value or "unknown browser"

    @staticmethod
    def _span(seconds: float) -> str:
        """A longer time span, such as 5h 12m or 2d 3h"""
        minutes = int(seconds) // 60
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)
        if days:
            return f"{days}d {hours}h"
        return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"

    def user_info(self, name: str) -> dict:
        """GET /user?name=: for the userscript's panel on a user's page: the user's /watchlist entry (None when not
        watched; a suspended or deleted user left out of the sync is its "gone" entry) and what their folder holds (None
        before anything is downloaded). Reads only the one folder."""
        if not USER_PATTERN.fullmatch(name):
            raise ValueError("'name' must be a Reddit user name")
        watched = None
        if self.scheduler is not None:
            snapshot = self.scheduler.snapshot()
            same = (entry for entry in snapshot["users"] + snapshot["gone"] if entry["name"].lower() == name.lower())
            watched = next(same, None)
        folder = (watched or {}).get("folder") or self._folder({"folder": name})
        directory = self.root / folder
        return {
            "name": name,
            "watchlist": self.scheduler is not None,
            "watched": watched,
            "folder": folder,
            "downloads": self._file_counts(directory) if directory.is_dir() else None,
        }

    def notify(self, message: str):
        """Sends a problem to --notify's URL, if one is set"""
        if self.notifier is not None:
            self.notifier.notify(message)

    def recent_jobs(self) -> dict:
        """GET /jobs: the queued, running and last MAX_FINISHED_JOBS finished jobs, newest first"""
        with self._lock:
            jobs = sorted(self.jobs.values(), key=lambda job: job.created, reverse=True)
        return {"jobs": [job.summary() for job in jobs]}

    def status(self) -> dict:
        with self._lock:
            scheduled = sum(job.state == "queued" and job.scheduled for job in self.jobs.values())
        return {
            "version": __version__,
            "root": str(self.root),
            "running": self.current.id if self.current else None,
            "queued": self._queue.qsize(),
            # Of the queued jobs, those serve queued itself (archive checks, retries)
            "scheduled": scheduled,
            "retries": self.retries.snapshot() if self.retries else None,
            # Arctic Shift is down: since when, no requests until when, and why
            "archive_down": self.archive_outage.snapshot(),
            # The userscript's background sync: its latest check for due users, and its latest run
            "sync": {"checked": self.sync_checked, "browser": self.sync_browser, "last_run": self.last_sync},
            "started": self.started,
            "notify": self.notifier is not None,
            "log": str(self.log_path) if self.log_path else None,
        }

    def request_summary(self) -> dict:
        """GET /requests: the requests jobs made to Arctic Shift, counted by why"""
        if self.requests is None:
            return {"enabled": False}
        return {"enabled": True, **self.requests.summary()}

    def retry_list(self) -> dict:
        """GET /retries: every post on the retry list"""
        return {"posts": self.retries.listing() if self.retries else [], "enabled": self.retries is not None}

    def log_tail(self, lines: int) -> dict:
        """GET /log: the last lines of serve_log.txt as entries (time, level, job, post, message; a traceback's lines
        are part of its entry's message), oldest first"""
        lines = max(1, min(lines, MAX_LOG_LINES))
        if self.log_path is None:
            return {"path": None, "entries": []}
        try:
            with open(self.log_path, "rb") as file:
                end = file.seek(0, os.SEEK_END)
                start, data = end, b""
                while start > 0 and data.count(b"\n") <= lines and end - start < MAX_LOG_READ_BYTES:
                    start = max(0, start - 64 * 1024)
                    file.seek(start)
                    data = file.read(end - start)
        except OSError as e:
            return {"path": str(self.log_path), "entries": [], "error": str(e)}
        entries: list[dict] = []
        for line in data.decode("utf-8", errors="replace").splitlines():
            if match := LOG_LINE.fullmatch(line):
                when, level, job, post, message = match.groups()
                entries.append({"time": when, "level": level, "job": job, "post": post, "message": message})
            elif entries:
                entries[-1]["message"] += "\n" + line
            elif start == 0:
                entries.append({"time": None, "level": None, "job": None, "post": None, "message": line})
            # Otherwise the end of an entry that started before what was read
        return {"path": str(self.log_path), "entries": entries[-lines:]}

    def folders(self) -> dict:
        """GET /folders: each folder under the root with its files (count, bytes, videos, images), what its hash store
        holds (files hashed, links stored, posts recorded as duplicates, unavailable or incomplete) and its posts on the
        retry list. Reads every folder and its store, so the dashboard only asks for it when shown."""
        retries: dict[str, int] = {}
        for entry in self.retries.listing() if self.retries else ():
            retries[entry["folder"].lower()] = retries.get(entry["folder"].lower(), 0) + 1
        try:
            directories = sorted(
                (entry for entry in os.scandir(self.root) if entry.is_dir() and not entry.name.startswith(".")),
                key=lambda entry: entry.name.lower(),
            )
        except OSError as e:
            return {"folders": [], "error": str(e)}
        folders = []
        for directory in directories:
            details = {"name": directory.name, "retries": retries.get(directory.name.lower(), 0)}
            details.update(self._file_counts(Path(directory.path)))
            details.update(self._store_counts(Path(directory.path)))
            folders.append(details)
        return {"folders": folders}

    @staticmethod
    def _file_counts(directory: Path) -> dict:
        """The folder's downloads (not BDFR's own files), its subfolders included: how many, their total size, how many
        are videos and images, and how many posts they're from (a post's several files differ only in their index,
        `_1`, `_2`, ..., whatever the file scheme; longer numbers, e.g. a year ending a title, aren't indexes)"""
        counts = {"file_count": 0, "bytes": 0, "videos": 0, "images": 0, "posts": 0}
        posts = set()
        for folder, _subfolders, names in os.walk(directory):
            for name in names:
                if HashStore._is_own_file(name):
                    continue
                try:
                    size = os.stat(os.path.join(folder, name)).st_size
                except OSError:
                    continue
                suffix = os.path.splitext(name)[1].lower()
                counts["file_count"] += 1
                counts["bytes"] += size
                counts["videos"] += suffix in VIDEO_SUFFIXES
                counts["images"] += suffix in IMAGE_SUFFIXES
                posts.add((folder, re.sub(r"_\d{1,3}$", "", os.path.splitext(name)[0])))
        counts["posts"] = len(posts)
        return counts

    @staticmethod
    def _store_counts(directory: Path) -> dict:
        """What a folder's hash store holds, read without writing (a job may be using it)"""
        kinds = {
            "duplicates": duplicate_post_key(""),
            "unavailable": unavailable_post_key(""),
            "incomplete": incomplete_post_key(""),
        }
        db_path = directory / "hashes.sqlite"
        json_path = directory / "hash_url_list.json"
        try:
            if db_path.exists():
                connection = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=5)
                try:
                    files = connection.execute("SELECT COUNT(*) FROM hashes").fetchone()[0]
                    counts = {"store": "sqlite", "files": files}
                    total = connection.execute("SELECT COUNT(*) FROM hashes_url").fetchone()[0]
                    for name, prefix in kinds.items():
                        # A range on the primary key, which LIKE wouldn't use
                        query = "SELECT COUNT(*) FROM hashes_url WHERE url >= ? AND url < ?"
                        counts[name] = connection.execute(query, (prefix, prefix[:-1] + ";")).fetchone()[0]
                finally:
                    connection.close()
            elif json_path.exists():
                stored = json.loads(json_path.read_text(encoding="utf-8"))
                hashes_path = directory / "hash_list.json"
                hashes = json.loads(hashes_path.read_text(encoding="utf-8")) if hashes_path.exists() else {}
                counts = {"store": "json", "files": len(hashes)}
                total = len(stored)
                for name, prefix in kinds.items():
                    counts[name] = sum(1 for key in stored if key.startswith(prefix))
            else:
                return {"store": None}
        except (sqlite3.Error, OSError, ValueError) as e:
            return {"store": "error", "error": str(e)}
        counts["links"] = total - sum(counts[name] for name in kinds)
        return counts

    def _worker(self):
        while True:
            _priority, _order, job = self._queue.get()
            self.current = job
            try:
                self._run(job)
            finally:
                self.current = None

    def _run(self, job: Job):
        # Every line logged while the job runs, in this thread and its download threads, says which job it's from
        with logs.job_context(job.id):
            self._run_job(job)

    def _run_job(self, job: Job):
        args = copy.deepcopy(self.base_args)
        args.directory = str(self.root / job.folder)
        for key in ("subreddit", "user", "link", "include_id_file", "post_json"):
            setattr(args, key, [])
        args.submitted = False
        post_file = None
        # Turned off in the dashboard: the user's archive isn't read, for any job into their folder
        archive_off = self.scheduler is not None and self.scheduler.archive_off(job.folder)
        if archive_off:
            args.archive_lookups = False
            job.archive_skipped = True
        if job.posts:
            # Every given post is checked (--stop-at-existing only applies to listings), so a run of existing posts
            # doesn't hide newer ones after it
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as file:
                json.dump({"posts": job.posts}, file)
                post_file = Path(file.name)
            args.post_json = [str(post_file)]
            if job.retry:
                # The posts' data is what the first job had, so the archive has nothing to add
                args.archive_lookups = False
        if job.user and archive_off:
            logger.info(f"The archive is off for /{job.folder}: u/{job.user}'s archive listing isn't read")
        elif job.user:
            # The page's posts are downloaded first, then the user's listing: until already-downloaded posts, or all
            # of it. Posts already in the folder are recognised from their IDs without requests, so reading the whole
            # archive mostly costs the listing's own requests.
            args.user = [job.user]
            args.submitted = True
            full = job.full_archive or job.after is not None
            args.stop_at_existing = 0 if full else (args.stop_at_existing or 5)
            args.listing_before = job.before
            args.listing_after = job.after
            if job.full_archive:
                # The whole archive, newest first, until two pages in a row have under 10% of their media left
                # (older posts are then very likely gone too), rather than a run of unavailable posts
                args.stop_at_unavailable = 0
                args.stop_at_dead_pages = 2
            args.stop_at_bytes = job.limit_bytes or 0

        job.state = "running"
        started = job.started = time.time()
        logger.info(f"Started: {self._describe(job)}")
        logger.debug(f"Options: {self._options(args)}")
        # The outcome is published once the job is cleaned up, so whoever sees it finished sees it complete
        state, error = "failed", None
        failed_posts: dict[str, tuple[dict, str]] = {}
        try:
            # Its lines go to serve_log.txt with the server's, so it opens no log file of its own
            downloader = Downloader(args, log_file=False)
            downloader.progress_callback = job.record
            downloader.arctic_shift.outage = self.archive_outage
            if self.requests is not None:
                downloader.arctic_shift.on_request = lambda entry: self._record_request(job, entry)
            failed_posts = downloader.failed_posts
            downloader.download()
            job.newest_post = downloader.newest_post
            job.bytes_written = downloader.bytes_written
            job.stop_reason = downloader.stop_reason
            job.oldest_post = downloader.oldest_listed
            if downloader.errors:
                # A listing stopped part way (e.g. Arctic Shift unavailable) must not look like nothing new
                error = "; ".join(downloader.errors)
            else:
                state = "done"
        except Exception as e:
            logger.exception(f"Unexpected error: {e}")
            error = str(e)
        finally:
            # The downloader's last line has the post counts
            took = self._duration(time.time() - started)
            if error:
                logger.warning(f"Failed after {took}: {error}")
                if not self.archive_outage.down:
                    # Nothing the owner can do about an outage; jobs carry on once the archive answers
                    self.notify(f"Job {job.id} failed ({self._describe(job)}): {error}")
            else:
                logger.info(f"Done in {took}")
            if post_file:
                post_file.unlink(missing_ok=True)
            if self.retries is not None:
                try:
                    self.retries.note_job(job.folder, dict(job.results), failed_posts)
                except Exception:
                    logger.exception("Recording failed posts for a retry failed")
            job.error = error
            job.state = state
            job.finished = time.time()
            if job.on_finish:
                try:
                    job.on_finish(job)
                except Exception:
                    logger.exception("Handling the end of the job failed")

    def _record_request(self, job: Job, entry: dict):
        entry.update(job=job.id, folder=job.folder, user=job.user, purpose=job.purpose)
        self.requests.record(entry)

    @staticmethod
    def _options(args: Configuration) -> str:
        """The download options that change what a job does"""
        flags = [
            name
            for name in (
                "no_dupes",
                "search_existing",
                "keep_hashes",
                "keep_hashes_db",
                "make_hard_links",
                "rename_existing",
            )
            if getattr(args, name, False)
        ]
        parts = [f"--{name.replace('_', '-')}" for name in flags]
        parts.append(f"--parallel {args.parallel}")
        if args.stop_at_existing:
            parts.append(f"--stop-at-existing {args.stop_at_existing}")
        parts.append(f'--file-scheme "{args.file_scheme}"')
        for name in ("min_score", "max_score", "skip_domain", "skip", "skip_subreddit", "disable_module"):
            value = getattr(args, name, None)
            if value:
                shown = ",".join(sorted(value)) if isinstance(value, (list, set)) else value
                parts.append(f"--{name.replace('_', '-')} {shown}")
        if not getattr(args, "archive_lookups", True):
            parts.append("(archive lookups off)")
        return " ".join(parts)

    @staticmethod
    def _duration(seconds: float) -> str:
        minutes, seconds = divmod(int(seconds), 60)
        return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"

    def _describe(self, job: Job) -> str:
        if job.user:
            source = f"{'all archived' if job.full_archive else 'new'} posts by u/{job.user}"
            if job.after:
                since = time.strftime("%Y-%m-%d %H:%M", time.localtime(job.after))
                source = f"all archived posts by u/{job.user} since {since}"
            if job.before:
                source += f" older than {time.strftime('%Y-%m-%d %H:%M', time.localtime(job.before))}"
            source += f" (with {len(job.posts)} from the page)" if job.posts else ""
        elif job.retry:
            source = f"{len(job.posts)} failed posts to retry"
        elif job.sync:
            source = f"{len(job.posts)} new posts from the userscript's sync"
        else:
            source = f"{len(job.posts)} posts from a page"
        return f"{source} into {self.root / job.folder}"


def make_handler(server: DownloadServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"bdfr/{__version__}"

        def log_request(self, code="-", size="-"):
            # The userscript polls job status every 2 seconds, so successful requests aren't logged
            if isinstance(code, int) and code >= 400 or str(code).startswith(("4", "5")):
                logger.warning(f"serve: {self.requestline} -> {code}")

        def log_message(self, format_string: str, *args):
            logger.debug(f"serve: {self.address_string()} {format_string % args}")

        def _send(self, status: int, body: dict):
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorised(self) -> bool:
            # No CORS headers are sent, so ordinary web pages can't read responses or send the token header
            if secrets.compare_digest(self.headers.get("X-BDFR-Token", ""), server.token):
                return True
            self._send(401, {"error": "Missing or wrong X-BDFR-Token"})
            return False

        def _send_dashboard(self, name: str, content_type: str):
            data = resources.files("bdfr").joinpath("dashboard", name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            for header, value in DASHBOARD_HEADERS.items():
                self.send_header(header, value)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in DASHBOARD_FILES:
                self._send_dashboard(*DASHBOARD_FILES[self.path])
                return
            if not self._authorised():
                return
            url = urlsplit(self.path)
            if self.path == "/status":
                self._send(200, server.status())
            elif self.path == "/watchlist":
                if self.headers.get("X-BDFR-Sync"):
                    server.note_sync_check(self.headers["X-BDFR-Sync"])
                self._send(200, server.watchlist())
            elif self.path == "/jobs":
                self._send(200, server.recent_jobs())
            elif self.path == "/retries":
                self._send(200, server.retry_list())
            elif self.path == "/requests":
                self._send(200, server.request_summary())
            elif self.path == "/folders":
                self._send(200, server.folders())
            elif url.path == "/user":
                try:
                    self._send(200, server.user_info(parse_qs(url.query).get("name", [""])[0]))
                except ValueError as e:
                    self._send(400, {"error": str(e)})
            elif url.path == "/log":
                lines = parse_qs(url.query).get("lines", ["500"])[0]
                if not lines.isdigit():
                    self._send(400, {"error": "'lines' must be a number"})
                    return
                self._send(200, server.log_tail(int(lines)))
            elif match := re.fullmatch(r"/jobs/(\w+)", self.path):
                job = server.jobs.get(match.group(1))
                self._send(200, job.to_dict()) if job else self._send(404, {"error": "No such job"})
            else:
                self._send(404, {"error": "Not found"})

        def do_POST(self):
            if not self._authorised():
                return
            if self.path not in POST_PATHS:
                self._send(404, {"error": "Not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send(413, {"error": "Request body missing or too large"})
                return
            try:
                payload = json.loads(self.rfile.read(length))
                if self.path == "/known":
                    self._send(200, server.known_posts(payload))
                elif self.path == "/watchlist/gone":
                    self._send(200, server.note_gone(payload))
                elif self.path == "/watchlist/reddit":
                    self._send(200, server.note_reddit_read(payload))
                elif self.path == "/watchlist/continue":
                    self._send(200, server.continue_archive(payload))
                elif self.path == "/watchlist/stop":
                    self._send(200, server.stop_archive(payload))
                elif self.path == "/watchlist/archive":
                    self._send(200, server.set_archive(payload))
                elif self.path == "/watchlist/hidden":
                    self._send(200, server.note_hidden(payload))
                elif self.path == "/retries/stop":
                    self._send(200, server.stop_retrying(payload))
                elif self.path == "/sync":
                    self._send(200, server.note_sync(payload))
                else:
                    self._send(202, server.submit(payload).to_dict())
            except (ValueError, json.JSONDecodeError) as e:
                self._send(400, {"error": str(e)})

    return Handler


def load_token(config_directory: Path) -> str:
    """The token is BDFR_TOKEN when set (e.g. in Docker), or kept next to BDFR's configuration, created on first use"""
    if token := os.environ.get("BDFR_TOKEN", "").strip():
        return token
    token_path = config_directory / "serve_token.txt"
    if token_path.exists() and (token := token_path.read_text(encoding="utf-8").strip()):
        return token
    config_directory.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    token_path.write_text(token, encoding="utf-8")
    return token


def _log_to_file(base_args: Configuration, config_directory: Path) -> logging.Handler:
    """Everything the server does goes to one file, serve_log.txt (or --log), rotated by size"""
    path = Path(base_args.log).expanduser() if base_args.log else config_directory / "serve_log.txt"
    config = configparser.ConfigParser()
    config.read(base_args.config or config_directory / "default_config.cfg")
    backup_count = config.getint("DEFAULT", "backup_log_count", fallback=3)
    handler = logs.serve_log_handler(path, SERVE_LOG_BYTES, backup_count, base_args.verbose or 0)
    root = logging.getLogger()
    root.setLevel(min(root.level, handler.level))
    root.addHandler(handler)
    return handler


def run_server(base_args: Configuration, port: int):
    root = Path(base_args.directory).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    config_directory = Path(platformdirs.user_config_dir("bdfr", "BDFR"))
    token = load_token(config_directory)
    log_file = _log_to_file(base_args, config_directory)
    server = DownloadServer(base_args, root, token)
    server.log_path = Path(log_file.baseFilename)
    if base_args.notify:
        from bdfr.notifier import Notifier

        server.notifier = Notifier(base_args.notify)
        logger.info(f"Problems are sent to {base_args.notify}")
    from bdfr.scheduler import BaselineScheduler

    watchlist = Path(base_args.watchlist).expanduser() if base_args.watchlist else config_directory / "watchlist.yaml"
    server.scheduler = BaselineScheduler(server, watchlist)
    server.scheduler.start()
    from bdfr.request_log import RequestLog

    server.requests = RequestLog(config_directory / "arctic_shift_requests.jsonl")
    if base_args.retry_failed:
        from bdfr.retries import RetryList

        server.retries = RetryList(server, config_directory / "failed_posts.json")
        server.retries.start()
    else:
        logger.info("Failed posts aren't retried (--retry-failed is off)")
    logger.info(f"Watched users are read from {watchlist}" + ("" if watchlist.exists() else " (not there yet)"))
    host = base_args.host or "127.0.0.1"
    httpd = ThreadingHTTPServer((host, port), make_handler(server))
    logger.info(f"Listening on http://{host}:{port}, downloading into folders under {root}")
    logger.info(f"Dashboard (needs the token): http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/")
    if host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            f"Listening on {host}: other machines on the network can reach the server. Every request needs the token, "
            "but it is sent unencrypted, so only do this on a network you trust"
        )
    logger.info(f"Token for the Tampermonkey script: {token}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping")
    finally:
        httpd.server_close()
        logging.getLogger().removeHandler(log_file)
        log_file.close()
