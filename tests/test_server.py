#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from bdfr.configuration import Configuration
from bdfr.server import DownloadServer, Job, load_token, make_handler

TOKEN = "test-token"


class FakeDownloader:
    """Stands in for Downloader, recording the arguments each job ran with"""

    runs = []

    def __init__(self, args: Configuration):
        self.args = args
        self.progress_callback = None
        self.errors = []
        self.newest_post = None

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
    with patch("bdfr.server.Downloader", FakeDownloader):
        args = Configuration()
        args.stop_at_existing = 0
        download_server = DownloadServer(args, tmp_path, TOKEN, tmp_path / "serve_log.txt")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(download_server))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
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
    assert job["counts"] == {"existing": 1, "new": 1, "duplicate": 0, "skipped": 1}
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


def test_jobs_keep_log_history(server):
    """Each job's downloader opens the serve log; it must append (rotating by size), not start a new file per job"""
    _, base = server
    job = wait_for(base, call(base, "POST", "/jobs", {"folder": "a", "user": "someone"})[1]["id"])
    assert job["state"] == "done"
    args, _ = FakeDownloader.runs[0]
    assert args.log_rotate_bytes >= 1024 * 1024


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


def test_serve_host_option(tmp_path: Path):
    from click.testing import CliRunner

    from bdfr.__main__ import cli

    captured = {}
    with patch("bdfr.__main__.run_server", lambda config, port: captured.update(config=config)):
        CliRunner().invoke(cli, ["serve", str(tmp_path), "--host", "0.0.0.0"])
    assert captured["config"].host == "0.0.0.0"


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
    assert result == {"known": ["abc123", "def456", "ghi789"], "known_by_link": [], "duplicates": [], "supported": True}
    assert call(base, "POST", "/known", {"folder": "missing", "ids": ["abc123"]})[1]["known"] == []
    assert call(base, "POST", "/known", {"folder": "../x", "ids": []})[0] == 400


def test_known_posts_needs_post_id_scheme(server):
    download_server, base = server
    download_server.base_args.file_scheme = "{REDDITOR}_{TITLE}"
    result = call(base, "POST", "/known", {"folder": "someone", "ids": ["abc123"]})[1]
    assert result == {"known": [], "known_by_link": [], "duplicates": [], "supported": False}


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
                "parallel": 8,
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


@pytest.mark.parametrize(("archive", "stop"), ((None, 5), ("all", 0)))
def test_user_job_archive(server, archive, stop):
    _, base = server
    body = {"folder": "someone", "user": "someone", "posts": []}
    if archive:
        body["archive"] = archive
    _, job = call(base, "POST", "/jobs", body)
    assert wait_for(base, job["id"])["state"] == "done"
    args, _ = FakeDownloader.runs[0]
    assert args.user == ["someone"] and args.stop_at_existing == stop


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
