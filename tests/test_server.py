#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import io
import json
import logging
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bdfr import logs
from bdfr.configuration import Configuration
from bdfr.server import DownloadServer, Job, load_token, make_handler

TOKEN = "test-token"


class FakeDownloader:
    """Stands in for Downloader, recording the arguments each job ran with"""

    runs = []
    log_files = []

    def __init__(self, args: Configuration, log_file: bool = True):
        self.log_files.append(log_file)
        self.args = args
        self.progress_callback = None
        self.errors = []
        self.newest_post = self.oldest_listed = self.stop_reason = None
        self.bytes_written = 0
        self.failed_posts = {}

    def download(self):
        post_ids = []
        if self.args.post_json:
            post_file = Path(self.args.post_json[0])
            post_ids = [post["id"] for post in json.loads(post_file.read_text(encoding="utf-8"))["posts"]]
        FakeDownloader.runs.append((self.args, post_ids))
        for post_id, result in zip(post_ids, (False, True, None)):
            self.progress_callback(post_id, result)


@pytest.fixture()
def server(tmp_path: Path):
    FakeDownloader.runs = []
    FakeDownloader.log_files = []
    with patch("bdfr.server.Downloader", FakeDownloader):
        args = Configuration()
        args.stop_at_existing = 0
        download_server = DownloadServer(args, tmp_path, TOKEN)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(download_server))
        threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True).start()
        yield download_server, f"http://127.0.0.1:{httpd.server_address[1]}"
        httpd.shutdown()
        httpd.server_close()


