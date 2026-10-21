#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Local HTTP server behind `bdfr serve`, which runs downloads requested by the Tampermonkey script
(scripts/tampermonkey/bdfr-export.user.js). It only listens on 127.0.0.1 and every request needs the token."""

import copy
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
from typing import Optional

import appdirs

from bdfr import __version__
from bdfr.configuration import Configuration
from bdfr.connector import RedditConnector
from bdfr.downloader import RedditDownloader, duplicate_post_key, post_ids_in_names, post_link

logger = logging.getLogger(__name__)

DEFAULT_PORT = 7635
MAX_BODY_BYTES = 20 * 1024 * 1024
MAX_POSTS_PER_JOB = 5000
MAX_FINISHED_JOBS = 50
# serve_log.txt rotates at this size (keeping the config's backup_log_count old files), not at every job
SERVE_LOG_BYTES = 10 * 1024 * 1024
# One folder name directly under the root, so a request can't write anywhere else
FOLDER_PATTERN = re.compile(r"[\w\-. ]{1,100}")
USER_PATTERN = re.compile(r"[\w-]{1,30}")
POST_ID_PATTERN = re.compile(r"[a-z0-9]{3,13}")  # also keeps gallery indexes (_1, _2) from matching
RESULT_LABELS = {True: "existing", False: "new", None: "skipped"}
LABELS = ("new", "existing", "duplicate", "skipped")


class Job:
    def __init__(
        self,
        job_id: str,
        folder: str,
        posts: Optional[list[dict]] = None,
        user: Optional[str] = None,
        full_archive: bool = False,
        before: Optional[int] = None,
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
        self.state = "queued"
        self.error: Optional[str] = None
        self.counts = {label: 0 for label in LABELS}
        self.results: dict[str, str] = {}
        self.created = time.time()
        self.finished: Optional[float] = None
        self._lock = threading.Lock()

    def record(self, submission_id: str, result: Optional[bool], label: Optional[str] = None):
        label = label or RESULT_LABELS[result]
        with self._lock:
            self.counts[label] += 1
            self.results[submission_id] = label

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "folder": self.folder,
                "mode": "user" if self.user else "posts",
                "total": None if self.user else len(self.posts),
                "state": self.state,
                "error": self.error,
                "counts": dict(self.counts),
                "results": dict(self.results),
            }


class DownloadServer:
    def __init__(self, base_args: Configuration, root: Path, token: str, log_path: Path):
        self.base_args = base_args
        self.root = root
        self.token = token
        self.log_path = log_path
        self.jobs: dict[str, Job] = {}
        self.current: Optional[Job] = None
        self._queue: "queue.Queue[Job]" = queue.Queue()
        self._lock = threading.Lock()
        threading.Thread(target=self._worker, name="bdfr-serve-worker", daemon=True).start()

    @staticmethod
    def _folder(payload) -> str:
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object")
        folder = str(payload.get("folder", "")).strip()
        if not FOLDER_PATTERN.fullmatch(folder) or folder.strip(".") == "":
            raise ValueError("'folder' must be a single folder name of letters, digits, spaces, '-', '_' or '.'")
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
            names = (name for name in filenames if not RedditConnector._is_own_file(name))
            found.update(wanted.intersection(post_ids_in_names(names)))
        # Posts recorded as all duplicates
        duplicates = self._stored_keys(self.root / folder, {duplicate_post_key(i): i for i in wanted - found})
        # Posts whose link was downloaded before, by any post (reposts); see RedditDownloader._remember_post_link
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

    def submit(self, payload: dict) -> Job:
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
        before = payload.get("before")
        if before is not None and (not isinstance(before, int) or isinstance(before, bool) or before <= 0):
            raise ValueError("'before' must be a time in seconds since 1970")
        job = Job(secrets.token_hex(6), folder, posts, user, payload.get("archive") == "all", before)
        with self._lock:
            self.jobs[job.id] = job
            finished = [j for j in self.jobs.values() if j.finished]
            for old in sorted(finished, key=lambda j: j.finished)[:-MAX_FINISHED_JOBS]:
                del self.jobs[old.id]
        self._queue.put(job)
        logger.info(f"Queued job {job.id}: {self._describe(job)}")
        return job

    def status(self) -> dict:
        return {
            "version": __version__,
            "root": str(self.root),
            "running": self.current.id if self.current else None,
            "queued": self._queue.qsize(),
        }

    def _worker(self):
        while True:
            job = self._queue.get()
            self.current = job
            try:
                self._run(job)
            finally:
                self.current = None

    def _run(self, job: Job):
        args = copy.deepcopy(self.base_args)
        args.directory = str(self.root / job.folder)
        args.source = "arctic-shift"
        args.log = str(self.log_path)
        args.log_rotate_bytes = SERVE_LOG_BYTES
        for key in ("subreddit", "user", "link", "include_id_file", "post_json", "multireddit"):
            setattr(args, key, [])
        args.submitted = args.saved = args.upvoted = args.subscribed = False
        args.search = None
        post_file = None
        if job.posts:
            # Every given post is checked (--stop-at-existing only applies to listings), so a run of existing posts
            # doesn't hide newer ones after it
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as file:
                json.dump({"posts": job.posts}, file)
                post_file = Path(file.name)
            args.post_json = [str(post_file)]
        if job.user:
            # The page's posts are downloaded first, then the user's listing: until already-downloaded posts, or all
            # of it. Posts already in the folder are recognised from their IDs without requests, so reading the whole
            # archive mostly costs the listing's own requests.
            args.user = [job.user]
            args.submitted = True
            args.stop_at_existing = 0 if job.full_archive else (args.stop_at_existing or 5)
            args.listing_before = job.before

        root_logger = logging.getLogger()
        handlers_before = list(root_logger.handlers)
        job.state = "running"
        started = time.time()
        logger.info(f"Starting job {job.id}: {self._describe(job)}")
        try:
            downloader = RedditDownloader(args)
            # Repeated once the downloader's log file is open, so each job's log says what it was for and how
            logger.debug(f"Job {job.id}: {self._describe(job)}; {self._options(args)}")
            downloader.progress_callback = job.record
            downloader.download()
            job.state = "done"
        except Exception as e:
            logger.exception(f"Job {job.id} failed")
            job.state = "failed"
            job.error = str(e)
        finally:
            job.finished = time.time()
            c = job.counts
            logger.info(
                f"Finished job {job.id} ({job.state}) in {self._duration(job.finished - started)}: {c['new']} new, "
                f"{c['existing']} already downloaded, {c['duplicate']} duplicates, {c['skipped']} skipped"
                + (f" - {job.error}" if job.error else "")
            )
            # Each downloader adds its own file log handler to the root logger
            for handler in [h for h in root_logger.handlers if h not in handlers_before]:
                root_logger.removeHandler(handler)
                handler.close()
            if post_file:
                post_file.unlink(missing_ok=True)

    @staticmethod
    def _options(args: Configuration) -> str:
        """The download options that change what a job does"""
        flags = [name for name in ("no_dupes", "search_existing", "keep_hashes", "keep_hashes_db", "make_hard_links",
                                   "rename_existing") if getattr(args, name, False)]
        parts = [f"--{name.replace('_', '-')}" for name in flags]
        parts.append(f"--parallel {args.parallel}")
        if args.stop_at_existing:
            parts.append(f"--stop-at-existing {args.stop_at_existing}")
        parts.append(f'--file-scheme "{args.file_scheme}"')
        for name in ("min_score", "max_score", "skip_domain", "skip", "skip_subreddit", "disable_module"):
            value = getattr(args, name, None)
            if value:
                parts.append(f"--{name.replace('_', '-')} {value if not isinstance(value, (list, set)) else ','.join(sorted(value))}")
        return " ".join(parts)

    @staticmethod
    def _duration(seconds: float) -> str:
        minutes, seconds = divmod(int(seconds), 60)
        return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"

    def _describe(self, job: Job) -> str:
        if job.user:
            source = f"{'all archived' if job.full_archive else 'new'} posts by u/{job.user}"
            if job.before:
                source += f" older than {time.strftime('%Y-%m-%d %H:%M', time.localtime(job.before))}"
            source += f" (with {len(job.posts)} from the page)" if job.posts else ""
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
            elif match := re.fullmatch(r"/jobs/(\w+)", self.path):
                job = server.jobs.get(match.group(1))
                self._send(200, job.to_dict()) if job else self._send(404, {"error": "No such job"})
            else:
                self._send(404, {"error": "Not found"})

        def do_POST(self):
            if not self._authorised():
                return
            if self.path not in ("/jobs", "/known"):
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
                else:
                    self._send(202, server.submit(payload).to_dict())
            except (ValueError, json.JSONDecodeError) as e:
                self._send(400, {"error": str(e)})

    return Handler


def load_token(config_directory: Path) -> str:
    """The token is kept next to BDFR's configuration, created on first use"""
    token_path = config_directory / "serve_token.txt"
    if token_path.exists() and (token := token_path.read_text(encoding="utf-8").strip()):
        return token
    config_directory.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    token_path.write_text(token, encoding="utf-8")
    return token


def run_server(base_args: Configuration, port: int):
    root = Path(base_args.directory).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    config_directory = Path(appdirs.user_config_dir("bdfr", "BDFR"))
    token = load_token(config_directory)
    server = DownloadServer(base_args, root, token, config_directory / "serve_log.txt")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(server))
    logger.info(f"Listening on http://127.0.0.1:{port}, downloading into folders under {root}")
    logger.info(f"Token for the Tampermonkey script: {token}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping")
    finally:
        httpd.server_close()
