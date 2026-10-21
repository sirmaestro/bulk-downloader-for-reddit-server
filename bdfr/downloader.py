#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging.handlers
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
from bdfr.configuration import Configuration
from bdfr.connector import Connector
from bdfr.post import Post
from bdfr.site_downloaders.download_factory import DownloadFactory

logger = logging.getLogger(__name__)

RESULT_NAMES = {False: "new", True: "already downloaded", None: "skipped"}


def duplicate_post_key(post_id: str) -> str:
    """The URL-table key that records a post whose media was all already downloaded under other links, so it's known
    (by `bdfr serve`'s /known, and by later runs) without downloading it again"""
    return f"post:{post_id}"


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
    # What happened to the files of the submission a thread is downloading, for its summary line
    _outcome = threading.local()

    def __init__(self, args: Configuration, logging_handlers: Iterable[logging.Handler] = ()):
        super().__init__(args, logging_handlers)
        # Called with (submission ID, result of _download_submission, label from result_label) after each
        # submission, in listing order; used by `bdfr serve`
        self.progress_callback: Optional[Callable[[str, Optional[bool], str], None]] = None
        # Why listings were stopped before their end in download(); the run didn't see everything it was asked to
        self.errors: list[str] = []
        # When the newest submission the run went through was posted (whatever happened to it), or None
        self.newest_post: Optional[float] = None

    def _stop_at_existing_reached(self, result, consecutive_existing: int) -> (bool, int):
        # result: True = already downloaded, False = newly downloaded, None = neither (filtered or failed)
        if not self.args.stop_at_existing:
            return False, consecutive_existing
        if result is True:
            consecutive_existing += 1
        elif result is False:
            consecutive_existing = 0
        if consecutive_existing >= self.args.stop_at_existing:
            logger.info(
                f"Reached {consecutive_existing} previously downloaded submissions in a row, stopping this listing"
            )
            return True, consecutive_existing
        return False, consecutive_existing

    def download(self):
        totals = Counter()
        number = 0
        done = set()
        workers = max(1, self.args.parallel or 1)
        pool = ThreadPoolExecutor(workers, thread_name_prefix="bdfr-download") if workers > 1 else None
        try:
            for generator in self.listings:
                consecutive_existing = 0
                # Given posts (--post-json, --link) are all checked; --stop-at-existing is for listings
                stop_enabled = generator is not getattr(self, "post_list", None)
                # Submissions being downloaded in threads, oldest first; results are reported in this order
                pending: deque = deque()
                stopped = False

                def finish(submission: Post, run: tuple) -> bool:
                    """Reports a finished submission; returns whether --stop-at-existing ends the listing"""
                    nonlocal number, consecutive_existing
                    result, outcome = run
                    number += 1
                    self._log_outcome(number, submission, result, outcome)
                    totals[result] += 1
                    totals["files"] += outcome["files"]["written"] + outcome["files"]["linked"]
                    created = getattr(submission, "created_utc", None)
                    if isinstance(created, (int, float)) and (self.newest_post is None or created > self.newest_post):
                        self.newest_post = created
                    if self.progress_callback:
                        self.progress_callback(submission.id, result, self.result_label(result, outcome))
                    if not stop_enabled:
                        return False
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
                        pending.append((submission, pool.submit(self._run_one, submission)))
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
            logger.error(f"Uncaught exception: {e}")
            logger.exception(e)
            self.errors.append(f"Uncaught exception: {e}")
        finally:
            if pool:
                pool.shutdown(wait=True, cancel_futures=True)
        logger.info(
            f"Finished {number} posts: {totals[False]} new ({totals['files']} files written), "
            f"{totals[True]} already downloaded, {totals[None]} skipped"
        )
        logger.info(self.arctic_shift.summary())
        self._hash_list_save(False)
        self.close_hash_db()

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

    def _run_one(self, submission: Post) -> tuple[Optional[bool], dict]:
        """Downloads one submission (in a download thread with --parallel); returns its result and what happened to
        its files"""
        self._outcome.state = {"files": Counter(), "skip_reason": None, "duplicates_of": [], "module": None}
        result = None
        try:
            result = self._download_submission(submission)
        except Exception as e:
            # One bad submission should not end the whole run
            logger.error(f"Unhandled exception downloading submission {submission.id}: {e}")
            logger.exception(e)
            self._skip_reason = f"error: {e}"
        return result, self._outcome.state

    def _state(self) -> dict:
        if not hasattr(self._outcome, "state"):
            self._outcome.state = {"files": Counter(), "skip_reason": None, "duplicates_of": [], "module": None}
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
        self._skip_reason = reason

    def _log_outcome(self, number: int, submission: Post, result: Optional[bool], outcome: dict):
        """One line per submission saying what happened to it and why; the per-file lines are debug"""
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
        where = f" in r/{submission.subreddit}" if submission.subreddit else ""
        module = f" [{outcome['module']}]" if outcome["module"] else ""
        logger.info(f"#{number} {submission.id}{where}{module}: {RESULT_NAMES[result]} - {'; '.join(parts)}")

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
            if submission.id in self._folder_post_ids():
                logger.debug(f"Submission {submission.id} has files in the folder already")
                self._files["known_post"] += 1
                return True
            if self._check_url_exists_or_add(duplicate_post_key(submission.id), None):
                logger.debug(f"Submission {submission.id} was all duplicates before")
                self._files["known_duplicate"] += 1
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
            known = self._check_url_exists_or_add(link, None) or link in self._run_urls()
        if known:
            logger.debug(f"Link {link} of submission {submission.id} was downloaded before")
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
        logger.debug(f"Renamed {existing.name} to {destination.name} in submission {submission.id}")
        self._files["renamed"] += 1
        return True

    def _check_before_download(self, submission: Post, destination: Path, res) -> Optional[bool]:
        """Whether a resource can be skipped without downloading it: True if it's already downloaded, False if it's
        filtered out, None if it needs downloading"""
        if destination.exists():
            logger.debug(
                f"File {destination} from submission {submission.id} in {submission.subreddit} "
                "already exists, continuing"
            )
            self._record_url_of_existing_file(res.url, destination)
            self._run_urls().add(res.url)
            self._files["existing"] += 1
            return True
        elif not self.download_filter.check_resource(res):
            logger.debug(f"Download filter removed {submission.id} file with URL {submission.url}")
            self._files["filtered"] += 1
            return False
        if self._check_url_exists_or_add(res.url, None) or res.url in self._run_urls():
            logger.debug(
                f"Url {res.url} from submission {submission.id} in {submission.subreddit} already downloaded before"
            )
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
            logger.debug(f"Submission {submission.id}: waiting for {res.url}, which another post is downloading")
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
            logger.debug(f"Object {submission.id} in exclusion list, skipping")
            return self._skip("in the exclusion list")
        elif submission.subreddit.lower() in self.args.skip_subreddit:
            logger.debug(f"Submission {submission.id} in {submission.subreddit} in skip list")
            return self._skip("subreddit is in --skip-subreddit")
        elif (submission.author and submission.author in self.args.ignore_user) or (
            submission.author is None and "DELETED" in self.args.ignore_user
        ):
            logger.debug(
                f"Submission {submission.id} in {submission.subreddit} skipped"
                f' due to {submission.author or "DELETED"} being an ignored user'
            )
            return self._skip("posted by an ignored user")
        # Archived and exported posts can lack scores; those are not filtered on them
        elif self.args.min_score and submission.score is not None and submission.score < self.args.min_score:
            logger.debug(
                f"Submission {submission.id} filtered due to score {submission.score} < [{self.args.min_score}]"
            )
            return self._skip(f"score {submission.score} is below --min-score {self.args.min_score}")
        elif self.args.max_score and submission.score is not None and self.args.max_score < submission.score:
            logger.debug(
                f"Submission {submission.id} filtered due to score {submission.score} > [{self.args.max_score}]"
            )
            return self._skip(f"score {submission.score} is above --max-score {self.args.max_score}")
        elif submission.upvote_ratio is not None and (
            (self.args.min_score_ratio and submission.upvote_ratio < self.args.min_score_ratio)
            or (self.args.max_score_ratio and self.args.max_score_ratio < submission.upvote_ratio)
        ):
            logger.debug(f"Submission {submission.id} filtered due to score ratio ({submission.upvote_ratio})")
            return self._skip(f"upvote ratio {submission.upvote_ratio} is outside --min/--max-score-ratio")
        elif not isinstance(submission, Post):
            logger.warning(f"{submission.id} is not a submission")
            return self._skip("not a submission")
        elif not self.download_filter.check_url(submission.url):
            logger.debug(f"Submission {submission.id} filtered due to URL {submission.url}")
            return self._skip(f"link removed by --skip-domain or --skip: {submission.url}")
        elif self.args.media_only and getattr(submission, "is_self", False):
            return self._skip("self post (--media-only)")
        elif self.args.media_only and getattr(submission, "crosspost_parent", None):
            return self._skip("crosspost (--media-only)")
        if self._post_id_known(submission):
            return True

        logger.debug(f"Attempting to download submission {submission.id}")
        if submission.url is None or submission.url == "":
            logger.debug("skipped empty url")
            return self._skip("no link")
        try:
            downloader_class = DownloadFactory.pull_lever(submission.url)
            downloader = downloader_class(submission, self.args)
            logger.debug(f"Using {downloader_class.__name__} with url {submission.url}")
        except errors.NotADownloadableLinkError as e:
            logger.error(f"Could not download submission {submission.id}: {e}")
            return self._skip(f"no downloader for {submission.url}")
        self._module = downloader_class.__name__
        if downloader_class.__name__.lower() in self.args.disable_module:
            logger.debug(f"Submission {submission.id} skipped due to disabled module {downloader_class.__name__}")
            return self._skip(f"module {downloader_class.__name__} is disabled")
        if self._post_link_known(submission):
            return True
        try:
            content = downloader.find_resources()
        except errors.SiteDownloaderError as e:
            logger.error(f"Site {downloader_class.__name__} failed to download submission {submission.id}: {e}")
            return self._skip(f"failed: {e}")
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
                    res.download({"max_wait_time": self.args.max_wait_time, "fail_fast": self.args.fail_fast})
                except errors.BulkDownloaderException as e:
                    logger.error(
                        f"Failed to download resource {res.url} in submission {submission.id} "
                        f"with downloader {downloader_class.__name__}: {e}"
                    )
                    return self._skip(f"download failed: {e}")
                # From here on, the hash store is read and changed, so one thread at a time
                with self._store_lock:
                    resource_hash = res.hash.hexdigest()
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
                            logger.debug(
                                f"Resource hash {resource_hash} from submission {submission.id} downloaded elsewhere"
                            )
                            logger.debug(f"URL: {res.url} has the same content as {existing_name}")
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
                                    logger.debug(
                                        f"Hard link made linking {destination} to {hashed_item} "
                                        f"in submission {submission.id}"
                                    )
                                    self._files["linked"] += 1
                                    continue
                                except OSError as e:
                                    logger.warning(
                                        f"Could not hard link {destination} to {hashed_item}, writing a copy: {e}"
                                    )
                    try:
                        with destination.open("wb") as file:
                            file.write(res.content)
                        logger.debug(f"Written file to {destination}")
                        logger.debug(f"Downloaded submission {submission.id} from {submission.subreddit}")
                    except OSError as e:
                        logger.exception(e)
                        logger.error(f"Failed to write file in submission {submission.id} to {destination}: {e}")
                        return self._skip(f"could not write {destination.name}: {e}")
                    self._files["written"] += 1
                    self._folder_post_ids().add(submission.id)
                    creation_time = time.mktime(datetime.fromtimestamp(submission.created_utc).timetuple())
                    os.utime(destination, (creation_time, creation_time))
                    if self.args.keep_hashes or self.args.keep_hashes_db:
                        self._check_hash_exists_or_add(destination, resource_hash)
                    else:
                        self.master_hash_list[resource_hash] = destination
                    self._check_url_exists_or_add(res.url, resource_hash)
                    self._run_urls().add(res.url)
                    logger.log(9, f"Hash added to master list: {resource_hash}")
                    already_downloaded = False
            finally:
                self._release(res.url)
        if len(content) == 1:
            self._remember_post_link(submission, content[0].url)
        if already_downloaded is None and self._files["duplicate"] and not self._skip_reason:
            # Nothing new and nothing failed: remember the post, so it isn't downloaded again to find the same
            with self._store_lock:
                self._check_url_exists_or_add(duplicate_post_key(submission.id), self._state()["duplicate_hash"])
        return already_downloaded