def call(base: str, method: str, path: str, body=None, token: str = TOKEN) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={"X-BDFR-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def wait_for(base: str, job_id: str) -> dict:
    for _ in range(100):
        _, job = call(base, "GET", f"/jobs/{job_id}")
        if job["state"] in ("done", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_requires_token(server):
    _, base = server
    assert call(base, "GET", "/status", token="wrong")[0] == 401
    assert call(base, "GET", "/status")[0] == 200


@pytest.mark.parametrize(
    "body",
    (
        {"folder": "../escape", "user": "someone"},
        {"folder": "a/b", "user": "someone"},
        {"folder": "..", "user": "someone"},
        {"folder": "ok"},
        {"folder": "ok", "user": "not a user!"},
        {"folder": "ok", "posts": []},
        {"folder": "ok", "user": "someone", "after": "last week"},
        {"folder": "ok", "user": "someone", "after": 0},
        {"folder": "ok", "user": "someone", "after": True},
        {"folder": "ok", "posts": [{"id": "abc"}], "after": 1700000000},
        [],
    ),
)
def test_rejects_bad_jobs(server, body):
    _, base = server
    assert call(base, "POST", "/jobs", body)[0] == 400


def test_posts_job(server, tmp_path: Path):
    _, base = server
    status, job = call(base, "POST", "/jobs", {"folder": "some user", "posts": [{"id": "a"}, {"id": "b"}, {"id": "c"}]})
    assert status == 202
    job = wait_for(base, job["id"])
    assert job["state"] == "done"
    assert job["results"] == {"a": "new", "b": "existing", "c": "skipped"}
    assert job["counts"] == {"existing": 1, "new": 1, "duplicate": 0, "skipped": 1, "failed": 0, "unavailable": 0}
    args, post_ids = FakeDownloader.runs[0]
    assert post_ids == ["a", "b", "c"]
    assert Path(args.directory) == tmp_path / "some user"
    assert args.stop_at_existing == 0 and not args.user
    assert not Path(args.post_json[0]).exists()


def test_user_job(server):
    _, base = server
    _, job = call(base, "POST", "/jobs", {"folder": "someone", "user": "someone"})
    assert wait_for(base, job["id"])["state"] == "done"
    args, _ = FakeDownloader.runs[0]
    assert args.user == ["someone"] and args.submitted and args.stop_at_existing == 5
    assert args.post_json == []


def test_user_job_with_page_posts(server):
    _, base = server
    _, job = call(base, "POST", "/jobs", {"folder": "someone", "user": "someone", "posts": [{"id": "a"}]})
    job = wait_for(base, job["id"])
    assert job["state"] == "done" and job["mode"] == "user" and job["total"] is None
    args, post_ids = FakeDownloader.runs[0]
    assert post_ids == ["a"]
    assert args.user == ["someone"] and args.submitted and args.stop_at_existing == 5


def test_job_lines_name_the_job(server):
    """Jobs log to serve_log.txt with the server (their downloaders open no log file), every line tagged with the job"""
    _, base = server
    stream = io.StringIO()
    handler = logs.prepare(logging.StreamHandler(stream), logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(context)s%(message)s"))
    root = logging.getLogger()
    level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    try:
        job = wait_for(base, call(base, "POST", "/jobs", {"folder": "a", "user": "someone"})[1]["id"])
    finally:
        root.removeHandler(handler)
        root.setLevel(level)
    assert job["state"] == "done"
    assert FakeDownloader.log_files == [False]
    lines = stream.getvalue().splitlines()
    assert f"job {job['id']}: Queued: new posts by u/someone into {server[0].root / 'a'}" in lines
    assert f"job {job['id']}: Done in 0s" in lines


def test_token_from_environment(tmp_path: Path, monkeypatch):
    """In Docker the token can be given as BDFR_TOKEN instead of being generated into the config volume"""
    monkeypatch.setenv("BDFR_TOKEN", "  from-env  ")
    assert load_token(tmp_path) == "from-env"
    assert not (tmp_path / "serve_token.txt").exists()
    monkeypatch.setenv("BDFR_TOKEN", "")
    assert load_token(tmp_path) == load_token(tmp_path) != ""


class StopServing(ThreadingHTTPServer):
    addresses = []

    def __init__(self, address, handler):
        StopServing.addresses.append(address)

    def serve_forever(self, poll_interval=0.5):
        raise KeyboardInterrupt

    def server_close(self):
        pass


@pytest.mark.parametrize(("host", "expected", "warns"), ((None, "127.0.0.1", False), ("0.0.0.0", "0.0.0.0", True)))
def test_run_server_host(tmp_path: Path, caplog, host, expected, warns):
    from bdfr.server import run_server

    StopServing.addresses = []
    args = Configuration()
    args.directory = str(tmp_path / "root")
    args.watchlist = str(tmp_path / "watchlist.yaml")
    args.host = host
    with (
        patch("bdfr.server.ThreadingHTTPServer", StopServing),
        patch("bdfr.server.platformdirs.user_config_dir", lambda *a: str(tmp_path / "config")),
        caplog.at_level("INFO"),
    ):
        run_server(args, 7635)
    assert StopServing.addresses == [(expected, 7635)]
    assert f"http://{expected}:7635" in caplog.text
    assert ("other machines" in caplog.text) is warns
    # Failed posts aren't retried without --retry-failed
    assert "--retry-failed is off" in caplog.text
    # Everything goes to serve_log.txt, and the handler is removed when the server stops
    assert f"http://{expected}:7635" in (tmp_path / "config" / "serve_log.txt").read_text(encoding="utf-8")
    assert not any(getattr(h, "baseFilename", "").endswith("serve_log.txt") for h in logging.getLogger().handlers)


def test_serve_host_option(tmp_path: Path):
    from click.testing import CliRunner

    from bdfr.__main__ import cli

    captured = {}
    with patch("bdfr.__main__.run_server", lambda config, port: captured.update(config=config)):
        CliRunner().invoke(cli, ["serve", str(tmp_path), "--host", "0.0.0.0"])
    assert captured["config"].host == "0.0.0.0"


@pytest.mark.parametrize(("arguments", "expected"), (([], False), (["--retry-failed"], True)))
def test_retry_failed_option(tmp_path: Path, arguments: list, expected: bool):
    from click.testing import CliRunner

    from bdfr.__main__ import cli

    captured = {}
    with patch("bdfr.__main__.run_server", lambda config, port: captured.update(config=config)):
        CliRunner().invoke(cli, ["serve", str(tmp_path), *arguments])
    assert captured["config"].retry_failed is expected


@pytest.mark.parametrize(
    ("fields", "purpose"),
    (
        ({"user": "x", "full_archive": True, "scheduled": True}, "watchlist: whole archive"),
        ({"user": "x", "after": 1, "scheduled": True}, "watchlist: weekly check"),
        ({"posts": [{}], "scheduled": True}, "watchlist: removed gallery lookups"),
        ({"posts": [{}], "scheduled": True, "retry": True}, "retry of failed posts"),
        ({"user": "x", "full_archive": True}, "Download: whole archive"),
        ({"user": "x"}, "Download: new posts in archive"),
        ({"posts": [{}]}, "userscript posts: lookups"),
    ),
)
def test_request_purpose(fields: dict, purpose: str):
    assert DownloadServer._purpose(Job("id", "folder", **fields)) == purpose


def test_requests_endpoint(server, tmp_path: Path):
    from bdfr.request_log import RequestLog

    download_server, base = server
    assert call(base, "GET", "/requests") == (200, {"enabled": False})
    download_server.requests = RequestLog(tmp_path / "requests.jsonl")
    download_server.requests.record({"time": time.time(), "purpose": "watchlist: weekly check", "status": 200})
    status, body = call(base, "GET", "/requests")
    assert status == 200 and body["enabled"] is True
    assert body["totals"]["day"]["requests"] == 1


def test_load_token_is_stable(tmp_path: Path):
    token = load_token(tmp_path)
    assert len(token) > 20
    assert load_token(tmp_path) == token


def test_known_posts_from_file_names(server, tmp_path: Path):
    _, base = server
    folder = tmp_path / "someone"
    folder.mkdir()
    names = (
        "someone_A title_abc123.jpg",
        "someone_Gallery_with_underscores_def456_2.png",
        "2026-10-20_ghi789_New scheme title.mp4",
        "hash_list.json",
    )
    for name in names:
        (folder / name).write_text("x")
    status, result = call(
        base, "POST", "/known", {"folder": "someone", "ids": ["abc123", "def456", "ghi789", "zzz999", "2", "title"]}
    )
    assert status == 200
    assert result == {
        "known": ["abc123", "def456", "ghi789"],
        "known_by_link": [],
        "duplicates": [],
        "unavailable": [],
        "supported": True,
    }
    assert call(base, "POST", "/known", {"folder": "missing", "ids": ["abc123"]})[1]["known"] == []
    assert call(base, "POST", "/known", {"folder": "../x", "ids": []})[0] == 400


def test_known_posts_needs_post_id_scheme(server):
    download_server, base = server
    download_server.base_args.file_scheme = "{REDDITOR}_{TITLE}"
    result = call(base, "POST", "/known", {"folder": "someone", "ids": ["abc123"]})[1]
    assert result == {"known": [], "known_by_link": [], "duplicates": [], "unavailable": [], "supported": False}


def test_duplicates_are_counted_apart_from_skips():
    job = Job("j", "folder", posts=[{"id": "a"}, {"id": "b"}])
    job.record("a", None, "duplicate")
    job.record("b", None)
    assert job.results == {"a": "duplicate", "b": "skipped"}
    assert job.counts["duplicate"] == 1 and job.counts["skipped"] == 1


@pytest.mark.parametrize("store", ("db", "json"))
def test_known_posts_by_link(server, tmp_path: Path, store: str):
    """Reposts of an already downloaded link are known before anything is sent"""
    _, base = server
    folder = tmp_path / "someone"
    folder.mkdir()
    (folder / "abc123_Title.mp4").write_bytes(b"x")
    stored = ["https://www.redgifs.com/watch/somevideo", "https://i.redd.it/pic.jpg"]
    if store == "db":
        connection = sqlite3.connect(folder / "hashes.sqlite")
        connection.execute("CREATE TABLE hashes_url (url TEXT PRIMARY KEY, hash TEXT NOT NULL)")
        connection.executemany("INSERT INTO hashes_url VALUES (?, 'h')", [(url,) for url in stored])
        connection.commit()
        connection.close()
    else:
        (folder / "hash_url_list.json").write_text(json.dumps({url: "h" for url in stored}))
    links = {
        "abc123": "https://redgifs.com/watch/somevideo",  # known by its file name already
        "rep001": "https://i.redgifs.com/i/SomeVideo",  # the same Redgifs video, linked another way
        "rep002": "https://i.redd.it/pic.jpg",
        "new001": "https://i.redd.it/other.jpg",
    }
    result = call(base, "POST", "/known", {"folder": "someone", "ids": list(links), "links": links})[1]
    assert result["known"] == ["abc123", "rep001", "rep002"]
    assert result["known_by_link"] == ["rep001", "rep002"]


@pytest.mark.parametrize(
    ("extra", "expected"),
    (
        (
            [],
            {
                "no_dupes": True,
                "search_existing": True,
                "keep_hashes_db": True,
                "folder_scheme": "",
                "file_scheme": "{POSTID}_{TITLE}",
                "parallel": 4,
            },
        ),
        (
            ["--parallel", "2", "--file-scheme", "{TITLE}_{POSTID}"],
            {"parallel": 2, "file_scheme": "{TITLE}_{POSTID}", "no_dupes": True},
        ),
        (
            ["--no-defaults"],
            {"no_dupes": False, "keep_hashes_db": False, "folder_scheme": "{SUBREDDIT}", "parallel": 1},
        ),
    ),
)
def test_serve_defaults(tmp_path: Path, extra: list, expected: dict):
    from click.testing import CliRunner

    from bdfr.__main__ import cli

    captured = {}
    with patch("bdfr.__main__.run_server", lambda config, port: captured.update(config=config, port=port)):
        result = CliRunner().invoke(cli, ["serve", str(tmp_path), *extra])
    assert result.exit_code == 0, result.output
    config = captured["config"]
    for key, value in expected.items():
        assert getattr(config, key) == value, key


def test_serve_defaults_can_be_turned_off_in_opts(tmp_path: Path):
    from click.testing import CliRunner

    from bdfr.__main__ import cli

    opts = tmp_path / "opts.yaml"
    opts.write_text("no_dupes: false\nparallel: 1\n")
    captured = {}
    with patch("bdfr.__main__.run_server", lambda config, port: captured.update(config=config)):
        CliRunner().invoke(cli, ["serve", str(tmp_path), "--opts", str(opts)])
    assert captured["config"].no_dupes is False and captured["config"].parallel == 1
    assert captured["config"].keep_hashes_db is True


@pytest.mark.parametrize(("archive", "stop", "dead_pages"), ((None, 5, 0), ("all", 0, 2)))
def test_user_job_archive(server, archive, stop, dead_pages):
    _, base = server
    body = {"folder": "someone", "user": "someone", "posts": []}
    if archive:
        body["archive"] = archive
    _, job = call(base, "POST", "/jobs", body)
    assert wait_for(base, job["id"])["state"] == "done"
    args, _ = FakeDownloader.runs[0]
    assert args.user == ["someone"] and args.stop_at_existing == stop
    # A whole-archive read stops after two dead pages rather than a run of unavailable posts
    assert args.stop_at_dead_pages == dead_pages
    if archive:
        assert args.stop_at_unavailable == 0
    assert args.stop_at_bytes == 0


def test_user_job_limit_bytes(server):
    _, base = server
    body = {"folder": "someone", "user": "someone", "archive": "all", "limit_bytes": 5_000_000}
    _, job = call(base, "POST", "/jobs", body)
    assert wait_for(base, job["id"])["bytes_written"] == 0
    assert FakeDownloader.runs[0][0].stop_at_bytes == 5_000_000
    for bad in (0, -1, "5", True):
        assert call(base, "POST", "/jobs", dict(body, limit_bytes=bad))[0] == 400
    assert call(base, "POST", "/jobs", {"folder": "x", "posts": [{"id": "a"}], "limit_bytes": 5})[0] == 400


def test_known_duplicates(server, tmp_path: Path):
    _, base = server
    folder = tmp_path / "someone"
    folder.mkdir()
    connection = sqlite3.connect(folder / "hashes.sqlite")
    connection.execute("CREATE TABLE hashes_url (url TEXT PRIMARY KEY, hash TEXT NOT NULL)")
    connection.execute("INSERT INTO hashes_url VALUES ('post:dup123', 'h')")
    connection.commit()
    connection.close()
    result = call(base, "POST", "/known", {"folder": "someone", "ids": ["dup123", "new123"]})[1]
    assert result["duplicates"] == ["dup123"] and result["known"] == []


def test_user_job_starts_below_the_page(server):
    _, base = server
    _, job = call(base, "POST", "/jobs", {"folder": "someone", "user": "someone", "posts": [], "before": 1792210918})
    assert wait_for(base, job["id"])["state"] == "done"
    args, _ = FakeDownloader.runs[0]
    assert args.listing_before == 1792210918
    for bad in ("1792210918", -5, True, 1.5):
        assert call(base, "POST", "/jobs", {"folder": "someone", "user": "someone", "before": bad})[0] == 400


def test_user_job_after_reads_the_whole_window(server):
    _, base = server
    _, job = call(base, "POST", "/jobs", {"folder": "someone", "user": "someone", "after": 1792210918})
    assert wait_for(base, job["id"])["state"] == "done"
    args, _ = FakeDownloader.runs[0]
    assert args.listing_after == 1792210918
    assert args.stop_at_existing == 0


def test_watchlist_gone_endpoint(server, tmp_path: Path):
    from bdfr.scheduler import BaselineScheduler

    download_server, base = server
    assert call(base, "POST", "/watchlist/gone", {"user": "someone", "gone": "deleted"}) == (
        400,
        {"error": "serve has no watchlist"},
    )
    (tmp_path / "watchlist.yaml").write_text("users: [someone]\n", encoding="utf-8")
    download_server.scheduler = BaselineScheduler(download_server, tmp_path / "watchlist.yaml")
    assert call(base, "POST", "/watchlist/gone", {"user": "someone", "gone": "deleted"}) == (
        200,
        {"user": "someone", "gone": "deleted"},
    )
    _, watchlist = call(base, "GET", "/watchlist")
    assert watchlist["users"] == [] and watchlist["gone"][0]["name"] == "someone"
    assert call(base, "POST", "/watchlist/gone", {"user": "bad name!", "gone": None})[0] == 400
    assert call(base, "POST", "/watchlist/gone", {"user": "someone", "gone": None})[0] == 200
    assert [user["name"] for user in call(base, "GET", "/watchlist")[1]["users"]] == ["someone"]


def test_user_endpoint(server, tmp_path: Path):
    from bdfr.scheduler import BaselineScheduler

    download_server, base = server
    assert call(base, "GET", "/user?name=someone") == (
        200,
        {"name": "someone", "watchlist": False, "watched": None, "folder": "someone", "downloads": None},
    )
    assert call(base, "GET", "/user?name=bad%20name!")[0] == 400
    assert call(base, "GET", "/user")[0] == 400
    watchlist = "users:\n  - {name: someone, folder: main, aliases: [oldname]}\n  - left\n"
    (tmp_path / "watchlist.yaml").write_text(watchlist, encoding="utf-8")
    download_server.scheduler = BaselineScheduler(download_server, tmp_path / "watchlist.yaml")
    (tmp_path / "main" / "sub").mkdir(parents=True)
    # A gallery's two files are one post; a folder's own files aren't counted
    for name in ("abc123_a gallery_1.jpg", "abc123_a gallery_2.jpg", "def456_a video.mp4", "hashes.sqlite"):
        (tmp_path / "main" / name).write_bytes(b"x" * 100)
    (tmp_path / "main" / "sub" / "ghi789_an image_2024.png").write_bytes(b"x" * 100)
    status, body = call(base, "GET", "/user?name=Someone")
    assert status == 200
    assert body["watched"]["name"] == "someone" and body["folder"] == "main"
    assert body["downloads"] == {"file_count": 4, "bytes": 400, "videos": 1, "images": 3, "posts": 3}
    _, body = call(base, "GET", "/user?name=oldname")
    assert body["watched"]["alias_of"] == "someone" and body["folder"] == "main"
    assert call(base, "GET", "/user?name=nobody")[1]["watched"] is None
    download_server.scheduler.note_gone("left", "suspended")
    _, body = call(base, "GET", "/user?name=left")
    assert body["watched"]["gone"] == "suspended" and body["downloads"] is None


def test_retry_jobs_never_look_posts_up(server):
    download_server, base = server
    download_server.scheduler = MagicMock()
    download_server.scheduler.folder_for = lambda folder: folder
    download_server.scheduler.archive_lookups.return_value = True
    job = download_server.submit({"folder": "someone", "posts": [{"id": "a"}]}, scheduled=True, retry=True)
    assert wait_for(base, job.id)["retry"] is True
    args, _ = FakeDownloader.runs[0]
    assert args.archive_lookups is False
    # The posts were noted when they first came
    download_server.scheduler.note_posts.assert_not_called()


def test_recent_jobs(server):
    _, base = server
    _, first = call(base, "POST", "/jobs", {"folder": "one", "posts": [{"id": "a"}]})
    wait_for(base, first["id"])
    _, second = call(base, "POST", "/jobs", {"folder": "two", "user": "someone"})
    wait_for(base, second["id"])
    status, body = call(base, "GET", "/jobs")
    assert status == 200
    assert [job["id"] for job in body["jobs"]] == [second["id"], first["id"]]
    newest = body["jobs"][0]
    assert newest["folder"] == "two" and newest["user"] == "someone" and newest["state"] == "done"
    assert newest["finished"] >= newest["created"] and "results" not in newest
    assert body["jobs"][1]["counts"]["new"] == 1


def test_status_counts_scheduled_jobs(server, monkeypatch: pytest.MonkeyPatch):
    download_server, base = server
    started, release = threading.Event(), threading.Event()
    original = FakeDownloader.download

    def blocked(self):
        started.set()
        release.wait(5)
        original(self)

    monkeypatch.setattr(FakeDownloader, "download", blocked)
    running = download_server.submit({"folder": "one", "posts": [{"id": "a"}]})
    assert started.wait(5)
    scheduled = download_server.submit({"folder": "two", "user": "someone"}, scheduled=True)
    download_server.submit({"folder": "three", "posts": [{"id": "b"}]})
    try:
        _, status = call(base, "GET", "/status")
        assert status["running"] == running.id and status["queued"] == 2 and status["scheduled"] == 1
        jobs = {job["id"]: job for job in call(base, "GET", "/jobs")[1]["jobs"]}
        assert jobs[scheduled.id]["scheduled"] is True and jobs[scheduled.id]["started"] is None
        assert jobs[running.id]["scheduled"] is False and jobs[running.id]["started"] >= jobs[running.id]["created"]
    finally:
        release.set()
    assert wait_for(base, scheduled.id)["state"] == "done"
    assert call(base, "GET", "/status")[1]["scheduled"] == 0


def test_failed_jobs_are_notified(server, monkeypatch: pytest.MonkeyPatch):
    download_server, base = server
    download_server.notifier = MagicMock()

    def fail(self):
        raise RuntimeError("disk full")

    monkeypatch.setattr(FakeDownloader, "download", fail)
    _, job = call(base, "POST", "/jobs", {"folder": "someone", "posts": [{"id": "a"}]})
    assert wait_for(base, job["id"])["state"] == "failed"
    message = download_server.notifier.notify.call_args.args[0]
    assert message.startswith(f"Job {job['id']} failed") and "someone" in message and "disk full" in message


@pytest.mark.parametrize(
    ("arguments", "environment"),
    ((["--notify", "https://ntfy.example/bdfr"], {}), ([], {"BDFR_NOTIFY": "https://ntfy.example/bdfr"})),
)
def test_notify_option(tmp_path: Path, arguments: list, environment: dict):
    from click.testing import CliRunner

    from bdfr.__main__ import cli

    captured = {}
    with patch("bdfr.__main__.run_server", lambda config, port: captured.update(config=config)):
        CliRunner(env=environment).invoke(cli, ["serve", str(tmp_path), *arguments])
    assert captured["config"].notify == "https://ntfy.example/bdfr"


def test_dashboard_needs_no_token(server):
    _, base = server
    for path, kind in (("/", "text/html"), ("/dashboard.js", "text/javascript"), ("/dashboard.css", "text/css")):
        with urllib.request.urlopen(base + path, timeout=10) as response:
            assert response.status == 200 and response.headers["Content-Type"].startswith(kind)
            assert "script-src 'self'" in response.headers["Content-Security-Policy"]
            assert response.read()
    # Only the page's own files: the data still needs the token
    for path in ("/status", "/log", "/retries", "/folders", "/dashboard.html", "/../server.py"):
        assert call(base, "GET", path, token="")[0] == 401


def test_status_for_the_dashboard(server):
    download_server, base = server
    _, status = call(base, "GET", "/status")
    assert status["started"] == download_server.started and status["notify"] is False and status["log"] is None
    download_server.notifier = MagicMock()
    assert call(base, "GET", "/status")[1]["notify"] is True


def test_log_tail(server, tmp_path: Path):
    download_server, base = server
    assert call(base, "GET", "/log")[1] == {"path": None, "entries": []}
    log_path = tmp_path / "serve_log.txt"
    lines = [f"2026-10-21 09:00:{i % 60:02d} INFO    Line {i}" for i in range(3000)]
    lines += [
        "2026-10-21 09:09:15 WARNING job 3fd9e1485ded, post 1xbgyxi: failed - HTTP 500: x",
        "Traceback (most recent call last):",
        '  File "x.py", line 1',
        "2026-10-21 09:09:16 DEBUG   job 3fd9e1485ded: Options: --parallel 4",
        "2026-10-21 09:09:17 INFO    post abc123: new - 1 file written",
        "2026-10-21 09:09:18 INFO    Listening on http://127.0.0.1:7635",
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    download_server.log_path = log_path
    status, body = call(base, "GET", "/log?lines=4")
    assert status == 200 and body["path"] == str(log_path)
    assert body["entries"] == [
        {
            "time": "2026-10-21 09:09:15",
            "level": "WARNING",
            "job": "3fd9e1485ded",
            "post": "1xbgyxi",
            "message": 'failed - HTTP 500: x\nTraceback (most recent call last):\n  File "x.py", line 1',
        },
        {
            "time": "2026-10-21 09:09:16",
            "level": "DEBUG",
            "job": "3fd9e1485ded",
            "post": None,
            "message": "Options: --parallel 4",
        },
        {
            "time": "2026-10-21 09:09:17",
            "level": "INFO",
            "job": None,
            "post": "abc123",
            "message": "new - 1 file written",
        },
        {
            "time": "2026-10-21 09:09:18",
            "level": "INFO",
            "job": None,
            "post": None,
            "message": "Listening on http://127.0.0.1:7635",
        },
    ]
    # At most MAX_LOG_LINES lines, and 500 without lines=
    assert len(call(base, "GET", "/log?lines=99999")[1]["entries"]) <= 2000
    assert len(call(base, "GET", "/log")[1]["entries"]) <= 500
    assert call(base, "GET", "/log?lines=x")[0] == 400


def test_retry_list_endpoint(server, tmp_path: Path):
    from bdfr.retries import RetryList

    download_server, base = server
    assert call(base, "GET", "/retries")[1] == {"posts": [], "enabled": False}
    download_server.retries = RetryList(download_server, tmp_path / "failed_posts.json", clock=lambda: 1000.0)
    post = {"id": "abc123", "title": "A title", "url": "https://i.redd.it/x.jpg", "author": "someone", "score": 1}
    download_server.retries.note_job("pics", {"abc123": "failed"}, {"abc123": (post, "HTTP 500")})
    _, body = call(base, "GET", "/retries")
    assert body["enabled"] is True
    assert body["posts"] == [
        {
            "folder": "pics",
            "id": "abc123",
            "title": "A title",
            "url": "https://i.redd.it/x.jpg",
            "author": "someone",
            "error": "HTTP 500",
            "attempts": 1,
            "failed": 1000.0,
            "next_try": 1000.0 + 3600,
            "stopped": False,
            "pending": False,
        }
    ]
    # The dashboard's Stop: given up on, and later failures don't put it back
    assert call(base, "POST", "/retries/stop", {"folder": "pics", "id": "abc123"}) == (
        200,
        {"folder": "pics", "id": "abc123", "stopped": True},
    )
    download_server.retries.note_job("pics", {"abc123": "failed"}, {"abc123": (post, "HTTP 503")})
    entry = call(base, "GET", "/retries")[1]["posts"][0]
    assert entry["next_try"] is None and entry["stopped"] is True and entry["error"] == "HTTP 503"
    assert call(base, "POST", "/retries/stop", {"folder": "pics", "id": "nope"})[0] == 400
    assert call(base, "POST", "/retries/stop", {"folder": "pics"})[0] == 400


def test_folders_endpoint(server, tmp_path: Path):
    download_server, base = server
    database = tmp_path / "sqlite_folder" / "hashes.sqlite"
    database.parent.mkdir()
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE hashes (hash TEXT PRIMARY KEY, file_path TEXT NOT NULL)")
    connection.execute("CREATE TABLE hashes_url (url TEXT PRIMARY KEY, hash TEXT NOT NULL)")
    connection.executemany("INSERT INTO hashes VALUES (?, ?)", [("h1", "a.jpg"), ("h2", "b.jpg")])
    urls = ["https://i.redd.it/a.jpg", "post:d1", "unavailable:u1", "unavailable:u2", "incomplete:i1"]
    connection.executemany("INSERT INTO hashes_url VALUES (?, ?)", [(url, "h1") for url in urls])
    connection.commit()
    connection.close()
    json_folder = tmp_path / "json_folder"
    json_folder.mkdir()
    (json_folder / "hash_list.json").write_text(json.dumps({"h1": "a.jpg"}), encoding="utf-8")
    stored = {"https://x/a.jpg": "h1", "https://x/b.jpg": "h1", "unavailable:u1": "gone:404"}
    (json_folder / "hash_url_list.json").write_text(json.dumps(stored), encoding="utf-8")
    (tmp_path / "empty").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "hash_url_list.json").write_text("{", encoding="utf-8")
    download_server.retries = MagicMock()
    download_server.retries.listing.return_value = [{"folder": "Empty"}, {"folder": "empty"}]
    status, body = call(base, "GET", "/folders")
    assert status == 200
    folders = {folder["name"]: folder for folder in body["folders"]}
    assert list(folders) == ["broken", "empty", "json_folder", "sqlite_folder"]
    (tmp_path / "sqlite_folder" / "abc123_one.jpg").write_bytes(b"x" * 300)
    (tmp_path / "sqlite_folder" / "def456_two.mp4").write_bytes(b"x" * 700)
    (tmp_path / "sqlite_folder" / "ghi789_text.txt").write_bytes(b"x" * 50)
    status, body = call(base, "GET", "/folders")
    folders = {folder["name"]: folder for folder in body["folders"]}
    assert folders["sqlite_folder"] == {
        "name": "sqlite_folder",
        "file_count": 3,  # hashes.sqlite isn't counted
        "bytes": 1050,
        "videos": 1,
        "images": 1,
        "posts": 3,
        "retries": 0,
        "store": "sqlite",
        "files": 2,
        "links": 1,
        "duplicates": 1,
        "unavailable": 2,
        "incomplete": 1,
    }
    assert folders["json_folder"] == {
        "name": "json_folder",
        "file_count": 0,
        "bytes": 0,
        "videos": 0,
        "images": 0,
        "posts": 0,
        "retries": 0,
        "store": "json",
        "files": 1,
        "links": 2,
        "duplicates": 0,
        "unavailable": 1,
        "incomplete": 0,
    }
    assert folders["empty"] == {
        "name": "empty",
        "retries": 2,
        "store": None,
        "file_count": 0,
        "bytes": 0,
        "videos": 0,
        "images": 0,
        "posts": 0,
    }
    assert folders["broken"]["store"] == "error"
