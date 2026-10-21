#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""The weekly Arctic Shift baseline for watched users (bdfr/scheduler.py)"""

import importlib.resources
import json
import logging
import shutil
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from bdfr.__main__ import SERVE_DEFAULTS
from bdfr.configuration import Configuration
from bdfr.scheduler import BaselineScheduler
from bdfr.server import DownloadServer, make_handler
from tests.mock_arctic_shift import MockArcticShift

DAY = 86400
START = 1_790_000_000


class FakeDownloader:
    runs = []
    errors_to_report = []

    def __init__(self, args: Configuration):
        self.args = args
        self.progress_callback = None
        self.errors = list(FakeDownloader.errors_to_report)

    def download(self):
        FakeDownloader.runs.append(self.args)


class Clock:
    def __init__(self, now: float = START):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def serve(tmp_path: Path):
    FakeDownloader.runs = []
    FakeDownloader.errors_to_report = []
    with patch("bdfr.server.RedditDownloader", FakeDownloader):
        yield DownloadServer(Configuration(), tmp_path / "root", "token", tmp_path / "serve_log.txt")


def write_watchlist(path: Path, text: str):
    path.write_text(text, encoding="utf-8")


def make_scheduler(serve: DownloadServer, tmp_path: Path, clock: Clock) -> BaselineScheduler:
    return BaselineScheduler(serve, tmp_path / "watchlist.yaml", clock=clock)


def settle(serve: DownloadServer):
    """Waits for queued jobs to finish"""
    deadline = time.time() + 10
    while serve.current or serve._queue.qsize() or any(not job.finished for job in serve.jobs.values()):
        if time.time() > deadline:
            raise AssertionError("jobs did not finish")
        time.sleep(0.02)


def tick(scheduler: BaselineScheduler, serve: DownloadServer):
    scheduler.tick()
    settle(serve)


def test_no_watchlist_does_nothing(serve, tmp_path, clock):
    scheduler = make_scheduler(serve, tmp_path, clock)
    tick(scheduler, serve)
    assert FakeDownloader.runs == []
    assert scheduler.snapshot()["users"] == []


def test_first_run_reads_whole_archive_then_weekly_window(serve, tmp_path, clock):
    write_watchlist(tmp_path / "watchlist.yaml", "users:\n  - someone\n")
    scheduler = make_scheduler(serve, tmp_path, clock)
    tick(scheduler, serve)
    first = FakeDownloader.runs[-1]
    assert first.user == ["someone"] and first.stop_at_existing == 0 and first.listing_after is None
    assert Path(first.directory).name == "someone"

    clock.now += 6 * DAY
    tick(scheduler, serve)
    assert len(FakeDownloader.runs) == 1  # not due yet

    clock.now += DAY
    tick(scheduler, serve)
    second = FakeDownloader.runs[-1]
    # The window starts a week (the overlap) before the previous run
    assert second.listing_after == START - 7 * DAY
    assert second.stop_at_existing == 0

    clock.now += 7 * DAY
    tick(scheduler, serve)
    assert FakeDownloader.runs[-1].listing_after == START + 7 * DAY - 7 * DAY


def test_settings_and_folders(serve, tmp_path, clock):
    write_watchlist(
        tmp_path / "watchlist.yaml",
        "interval_days: 2\noverlap_days: 1\nusers:\n  - name: someone\n    folder: Some Folder\n",
    )
    scheduler = make_scheduler(serve, tmp_path, clock)
    tick(scheduler, serve)
    assert Path(FakeDownloader.runs[-1].directory).name == "Some Folder"
    clock.now += 2 * DAY
    tick(scheduler, serve)
    assert FakeDownloader.runs[-1].listing_after == START - DAY
    assert scheduler.snapshot()["users"] == [
        {"name": "someone", "folder": "Some Folder", "last_success": START + 2 * DAY, "last_error": None,
         "next_check": START + 4 * DAY}
    ]


def test_one_scheduled_job_at_a_time(serve, tmp_path, clock):
    write_watchlist(tmp_path / "watchlist.yaml", "users: [one, two, three]\n")
    scheduler = make_scheduler(serve, tmp_path, clock)
    scheduler.tick()
    scheduler.tick()  # the first job is queued, running or only just finished: nothing more is queued
    settle(serve)
    tick(scheduler, serve)
    assert [run.user for run in FakeDownloader.runs] == [["one"]]
    for _ in range(2):
        clock.now += BaselineScheduler.PAUSE_SECONDS
        tick(scheduler, serve)
    assert [run.user[0] for run in FakeDownloader.runs] == ["one", "two", "three"]


def test_failed_run_keeps_window_and_retries_later(serve, tmp_path, clock, caplog):
    write_watchlist(tmp_path / "watchlist.yaml", "users: [someone]\n")
    scheduler = make_scheduler(serve, tmp_path, clock)
    tick(scheduler, serve)  # succeeds: window ends at START

    clock.now += 7 * DAY
    FakeDownloader.errors_to_report = ["Arctic Shift request failed after 5 attempts: HTTP 503"]
    with caplog.at_level(logging.WARNING):
        tick(scheduler, serve)
    assert "u/someone" in caplog.text and "HTTP 503" in caplog.text
    user = scheduler.snapshot()["users"][0]
    assert "HTTP 503" in user["last_error"] and user["last_success"] == START

    FakeDownloader.errors_to_report = []
    clock.now += 3600
    tick(scheduler, serve)
    assert len(FakeDownloader.runs) == 2  # not retried within the retry delay

    clock.now += DAY
    tick(scheduler, serve)
    # The failed window is read again, from the last successful run
    assert FakeDownloader.runs[-1].listing_after == START - 7 * DAY
    assert scheduler.snapshot()["users"][0]["last_error"] is None


