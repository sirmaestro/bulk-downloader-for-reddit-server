#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import contextlib
import contextvars
import logging
import os
import re
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

from bdfr import exceptions as errors
from bdfr import logs
from bdfr.configuration import Configuration
from bdfr.connector import Connector
from bdfr.post import Post
from bdfr.resource import TEMP_PREFIX, TEMP_SUFFIX
from bdfr.site_downloaders.download_factory import DownloadFactory

logger = logging.getLogger(__name__)

# How each log_label() reads in the log
LOG_NAMES = {
    "new": "new",
    "existing": "already downloaded",
    "duplicate": "duplicate",
    "skipped": "skipped",
    "failed": "failed",
    "unavailable": "unavailable",
}


def duplicate_post_key(post_id: str) -> str:
    """The URL-table key that records a post whose media was all already downloaded under other links (the same
    content), or whose several files' links were all downloaded before by other posts, so it's known (by `bdfr serve`'s
    /known, and by later runs) without downloading it again"""
    return f"post:{post_id}"


def incomplete_post_key(post_id: str) -> str:
    """The URL-table key that records a post that failed after some of its files were written (e.g. a gallery that
    stopped part-way). Its ID is then in file names, but the post isn't known until a run finishes it."""
    return f"incomplete:{post_id}"


def unavailable_post_key(post_id: str) -> str:
    """The URL-table key that records a post that can't be downloaded: its media is gone for good (ContentGone) or its
    link has no downloader. Its value is "<kind>:<reason>"; kind "nodata" (a removed gallery whose post data had no
    image list) is tried again when the post comes with its image list, e.g. from the archive."""
    return f"unavailable:{post_id}"


def post_ids_in_names(names) -> set[str]:
    """Every "_"- or "."-separated part of the file names; a post's ID is one of them in any file scheme with
    {POSTID}"""
    parts = set()
    for name in names:
        parts.update(re.split(r"[_.]", name))
    return parts


def post_link(url: str) -> str:
    """A post's own link, in one form for hosts that have several (e.g. redgifs.com/watch/x and i.redgifs.com/i/x),
    so posts sharing a link are recognised. This is how post links are stored in the hash store's URL table."""
    match = re.match(r"https?://(?:www\.|v3\.|i\.)?redgifs\.com/(?:watch|ifr|i)/([A-Za-z]+)", url)
    if match:
        return f"https://www.redgifs.com/watch/{match.group(1).lower()}"
    return url


