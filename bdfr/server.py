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
from pathlib import Path
from typing import Callable, Optional

import platformdirs

from bdfr import __version__, logs
from bdfr.configuration import Configuration
from bdfr.downloader import Downloader, duplicate_post_key, incomplete_post_key, post_ids_in_names, post_link
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
LABELS = ("new", "existing", "duplicate", "skipped", "failed")


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
        # Called with the job once it has finished, whatever the outcome (used by the scheduler)
        self.on_finish: Optional[Callable[["Job"], None]] = None
        self.state = "queued"
        self.error: Optional[str] = None
        self.counts = {label: 0 for label in LABELS}
        self.results: dict[str, str] = {}
        # When the newest post the job went through was posted (used by the scheduler to notice quiet users)
        self.newest_post: Optional[float] = None
        self.created = time.time()
        self.finished: Optional[float] = None
        self._lock = threading.Lock()

    def record(self, submission_id: str, result: Optional[bool], label: Optional[str] = None):
        label = label or RESULT_LABELS[result]
        with self._lock:
            self.counts[label] += 1
            self.results[submission_id] = label

    def summary(self) -> dict:
        """The job without its per-post results, for GET /jobs"""
        details = self.to_dict()
        del details["results"]
        details.update(user=self.user, created=self.created, finished=self.finished)
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
        return {
            "known": known,
            "known_by_link": [i for i in ids if i in by_link],
            "duplicates": [i for i in ids if i in duplicates and i not in by_link],
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
        job = Job(secrets.token_hex(6), folder, posts, user, payload.get("archive") == "all", before, after, retry)
        job.on_finish = on_finish
        if posts and self.scheduler is not None and not retry:
            # Posts read from Reddit by the userscript wake hibernating watched users
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

    def watchlist(self) -> dict:
        """The watched users with their baseline state, for GET /watchlist (the userscript's background sync)"""
        if self.scheduler is None:
            return {"users": [], "enabled": False}
        return self.scheduler.snapshot()

    def recent_jobs(self) -> dict:
        """GET /jobs: the queued, running and last MAX_FINISHED_JOBS finished jobs, newest first"""
        with self._lock:
            jobs = sorted(self.jobs.values(), key=lambda job: job.created, reverse=True)
        return {"jobs": [job.summary() for job in jobs]}

    def status(self) -> dict:
        return {
            "version": __version__,
            "root": str(self.root),
            "running": self.current.id if self.current else None,
            "queued": self._queue.qsize(),
            "retries": self.retries.snapshot() if self.retries else None,
        }

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
        if job.posts:
            # Every given post is checked (--stop-at-existing only applies to listings), so a run of existing posts
            # doesn't hide newer ones after it
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as file:
                json.dump({"posts": job.posts}, file)
                post_file = Path(file.name)
            args.post_json = [str(post_file)]
            if not job.user and self.scheduler is not None:
                # Archive checks off for the folder's user (the watchlist's default): Arctic Shift isn't asked for
                # anything, not even the data of single posts
                args.archive_lookups = self.scheduler.archive_lookups(job.folder)
            if job.retry:
                # The posts' data is what the first job had, so the archive has nothing to add
                args.archive_lookups = False
        if job.user:
            # The page's posts are downloaded first, then the user's listing: until already-downloaded posts, or all
            # of it. Posts already in the folder are recognised from their IDs without requests, so reading the whole
            # archive mostly costs the listing's own requests.
            args.user = [job.user]
            args.submitted = True
            full = job.full_archive or job.after is not None
            args.stop_at_existing = 0 if full else (args.stop_at_existing or 5)
            args.listing_before = job.before
            args.listing_after = job.after

        job.state = "running"
        started = time.time()
        logger.info(f"Started: {self._describe(job)}")
        logger.debug(f"Options: {self._options(args)}")
        # The outcome is published once the job is cleaned up, so whoever sees it finished sees it complete
        state, error = "failed", None
        failed_posts: dict[str, tuple[dict, str]] = {}
        try:
            # Its lines go to serve_log.txt with the server's, so it opens no log file of its own
            downloader = Downloader(args, log_file=False)
            downloader.progress_callback = job.record
            failed_posts = downloader.failed_posts
            downloader.download()
            job.newest_post = downloader.newest_post
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

        def do_GET(self):
            if not self._authorised():
                return
            if self.path == "/status":
                self._send(200, server.status())
            elif self.path == "/watchlist":
                self._send(200, server.watchlist())
            elif self.path == "/jobs":
                self._send(200, server.recent_jobs())
            elif match := re.fullmatch(r"/jobs/(\w+)", self.path):
                job = server.jobs.get(match.group(1))
                self._send(200, job.to_dict()) if job else self._send(404, {"error": "No such job"})
            else:
                self._send(404, {"error": "Not found"})

        def do_POST(self):
            if not self._authorised():
                return
            if self.path not in ("/jobs", "/known", "/watchlist/gone"):
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
    from bdfr.scheduler import BaselineScheduler

    watchlist = Path(base_args.watchlist).expanduser() if base_args.watchlist else config_directory / "watchlist.yaml"
    server.scheduler = BaselineScheduler(server, watchlist)
    server.scheduler.start()
    from bdfr.retries import RetryList

    server.retries = RetryList(server, config_directory / "failed_posts.json")
    server.retries.start()
    logger.info(f"Watched users are read from {watchlist}" + ("" if watchlist.exists() else " (not there yet)"))
    host = base_args.host or "127.0.0.1"
    httpd = ThreadingHTTPServer((host, port), make_handler(server))
    logger.info(f"Listening on http://{host}:{port}, downloading into folders under {root}")
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