def test_never_successful_user_retries_whole_archive(serve, tmp_path, clock):
    write_watchlist(tmp_path / "watchlist.yaml", "users: [someone]\n")
    scheduler = make_scheduler(serve, tmp_path, clock)
    FakeDownloader.errors_to_report = ["Stopped"]
    tick(scheduler, serve)
    FakeDownloader.errors_to_report = []
    clock.now += DAY
    tick(scheduler, serve)
    assert FakeDownloader.runs[-1].listing_after is None and FakeDownloader.runs[-1].stop_at_existing == 0


def test_state_survives_restart(serve, tmp_path, clock):
    write_watchlist(tmp_path / "watchlist.yaml", "users: [someone]\n")
    tick(make_scheduler(serve, tmp_path, clock), serve)
    state = json.loads((tmp_path / "watchlist_state.json").read_text(encoding="utf-8"))
    assert state["users"]["someone"]["window_end"] == START
    clock.now += DAY
    restarted = make_scheduler(serve, tmp_path, clock)
    tick(restarted, serve)
    assert len(FakeDownloader.runs) == 1


def test_bad_watchlist_warns_and_keeps_previous(serve, tmp_path, clock, caplog):
    path = tmp_path / "watchlist.yaml"
    write_watchlist(path, "users: [someone]\n")
    scheduler = make_scheduler(serve, tmp_path, clock)
    tick(scheduler, serve)
    write_watchlist(path, "users: [someone\n")  # broken YAML
    with caplog.at_level(logging.WARNING):
        scheduler.tick()
    assert "watchlist" in caplog.text.lower()
    assert [user["name"] for user in scheduler.snapshot()["users"]] == ["someone"]


def test_bad_entries_are_skipped_with_a_warning(serve, tmp_path, clock, caplog):
    write_watchlist(
        tmp_path / "watchlist.yaml",
        "users:\n  - good_one\n  - not a user!\n  - name: other\n    folder: ../escape\n  - 42\n  - Good_One\n",
    )
    with caplog.at_level(logging.WARNING):
        scheduler = make_scheduler(serve, tmp_path, clock)
        scheduler.tick()
    assert [user["name"] for user in scheduler.snapshot()["users"]] == ["good_one"]
    assert "not a user!" in caplog.text and "../escape" in caplog.text and "42" in caplog.text
    assert "Good_One" in caplog.text  # listed twice


def test_watchlist_endpoint(serve, tmp_path, clock):
    write_watchlist(tmp_path / "watchlist.yaml", "users: [someone]\n")
    serve.scheduler = make_scheduler(serve, tmp_path, clock)
    tick(serve.scheduler, serve)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(serve))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        request = urllib.request.Request(base + "/watchlist", headers={"X-BDFR-Token": "token"})
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read())
        assert body["users"][0]["name"] == "someone" and body["users"][0]["folder"] == "someone"
        assert body["interval_days"] == 7
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(base + "/watchlist"), timeout=10)
        assert error.value.code == 401
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_serve_watchlist_option(tmp_path: Path):
    from click.testing import CliRunner

    from bdfr.__main__ import cli

    captured = {}
    with patch("bdfr.__main__.run_server", lambda config, port: captured.update(config=config)):
        CliRunner().invoke(cli, ["serve", str(tmp_path), "--watchlist", str(tmp_path / "users.yaml")])
    assert captured["config"].watchlist == str(tmp_path / "users.yaml")


def test_watchlist_endpoint_without_scheduler(serve):
    assert serve.watchlist() == {"users": [], "enabled": False}


def test_weekly_baseline_against_mock_archive(tmp_path, clock):
    """The real downloader: the first run takes the whole archive, a week later only what is missing"""
    archive = MockArcticShift(page_size=2)
    with archive.running():
        config = tmp_path / "config.cfg"
        with importlib.resources.path("bdfr", "default_config.cfg") as path:
            shutil.copy(path, config)
        args = Configuration()
        args.config = str(config)
        for key, value in SERVE_DEFAULTS.items():
            setattr(args, key, value)
        args.parallel = 1
        serve = DownloadServer(args, tmp_path / "root", "token", tmp_path / "serve_log.txt")
        write_watchlist(tmp_path / "watchlist.yaml", "users: [someone]\n")
        scheduler = make_scheduler(serve, tmp_path, clock)
        for number in range(3):
            archive.add_post(f"abc000{number}", START - DAY - number * 100)
        tick(scheduler, serve)
        assert len(list((tmp_path / "root" / "someone").glob("*.jpg"))) == 3

        # During the week: a new post, and one from before the last run that the archive only got now
        archive.add_post("new0001", START + 3 * DAY)
        archive.add_post("late001", START - 2 * DAY)
        archive.add_post("ancient", START - 30 * DAY)  # outside the window: not read
        archive.requests.clear()
        clock.now += 7 * DAY
        tick(scheduler, serve)
        names = sorted(p.name.split("_")[0] for p in (tmp_path / "root" / "someone").glob("*.jpg"))
        assert names == ["abc0000", "abc0001", "abc0002", "late001", "new0001"]
        assert all(params["after"] == str(START - 7 * DAY) for params in archive.endpoint_requests("posts/search"))
        assert scheduler.snapshot()["users"][0]["last_error"] is None