class Downloader(Connector):
    # With --parallel, submissions download in threads. Checking and changing the hash store, and writing files, is
    # done under this lock, so two threads can't both decide the same media is new.
    _store_lock = threading.RLock()
    # Hashes of files being written outside _store_lock, and the condition to wait on until they are stored, so a
    # second download of the same content is still recognised as a duplicate
    _writing: set[str] = set()
    _written = threading.Condition(_store_lock)
    # The hash store's rows are committed once per submission, in _run_one
    _defer_commits = True
    # What happened to the files of the submission a thread is downloading, for its summary line
    _outcome = threading.local()

    def __init__(self, args: Configuration, logging_handlers: Iterable[logging.Handler] = (), log_file: bool = True):
        super().__init__(args, logging_handlers, log_file)
        # Called with (submission ID, result of _download_submission, label from log_label) after each
        # submission, in listing order; used by `bdfr serve`
        self.progress_callback: Optional[Callable[[str, Optional[bool], str], None]] = None
        # Why listings were stopped before their end in download(); the run didn't see everything it was asked to
        self.errors: list[str] = []
        # Submissions that failed (an error, not filtered out): ID -> (post data, reason), for serve's retry list
        self.failed_posts: dict[str, tuple[dict, str]] = {}
        # When the newest submission the run went through was posted (whatever happened to it), or None
        self.newest_post: Optional[float] = None
        # Bytes of the files written (not hard links), for args.stop_at_bytes (serve's storage limit)
        self.bytes_written = 0
        # Why a listing was stopped by stop_at_bytes ("limit") or stop_at_dead_pages ("dead"), and the oldest listing
        # post the run went through, where a whole-archive read stopped at its limit carries on
        self.stop_reason: Optional[str] = None
        self.oldest_listed: Optional[float] = None

    def _stop_at_existing_reached(self, result, consecutive_existing: int) -> (bool, int):
        # result: True = already downloaded, False = newly downloaded, None = neither (filtered or failed)
        if not self.args.stop_at_existing:
            return False, consecutive_existing
        if result is True:
            consecutive_existing += 1
        elif result is False:
            consecutive_existing = 0
        if consecutive_existing >= self.args.stop_at_existing:
            logger.info(f"{consecutive_existing} already downloaded posts in a row; stopping this listing")
            return True, consecutive_existing
        return False, consecutive_existing

    def _stop_at_unavailable_reached(self, label: str, consecutive: int) -> (bool, int):
        """--stop-at-unavailable: a run of posts whose media is gone (deleted, removed) usually means the user deleted
        their older posts, so the rest of the listing, older still, is very likely gone too. Posts with media count
        against the run; filtered, failed and already-downloaded posts leave it as it is."""
        if not self.args.stop_at_unavailable:
            return False, consecutive
        if label == "unavailable":
            consecutive += 1
        elif label in ("new", "duplicate"):
            consecutive = 0
        if consecutive >= self.args.stop_at_unavailable:
            logger.info(
                f"{consecutive} posts in a row whose media is gone; stopping this listing (older posts are very "
                "likely gone too)"
            )
            return True, consecutive
        return False, consecutive

    # A page of a listing, for stop_at_dead_pages, and the share of its posts with media that must still have it
    DEAD_PAGE_POSTS = 100
    DEAD_PAGE_SHARE = 0.10

    def _dead_page(self, page: Counter) -> bool:
        """Whether a page of a listing had under DEAD_PAGE_SHARE of its posts with media still there (downloaded now,
        already, or as a duplicate) rather than gone (unavailable); a page without either isn't dead"""
        seen = page["alive"] + page["gone"]
        return bool(seen) and page["alive"] / seen < self.DEAD_PAGE_SHARE

    def download(self):
        self._remove_stale_downloads()
        totals = Counter()
        number = 0
        done = set()
        workers = max(1, self.args.parallel or 1)
        pool = ThreadPoolExecutor(workers, thread_name_prefix="bdfr-download") if workers > 1 else None
        try:
            for generator in self.listings:
                consecutive_existing = 0
                consecutive_unavailable = 0
                page: Counter = Counter()
                dead_pages = 0
                # Given posts (--post-json, --link) are all checked; --stop-at-existing is for listings
                stop_enabled = generator is not getattr(self, "post_list", None)
                # Submissions being downloaded in threads, oldest first; results are reported in this order
                pending: deque = deque()
                stopped = False

                def finish(submission: Post, run: tuple) -> bool:
                    """Reports a finished submission; returns whether --stop-at-existing ends the listing"""
                    nonlocal number, consecutive_existing, consecutive_unavailable, page, dead_pages
                    result, outcome = run
                    number += 1
                    self.bytes_written += outcome["files"]["bytes"]
                    self._log_outcome(number, submission, result, outcome)
                    label = self.log_label(result, outcome)
                    totals[label] += 1
                    # Failures that would happen again with the same post data aren't kept for a retry
                    if label == "failed" and not outcome["permanent"]:
                        self.failed_posts[submission.id] = (submission.to_dict(), outcome["skip_reason"])
                    totals["files"] += outcome["files"]["written"] + outcome["files"]["linked"]
                    created = getattr(submission, "created_utc", None)
                    if isinstance(created, (int, float)) and (self.newest_post is None or created > self.newest_post):
                        self.newest_post = created
                    if self.progress_callback:
                        self.progress_callback(submission.id, result, label)
                    if not stop_enabled:
                        return False
                    if isinstance(created, (int, float)) and (
                        self.oldest_listed is None or created < self.oldest_listed
                    ):
                        self.oldest_listed = created
                    if self.args.stop_at_bytes and self.bytes_written >= self.args.stop_at_bytes:
                        logger.info(
                            f"{self.bytes_written / 1e9:.1f} GB written, the storage limit; stopping this listing"
                        )
                        self.stop_reason = "limit"
                        return True
                    if self.args.stop_at_dead_pages:
                        page["posts"] += 1
                        if label in ("new", "existing", "duplicate"):
                            page["alive"] += 1
                        elif label == "unavailable":
                            page["gone"] += 1
                        if page["posts"] >= self.DEAD_PAGE_POSTS:
                            dead_pages = dead_pages + 1 if self._dead_page(page) else 0
                            page = Counter()
                            if dead_pages >= self.args.stop_at_dead_pages:
                                logger.info(
                                    f"{dead_pages} pages in a row with under {self.DEAD_PAGE_SHARE:.0%} of their media "
                                    "left; stopping this listing (older posts are very likely gone too)"
                                )
                                self.stop_reason = "dead"
                                return True
                    stop, consecutive_unavailable = self._stop_at_unavailable_reached(label, consecutive_unavailable)
                    if stop:
                        return True
                    stop, consecutive_existing = self._stop_at_existing_reached(result, consecutive_existing)
                    return stop

                try:
                    for submission in generator:
                        if submission.id in done:
                            # Already handled in this run, e.g. a given post that is also in a user's listing; not
                            # counted towards --stop-at-existing, since this run may just have downloaded it
                            continue
                        done.add(submission.id)
                        if pool is None:
                            if finish(submission, self._run_one(submission)):
                                stopped = True
                                break
                            continue
                        # In a copy of this thread's context, so its log lines carry the job
                        context = contextvars.copy_context()
                        pending.append((submission, pool.submit(context.run, self._run_one, submission)))
                        # Stay a few submissions ahead of the oldest unfinished one. Reading further ahead could fetch
                        # listing pages that --stop-at-existing would never need.
                        while pending and (len(pending) >= workers * 2 or pending[0][1].done()):
                            finished, future = pending.popleft()
                            if finish(finished, future.result()):
                                stopped = True
                                break
                        if stopped:
                            break
                except errors.ArcticShiftError as e:
                    logger.error(f"Stopped reading a listing: {e}")
                    self.errors.append(str(e))
                finally:
                    # Submissions already started are finished and reported; once the listing is stopped, ones not
                    # started yet are dropped
                    while pending:
                        finished, future = pending.popleft()
                        if stopped and future.cancel():
                            continue
                        if finish(finished, future.result()):
                            stopped = True
        except Exception as e:
            logger.exception(f"Stopped by an unexpected error: {e}")
            self.errors.append(f"Unexpected error: {e}")
        finally:
            if pool:
                pool.shutdown(wait=True, cancel_futures=True)
        # "unavailable" only when there are any, so the line stays as it was otherwise
        counts = ", ".join(
            f"{totals[label]} {name}" for label, name in LOG_NAMES.items() if label != "unavailable" or totals[label]
        )
        files = totals["files"]
        logger.info(f"Finished {number} posts: {counts}; {files} file{'s' if files != 1 else ''} written")
        logger.info(self.arctic_shift.summary())
        self._note_not_in_archive()
        self._hash_list_save(False)
        self.close_hash_db()

    # Downloads in progress older than this are left from a run that was stopped
    STALE_DOWNLOAD_SECONDS = 24 * 3600

    def _remove_stale_downloads(self):
        """Deletes temporary download files a stopped run left in the download folder"""
        cutoff = time.time() - self.STALE_DOWNLOAD_SECONDS
        for path in Path(self.download_directory).glob(f"{TEMP_PREFIX}*{TEMP_SUFFIX}"):
            with contextlib.suppress(OSError):
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    logger.debug(f"Deleted {path.name}, left by a stopped run")

    @staticmethod
    def result_label(result: Optional[bool], outcome: dict) -> str:
        """ "new", "existing", "duplicate" (nothing written because all its media was already downloaded under other
        links) or "skipped" (filtered out, failed or nothing to download)"""
        if result is False:
            return "new"
        if result is True:
            return "existing"
        files = outcome["files"]
        if files["duplicate"] and not outcome["skip_reason"]:
            return "duplicate"
        return "skipped"

    @classmethod
    def log_label(cls, result: Optional[bool], outcome: dict) -> str:
        """result_label, but "failed" (an error) or "unavailable" (its media is gone for good, or its link has no
        downloader) rather than "skipped" (filtered out or nothing to download)"""
        if result is None and outcome["unavailable"]:
            return "unavailable"
        if result is None and outcome["failed"]:
            return "failed"
        return cls.result_label(result, outcome)

    def _run_one(self, submission: Post) -> tuple[Optional[bool], dict]:
        """Downloads one submission (in a download thread with --parallel); returns its result and what happened to
        its files"""
        self._outcome.state = self._new_state()
        result = None
        with logs.post_context(submission.id):
            try:
                result = self._download_submission(submission)
            except Exception as e:
                # One bad submission should not end the whole run
                logger.exception(f"Unexpected error: {e}")
                self._fail(f"unexpected error: {e}")
            finally:
                with self._store_lock:
                    self._note_incomplete(submission)
                    self._note_unavailable(submission)
                    self._commit_hash_db()
        return result, self._outcome.state

    def _note_incomplete(self, submission: Post):
        """Records a submission that failed after writing some of its files as incomplete, so it isn't taken as
        downloaded from its ID in their names; forgets that once a run of it doesn't fail (under _store_lock)"""
        key = incomplete_post_key(submission.id)
        if self._state()["failed"]:
            if self._files["written"] or self._files["linked"]:
                self._check_url_exists_or_add(key, "incomplete")
        elif self._check_url_exists_or_add(key, None):
            self._forget_url(key)

    def _note_unavailable(self, submission: Post):
        """Records a submission that can't be downloaded (see unavailable_post_key), so later runs and /known skip it
        without requests; forgets that once a run writes its files (under _store_lock)"""
        key = unavailable_post_key(submission.id)
        value = self._state()["unavailable"]
        if value:
            stored = self._get_url_hash(key)
            if stored != value:
                # e.g. a removed gallery looked up in the archive, which has no image list either
                if stored:
                    self._forget_url(key)
                self._check_url_exists_or_add(key, value)
        elif (self._files["written"] or self._files["linked"]) and self._check_url_exists_or_add(key, None):
            self._forget_url(key)

    def _note_not_in_archive(self):
        """Removed galleries looked up by ID that the archive doesn't have can't be had anywhere: they're recorded as
        gone, so they aren't looked up again"""
        with self._store_lock:
            for post_id in sorted(self.arctic_shift.not_found):
                key = unavailable_post_key(post_id)
                stored = self._get_url_hash(key)
                if stored and stored.startswith("nodata:"):
                    self._forget_url(key)
                    self._check_url_exists_or_add(key, f"gone:{stored.partition(':')[2]}; not in the archive")
            self._commit_hash_db()

    def _known_removed(self, submission: Post) -> bool:
        """Whether the post is recorded as a removed gallery whose post data had no image list"""
        with self._store_lock:
            stored = self._get_url_hash(unavailable_post_key(submission.id))
        return bool(stored) and stored.startswith("nodata:")

    def _from_archive(self, submission: Post) -> bool:
        """Whether the submission's data came from the archive (a listing, or a lookup by ID)"""
        return submission.id in self.arctic_shift.from_archive

    @staticmethod
    def _new_state() -> dict:
        return {
            "files": Counter(),
            "skip_reason": None,
            "failed": False,
            # Failed in a way a retry with the same post data can't change
            "permanent": False,
            # "<kind>:<reason>" when the post can't be downloaded (see unavailable_post_key)
            "unavailable": None,
            "duplicates_of": [],
            "module": None,
        }

    def _state(self) -> dict:
        if not hasattr(self._outcome, "state"):
            self._outcome.state = self._new_state()
        return self._outcome.state

    @property
    def _files(self) -> Counter:
        return self._state()["files"]

    @property
    def _duplicates_of(self) -> list[str]:
        return self._state()["duplicates_of"]

    @property
    def _skip_reason(self) -> Optional[str]:
        return self._state()["skip_reason"]

    @_skip_reason.setter
    def _skip_reason(self, reason: Optional[str]):
        self._state()["skip_reason"] = reason

    @property
    def _module(self) -> Optional[str]:
        return self._state()["module"]

    @_module.setter
    def _module(self, name: Optional[str]):
        self._state()["module"] = name

    def _skip(self, reason: str) -> None:
        """Ends the submission as filtered out or with nothing to download"""
        self._skip_reason = reason

    def _fail(self, reason: str, permanent: bool = False) -> None:
        """Ends the submission as failed, which is logged as a warning. A permanent failure isn't retried."""
        self._skip_reason = reason
        self._state()["failed"] = True
        self._state()["permanent"] = permanent

    def _unavailable(self, reason: str, kind: str = "gone") -> None:
        """Ends the submission as one that can't be downloaded (see unavailable_post_key)"""
        self._skip_reason = reason
        self._state()["unavailable"] = f"{kind}:{reason}"

    def _log_outcome(self, number: int, submission: Post, result: Optional[bool], outcome: dict):
        """One line per submission saying what happened to it and why, e.g. `new - 1 file written (#1, r/pics,
        Direct, posted 2026-10-27 14:05)`; a warning when it failed. The per-file lines are debug, or trace where this
        line already says the same."""
        files = outcome["files"]
        parts = []
        if files["written"]:
            parts.append(f"{files['written']} file{'s' if files['written'] != 1 else ''} written")
        if files["linked"]:
            parts.append(f"{files['linked']} hard-linked to earlier downloads")
        if files["renamed"]:
            parts.append(f"{files['renamed']} renamed to the current file scheme")
        if files["existing"]:
            parts.append(f"{files['existing']} already in the folder")
        if files["known_post"]:
            parts.append("files with its ID already in the folder")
        if files["known_duplicate"]:
            parts.append("its media was all duplicates before")
        if files["known_link"]:
            parts.append(f"{files['known_link']} link{'s' if files['known_link'] != 1 else ''} downloaded before")
        if files["duplicate"]:
            duplicates_of = outcome["duplicates_of"]
            names = ", ".join(duplicates_of[:3]) + (", ..." if len(duplicates_of) > 3 else "")
            parts.append(f"{files['duplicate']} same as already downloaded {names}".rstrip())
        if files["filtered"]:
            parts.append(f"{files['filtered']} removed by the download filter")
        if outcome["skip_reason"]:
            parts.append(outcome["skip_reason"])
        if result is None and not parts:
            parts.append("nothing to download")
        details = [f"#{number}"]
        if submission.subreddit:
            details.append(f"r/{submission.subreddit}")
        if outcome["module"]:
            details.append(outcome["module"])
        created = getattr(submission, "created_utc", None)
        if isinstance(created, (int, float)) and not isinstance(created, bool) and created > 0:
            # How long posts last before their media goes is what the sync's timing rests on
            details.append(f"posted {time.strftime('%Y-%m-%d %H:%M', time.localtime(created))}")
        label = self.log_label(result, outcome)
        with logs.post_context(submission.id):
            logger.log(
                logging.WARNING if label == "failed" else logging.INFO,
                f"{LOG_NAMES[label]} - {'; '.join(parts)} ({', '.join(details)})",
            )

    @staticmethod
    def _post_link(submission: Post) -> str:
        return post_link(submission.url)

    def _post_id_known(self, submission: Post) -> bool:
        """Whether the post was handled before: its ID is in a file name in the download folder, or it was recorded as
        all duplicates. Checked before anything is requested, so going through a long listing of mostly downloaded
        posts (e.g. a user's whole archive) costs no requests to the media hosts."""
        if self.args.rename_existing:
            # Renaming needs each resource's new destination, so the post goes through the normal checks
            return False
        with self._store_lock:
            if self._incomplete(submission):
                return False
            if submission.id in self._folder_post_ids():
                self._files["known_post"] += 1
                return True
            if self._check_url_exists_or_add(duplicate_post_key(submission.id), None):
                self._files["known_duplicate"] += 1
                return True
        return False

    def _post_unavailable(self, submission: Post) -> bool:
        """Whether the post was found unavailable before (see _note_unavailable), checked before any request. A removed
        gallery that lacked its image list is tried again when the post has one now."""
        with self._store_lock:
            stored = self._get_url_hash(unavailable_post_key(submission.id))
        if not stored:
            return False
        kind, _, reason = stored.partition(":")
        if kind == "nodata" and (getattr(submission, "gallery_data", None) or self._from_archive(submission)):
            # Tried again with this data; if the archive has no image list either, it's recorded as gone
            logger.debug("Unavailable before for lack of its image list; trying again with this post's data")
            return False
        self._skip_reason = f"unavailable before: {reason}"
        self._state()["unavailable"] = stored
        return True

    def _incomplete(self, submission: Post) -> bool:
        """Whether the submission failed after writing some of its files (see _note_incomplete); its files are then
        checked one by one (under _store_lock)"""
        if self._check_url_exists_or_add(incomplete_post_key(submission.id), None):
            logger.debug("Its files are incomplete; checking each")
            return True
        return False

    def _folder_post_ids(self) -> set:
        """Name parts of the files in the download folder, read once per run (under _store_lock)"""
        if not hasattr(self, "_folder_ids"):
            names = []
            for _dirpath, _dirnames, filenames in os.walk(self.download_directory):
                names.extend(name for name in filenames if not Connector._is_own_file(name))
            self._folder_ids = post_ids_in_names(names)
        return self._folder_ids

    def _post_link_known(self, submission: Post) -> bool:
        """Whether this post's link was downloaded before (by another post, in this or an earlier run), checked before
        any request is made for it"""
        if self.args.rename_existing:
            # Renaming needs the resource's new destination, so the post goes through the normal checks
            return False
        link = self._post_link(submission)
        with self._store_lock:
            if self._incomplete(submission):
                return False
            known = self._check_url_exists_or_add(link, None) or link in self._run_urls()
        if known:
            logger.log(logs.TRACE, f"Its link was downloaded before: {link}")
            self._files["known_link"] += 1
        return known

    def _remember_post_link(self, submission: Post, media_url: str):
        """Stores the post's link next to its media URL, with the same hash, for posts with a single file. Later posts
        with the same link (reposts, e.g. of one Redgifs video) are then recognised without asking the host where the
        media is. Galleries and albums aren't stored, so one file never stands for a whole post."""
        link = self._post_link(submission)
        if link == media_url:
            return
        with self._store_lock:
            if media_url in self._run_urls():
                self._run_urls().add(link)
            file_hash = self._get_url_hash(media_url)
            if file_hash:
                self._check_url_exists_or_add(link, file_hash)

    def _record_url_of_existing_file(self, url: str, destination: Path):
        """Stores the media URL of a file that is already in place, using the file's stored hash, without downloading
        it. Files hashed offline (e.g. by `bdfr rename --build-hash-db`) then get their URLs as their posts come up,
        so a later file scheme change can match them by URL."""
        if not (self.args.keep_hashes or self.args.keep_hashes_db):
            return
        file_hash = self._get_file_hash(destination)
        if file_hash:
            self._check_url_exists_or_add(url, file_hash)

    def _rename_existing(self, file_hash: Optional[str], destination: Path, submission: Post) -> bool:
        """With --rename-existing, moves a previously downloaded copy of this resource to the name the current file
        scheme gives it, so changing the scheme doesn't leave old names behind. Only files whose name contains this
        submission's ID are renamed; an identical file from another submission keeps its name."""
        if not self.args.rename_existing or not file_hash:
            return False
        existing = self._get_hashed_item(file_hash)
        if not existing:
            return False
        existing = Path(existing)
        if existing == destination or not existing.exists() or destination.exists():
            return False
        if submission.id not in re.split(r"[_.]", existing.name):
            return False
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            existing.rename(destination)
        except OSError as e:
            logger.warning(f"Could not rename {existing} to {destination}: {e}")
            return False
        self._move_hashed_file(existing, destination, file_hash)
        logger.debug(f"Renamed {existing.name} to {destination.name}")
        self._files["renamed"] += 1
        return True

    def _check_before_download(self, submission: Post, destination: Path, res) -> Optional[bool]:
        """Whether a resource can be skipped without downloading it: True if it's already downloaded, False if it's
        filtered out, None if it needs downloading"""
        if destination.exists():
            logger.debug(f"Already in the folder: {destination.name}")
            self._record_url_of_existing_file(res.url, destination)
            self._run_urls().add(res.url)
            self._files["existing"] += 1
            return True
        elif not self.download_filter.check_resource(res):
            logger.debug(f"Removed by the download filter: {res.url}")
            self._files["filtered"] += 1
            return False
        if self._check_url_exists_or_add(res.url, None) or res.url in self._run_urls():
            logger.log(logs.TRACE, f"Downloaded before: {res.url}")
            if not self._rename_existing(self._get_url_hash(res.url), destination, submission):
                self._files["known_link"] += 1
            self._run_urls().add(res.url)
            return True
        return None

    def _claim(self, submission: Post, destination: Path, res) -> Optional[bool]:
        """Checks a resource before downloading it (see _check_before_download). If it needs downloading, marks its URL
        as being downloaded, to be released with _release. A URL another post of this run is downloading at the same
        time (with --parallel) is waited for and checked again, so the same link isn't downloaded twice."""
        while True:
            with self._store_lock:
                skip = self._check_before_download(submission, destination, res)
                if skip is not None:
                    return skip
                in_flight = self._in_flight()
                event = in_flight.get(res.url)
                if event is None:
                    in_flight[res.url] = threading.Event()
                    return None
            logger.log(logs.TRACE, f"Waiting for {res.url}, which another post is downloading")
            event.wait()

    def _release(self, url: str):
        with self._store_lock:
            event = self._in_flight().pop(url, None)
        if event:
            event.set()

    # Both are only used under _store_lock
    def _in_flight(self) -> dict:
        """URLs being downloaded right now, each with an event set when its download is dealt with"""
        if not hasattr(self, "_in_flight_urls"):
            self._in_flight_urls = {}
        return self._in_flight_urls

    def _run_urls(self) -> set:
        """URLs downloaded in this run, so a link shared by several posts is downloaded once even without a hash
        store"""
        if not hasattr(self, "_downloaded_urls"):
            self._downloaded_urls = set()
        return self._downloaded_urls

    def _download_submission(self, submission: Post) -> Optional[bool]:
        """Returns True if the submission was downloaded previously, False if anything new was written
        and None if it was filtered, failed or had nothing to download."""
        if submission.id in self.excluded_submission_ids:
            return self._skip("in the exclusion list")
        elif submission.subreddit.lower() in self.args.skip_subreddit:
            return self._skip("subreddit is in --skip-subreddit")
        elif (submission.author and submission.author in self.args.ignore_user) or (
            submission.author is None and "DELETED" in self.args.ignore_user
        ):
            return self._skip(f"posted by an ignored user ({submission.author or 'DELETED'})")
        # Archived and exported posts can lack scores; those are not filtered on them
        elif self.args.min_score and submission.score is not None and submission.score < self.args.min_score:
            return self._skip(f"score {submission.score} is below --min-score {self.args.min_score}")
        elif self.args.max_score and submission.score is not None and self.args.max_score < submission.score:
            return self._skip(f"score {submission.score} is above --max-score {self.args.max_score}")
        elif submission.upvote_ratio is not None and (
            (self.args.min_score_ratio and submission.upvote_ratio < self.args.min_score_ratio)
            or (self.args.max_score_ratio and self.args.max_score_ratio < submission.upvote_ratio)
        ):
            return self._skip(f"upvote ratio {submission.upvote_ratio} is outside --min/--max-score-ratio")
        elif not isinstance(submission, Post):
            return self._skip("not a submission")
        elif not self.download_filter.check_url(submission.url):
            return self._skip(f"link removed by --skip-domain or --skip: {submission.url}")
        elif self.args.media_only and getattr(submission, "is_self", False):
            return self._skip("self post (--media-only)")
        elif self.args.media_only and getattr(submission, "crosspost_parent", None):
            return self._skip("crosspost (--media-only)")
        if self._post_id_known(submission):
            return True
        if self._post_unavailable(submission):
            return None

        if submission.url is None or submission.url == "":
            return self._skip("no link")
        try:
            downloader_class = DownloadFactory.pull_lever(submission.url)
            downloader = downloader_class(submission, self.args)
            logger.log(logs.TRACE, f"{downloader_class.__name__}: {submission.url}")
        except errors.NotADownloadableLinkError:
            return self._unavailable(f"no downloader for {submission.url}")
        self._module = downloader_class.__name__
        if downloader_class.__name__.lower() in self.args.disable_module:
            return self._skip(f"module {downloader_class.__name__} is disabled")
        if self._post_link_known(submission):
            return True
        try:
            content = downloader.find_resources()
        except errors.SiteDownloaderError as e:
            gone = isinstance(e, errors.ContentGone)
            # A removed gallery may have its image list in the archive's data, unless that's what this is
            if self._module == "Gallery" and not getattr(submission, "gallery_data", None):
                if self._from_archive(submission) and (gone or self._known_removed(submission)):
                    return self._unavailable(f"{e}; the archive has no image list either")
                if gone:
                    return self._unavailable(str(e), "nodata")
            if gone:
                return self._unavailable(str(e))
            return self._fail(str(e))
        already_downloaded = None
        content = list(content)
        for destination, res in self.file_name_formatter.format_resource_paths(content, self.download_directory):
            skip = self._claim(submission, destination, res)
            if skip is not None:
                if skip and already_downloaded is None:
                    already_downloaded = True
                continue
            try:
                try:
                    # Written straight to a temporary file in the destination folder, not held in memory
                    res.download(
                        {
                            "max_wait_time": self.args.max_wait_time,
                            "fail_fast": self.args.fail_fast,
                            "temp_dir": destination.parent,
                        }
                    )
                except errors.ContentGone as e:
                    if len(content) == 1:
                        return self._unavailable(f"could not download {res.url}: {e}")
                    # One of several files: the others may still be there, so the post isn't given up on
                    return self._fail(f"could not download {res.url}: {e}", permanent=True)
                except errors.BulkDownloaderException as e:
                    return self._fail(f"could not download {res.url}: {e}")
                # The hash store is read and changed one thread at a time; the file is written outside the lock
                with self._store_lock:
                    resource_hash = res.hash.hexdigest()
                    while resource_hash in self._writing:
                        self._written.wait()
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if (
                        (self.args.keep_hashes or self.args.keep_hashes_db)
                        and self._check_hash_exists_or_add(None, resource_hash)
                        or (not self.args.keep_hashes and not self.args.keep_hashes_db)
                        and resource_hash in self.master_hash_list
                    ):
                        self._check_url_exists_or_add(res.url, resource_hash)
                        self._run_urls().add(res.url)
                        if self._rename_existing(resource_hash, destination, submission):
                            if already_downloaded is None:
                                already_downloaded = True
                            continue
                        if self.args.no_dupes:
                            hashed_item = self._get_hashed_item(resource_hash)
                            existing_name = Path(hashed_item).name if hashed_item else "(unknown file)"
                            logger.log(logs.TRACE, f"Same content as {existing_name}: {res.url}")
                            self._files["duplicate"] += 1
                            self._duplicates_of.append(existing_name)
                            self._state()["duplicate_hash"] = resource_hash
                            continue
                        elif self.args.make_hard_links:
                            hashed_item = self._get_hashed_item(resource_hash)
                            # The stored file may have been moved or deleted since; then the download is written instead
                            if hashed_item and Path(hashed_item).exists():
                                try:
                                    os.link(hashed_item, destination)
                                    logger.debug(f"Hard-linked {destination.name} to {hashed_item}")
                                    self._files["linked"] += 1
                                    continue
                                except OSError as e:
                                    logger.warning(
                                        f"Could not hard link {destination} to {hashed_item}, writing a copy: {e}"
                                    )
                    self._writing.add(resource_hash)
                try:
                    res.save(destination)
                    self._files["bytes"] += destination.stat().st_size
                    creation_time = time.mktime(datetime.fromtimestamp(submission.created_utc).timetuple())
                    os.utime(destination, (creation_time, creation_time))
                    logger.debug(f"Wrote {destination}")
                except OSError as e:
                    # A partly written file would make the post look downloaded (its ID is in the name)
                    with contextlib.suppress(OSError):
                        destination.unlink(missing_ok=True)
                    with self._written:
                        self._writing.discard(resource_hash)
                        self._written.notify_all()
                    return self._fail(f"could not write {destination.name}: {e}")
                with self._written:
                    self._writing.discard(resource_hash)
                    self._written.notify_all()
                    self._files["written"] += 1
                    self._folder_post_ids().add(submission.id)
                    if self.args.keep_hashes or self.args.keep_hashes_db:
                        self._check_hash_exists_or_add(destination, resource_hash)
                    else:
                        self.master_hash_list[resource_hash] = destination
                    self._check_url_exists_or_add(res.url, resource_hash)
                    self._run_urls().add(res.url)
                    logger.log(logs.TRACE, f"Hash of {destination.name}: {resource_hash}")
                    already_downloaded = False
            finally:
                # A download that wasn't kept (a duplicate, a failure) leaves no temporary file behind
                res.discard()
                self._release(res.url)
        if len(content) == 1:
            self._remember_post_link(submission, content[0].url)
        if already_downloaded is None and self._files["duplicate"] and not self._skip_reason:
            # Nothing new and nothing failed: remember the post, so it isn't downloaded again to find the same
            with self._store_lock:
                self._check_url_exists_or_add(duplicate_post_key(submission.id), self._state()["duplicate_hash"])
        elif len(content) > 1 and self._files["known_link"] == len(content):
            # Every file was downloaded before under other links' posts (e.g. a gallery posted again, or an old
            # account's): nothing has this post's ID, so it's remembered the same way, for later runs and /known
            with self._store_lock:
                if submission.id not in self._folder_post_ids():
                    stored_hash = self._get_url_hash(content[0].url) or "known"
                    self._check_url_exists_or_add(duplicate_post_key(submission.id), stored_hash)
        return already_downloaded
