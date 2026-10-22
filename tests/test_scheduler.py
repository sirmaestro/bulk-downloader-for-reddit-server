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
    newest_post = None
    gate = None  # a threading.Event the run waits for, to hold a job running

    def __init__(self, args: Configuration, log_file: bool = True):
        self.log_file = log_file
        self.args = args
        self.progress_callback = None
        self.errors = list(FakeDownloader.errors_to_report)
        self.newest_post = FakeDownloader.newest_post
        self.failed_posts = {}

    def download(self):
        FakeDownloader.runs.append(self.args)
        if FakeDownloader.gate is not None:
            assert FakeDownloader.gate.wait(10), "the test never released the job"


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
    FakeDownloader.gate = None
    FakeDownloader.errors_to_report = []
    FakeDownloader.newest_post = None
    with patch("bdfr.server.Downloader", FakeDownloader):
        yield DownloadServer(Configuration(), tmp_path / "root", "token")


def write_watchlist(path: Path, text: str, archive_checks: bool = True):
    """Writes the watchlist; most tests are about archive checks, which are off by default, so they're turned on
    unless the text sets archive_checks itself or archive_checks=False"""
    if archive_checks and "archive_checks:" in text:
        archive_checks = False
    path.write_text(("archive_checks: true\n" if archive_checks else "") + text, encoding="utf-8")


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
        {
            "name": "someone",
            "folder": "Some Folder",
            "archive": True,
            "full_archive_done": START,
            "last_success": START + 2 * DAY,
            "last_error": None,
            "next_check": START + 4 * DAY,
            "newest_post": None,
            "hibernating": False,
        }
    ]


@pytest.mark.parametrize(
    ("quiet_days", "hibernating"),
    ((1, False), (59, False), (61, True), (400, True)),
)
def test_users_quiet_for_hibernate_days_hibernate(serve, tmp_path, clock, quiet_days, hibernating):
    write_watchlist(tmp_path / "watchlist.yaml", "users: [someone]\n")
    scheduler = make_scheduler(serve, tmp_path, clock)
    FakeDownloader.newest_post = START - quiet_days * DAY
    tick(scheduler, serve)  # the first run measures the newest post
    user = scheduler.snapshot()["users"][0]
    assert user["newest_post"] == START - quiet_days * DAY
    assert user["hibernating"] is hibernating
    assert user["next_check"] == (None if hibernating else START + 7 * DAY)
    clock.now += 30 * DAY
    tick(scheduler, serve)
    # Quiet for 59 days at the first run: hibernating by the second
    assert len(FakeDownloader.runs) == (1 if quiet_days + 30 > 60 else 2)


def test_hibernating_from_saved_state_reads_nothing(serve, tmp_path, clock):
    write_watchlist(tmp_path / "watchlist.yaml", "users: [someone, other]\n")
    (tmp_path / "watchlist_state.json").write_text(
        json.dumps({"users": {"someone": {"newest_post": START - 90 * DAY}}}), encoding="utf-8"
    )
    scheduler = make_scheduler(serve, tmp_path, clock)
    tick(scheduler, serve)
    clock.now += BaselineScheduler.PAUSE_SECONDS
    tick(scheduler, serve)
    assert [run.user[0] for run in FakeDownloader.runs] == ["other"]


def test_posts_sent_to_the_server_wake_a_hibernating_user(serve, tmp_path, clock, caplog):
    write_watchlist(tmp_path / "watchlist.yaml", "users: [Someone]\n")
    serve.scheduler = scheduler = make_scheduler(serve, tmp_path, clock)
    FakeDownloader.newest_post = START - 90 * DAY
    tick(scheduler, serve)
    clock.now += 14 * DAY
    tick(scheduler, serve)
    assert len(FakeDownloader.runs) == 1
    assert scheduler.snapshot()["users"][0]["hibernating"] is True
    # The userscript's sync sends a new post (and one by someone not watched, which is ignored)
    post = {"id": "new0001", "author": "someone", "created_utc": clock.now - DAY, "url": "https://i.redd.it/x.jpg"}
    stranger = {"id": "str0001", "author": "stranger", "created_utc": clock.now, "url": "https://i.redd.it/y.jpg"}
    with caplog.at_level(logging.INFO):
        serve.submit({"folder": "Someone", "posts": [post, stranger]})
        settle(serve)
    assert "u/someone posted again" in caplog.text
    assert "stranger" not in scheduler.state
    user = scheduler.snapshot()["users"][0]
    assert user["hibernating"] is False and user["newest_post"] == clock.now - DAY
    FakeDownloader.newest_post = None  # the window has nothing new for the archive (yet)
    tick(scheduler, serve)
    assert [run.user for run in FakeDownloader.runs if run.user] == [["Someone"], ["Someone"]]
    # A window with no posts keeps the newest post known so far
    assert scheduler.snapshot()["users"][0]["newest_post"] == clock.now - DAY


def test_aliases_are_not_checked(serve, tmp_path, clock, caplog):
    write_watchlist(
        tmp_path / "watchlist.yaml",
        "users:\n  - name: new_account\n    aliases: [old_account, 'not a name', new_account]\n  - old_account\n",
    )
    scheduler = make_scheduler(serve, tmp_path, clock)
    with caplog.at_level(logging.WARNING):
        tick(scheduler, serve)
    assert "'not a name'" in caplog.text and "u/new_account is listed more than once" in caplog.text
    assert "u/old_account is listed more than once" in caplog.text
    users = scheduler.snapshot()["users"]
    assert [(u["name"], u["folder"], u.get("alias_of"), u["next_check"]) for u in users] == [
        ("new_account", "new_account", None, START + 7 * DAY),
        ("old_account", "new_account", "new_account", None),
    ]
    for _ in range(3):
        clock.now += BaselineScheduler.PAUSE_SECONDS
        tick(scheduler, serve)
    assert [run.user[0] for run in FakeDownloader.runs] == ["new_account"]


def test_old_accounts_page_names_the_alias_folder(serve, tmp_path, clock):
    write_watchlist(tmp_path / "watchlist.yaml", "users:\n  - name: new_account\n    aliases: [Old_Account]\n")
    serve.scheduler = scheduler = make_scheduler(serve, tmp_path, clock)
    tick(scheduler, serve)  # the scheduler's own job (submitted under its lock) must not wait for the lock again
    post = {"id": "abc0001", "author": "Old_Account", "created_utc": START, "url": "https://i.redd.it/x.jpg"}
    job = serve.submit({"folder": "old_account", "posts": [post]})
    settle(serve)
    assert job.folder == "new_account"
    assert Path(FakeDownloader.runs[-1].directory).name == "new_account"
    (serve.root / "new_account").mkdir(parents=True, exist_ok=True)
    (serve.root / "new_account" / "abc0001_Title.jpg").write_bytes(b"x")
    assert serve.known_posts({"folder": "Old_Account", "ids": ["abc0001"]})["known"] == ["abc0001"]
    assert serve.submit({"folder": "other", "posts": [post]}).folder == "other"


def test_userscript_jobs_run_before_scheduled_jobs(serve, tmp_path, clock):
    write_watchlist(tmp_path / "watchlist.yaml", "users: [watched]\n")
    scheduler = make_scheduler(serve, tmp_path, clock)
    FakeDownloader.gate = gate = threading.Event()
    post = {"id": "abc0001", "url": "https://i.redd.it/x.jpg"}
    serve.submit({"folder": "first", "posts": [post]})
    deadline = time.time() + 10
    while not FakeDownloader.runs and time.time() < deadline:
        time.sleep(0.02)
    scheduler.tick()  # queued behind the running job
    serve.submit({"folder": "second", "posts": [post]})  # queued after it, but runs first
    gate.set()
    settle(serve)
    assert [Path(run.directory).name for run in FakeDownloader.runs] == ["first", "second", "watched"]


def test_archive_checks_are_off_by_default(serve, tmp_path, clock):
    write_watchlist(tmp_path / "watchlist.yaml", "users: [someone, other]\n", archive_checks=False)
    scheduler = make_scheduler(serve, tmp_path, clock)
    for _ in range(3):
        tick(scheduler, serve)
        clock.now += 30 * DAY
    assert FakeDownloader.runs == []
    user = scheduler.snapshot()["users"][0]
    assert user["archive"] is False and user["next_check"] is None


def test_userscript_posts_are_looked_up_in_the_archive_only_with_archive_checks(serve, tmp_path, clock):
    watchlist = tmp_path / "watchlist.yaml"
    serve.scheduler = scheduler = make_scheduler(serve, tmp_path, clock)
    post = {"id": "abc0001", "url": "https://www.reddit.com/gallery/abc0001"}

    def lookups(payload: dict) -> bool:
        serve.submit(payload)
        settle(serve)
        return FakeDownloader.runs[-1].archive_lookups

    assert lookups({"folder": "someone", "posts": [post]})  # no watchlist file: as before
    text = "users:\n  - someone\n  - name: keen\n    archive: true\n  - name: new_account\n    aliases: [old]\n"
    write_watchlist(watchlist, text, archive_checks=False)
    tick(scheduler, serve)
    FakeDownloader.runs.clear()
    assert not lookups({"folder": "someone", "posts": [post]})
    assert not lookups({"folder": "old", "posts": [post]})  # an alias: its account's folder
    assert not lookups({"folder": "unwatched", "posts": [post]})
    assert lookups({"folder": "keen", "posts": [post]})
    assert lookups({"folder": "someone", "user": "someone", "posts": [post]})  # reads the archive anyway
    write_watchlist(watchlist, text)  # archive_checks: true
    tick(scheduler, serve)
    FakeDownloader.runs.clear()
    assert lookups({"folder": "unwatched", "posts": [post]})


def test_archive_true_reads_one_users_whole_archive_once_then_windows(serve, tmp_path, clock):
    write_watchlist(
        tmp_path / "watchlist.yaml",
        "users:\n  - someone\n  - name: wanted\n    archive: true\n",
        archive_checks=False,
    )
    # Checked before (e.g. seeded), but never read in full
    (tmp_path / "watchlist_state.json").write_text(
        json.dumps(
            {"users": {"wanted": {"last_attempt": START - DAY, "last_success": START - DAY, "window_end": START - DAY}}}
        ),
        encoding="utf-8",
    )
    scheduler = make_scheduler(serve, tmp_path, clock)
    tick(scheduler, serve)
    first = FakeDownloader.runs[-1]
    assert first.user == ["wanted"] and first.listing_after is None and first.stop_at_existing == 0
    assert scheduler.snapshot()["users"][1]["full_archive_done"] == START
    clock.now += 7 * DAY
    tick(scheduler, serve)
    assert [run.user for run in FakeDownloader.runs] == [["wanted"], ["wanted"]]
    assert FakeDownloader.runs[-1].listing_after == START - 7 * DAY  # a window from now on


def test_own_archive_true_is_read_once_even_while_hibernating(serve, tmp_path, clock):
    write_watchlist(
        tmp_path / "watchlist.yaml",
        "users:\n  - name: banned\n    archive: true\n  - name: quiet\n",
        archive_checks=True,
    )
    old = {"newest_post": START - 300 * DAY}
    (tmp_path / "watchlist_state.json").write_text(
        json.dumps({"users": {"banned": dict(old), "quiet": dict(old)}}), encoding="utf-8"
    )
    scheduler = make_scheduler(serve, tmp_path, clock)
    for _ in range(3):
        tick(scheduler, serve)
        clock.now += 8 * DAY
    # banned: its whole archive once, then hibernating like everyone else; quiet (archive from archive_checks): never
    assert [run.user for run in FakeDownloader.runs] == [["banned"]]


def test_archive_false_overrides_archive_checks_and_bad_values_warn(serve, tmp_path, clock, caplog):
    write_watchlist(
        tmp_path / "watchlist.yaml",
        "archive_checks: maybe\nusers:\n  - name: skipped\n    archive: false\n  - name: odd\n    archive: sometimes\n",
    )
    with caplog.at_level(logging.WARNING):
        scheduler = make_scheduler(serve, tmp_path, clock)
        tick(scheduler, serve)
    assert "'archive_checks' must be true or false" in caplog.text
    assert "u/odd's 'archive' must be true or false" in caplog.text
    assert FakeDownloader.runs == []


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
    FakeDownloader.newest_post = None
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
    FakeDownloader.newest_post = None
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
    threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True).start()
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
        serve = DownloadServer(args, tmp_path / "root", "token")
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


def test_gone_users_are_left_out_and_not_checked_until_recheck(serve, tmp_path, clock, caplog):
    caplog.set_level(logging.INFO)
    write_watchlist(tmp_path / "watchlist.yaml", "gone_recheck_days: 30\nusers: [someone, other]\n")
    serve.scheduler = scheduler = make_scheduler(serve, tmp_path, clock)
    scheduler.note_gone("Someone", "deleted")
    assert "u/someone is deleted on Reddit; not synced or checked for 30 days" in caplog.text
    snapshot = scheduler.snapshot()
    assert [user["name"] for user in snapshot["users"]] == ["other"]
    assert snapshot["gone"] == [{"name": "someone", "gone": "deleted", "recheck": clock.now + 30 * DAY}]
    tick(scheduler, serve)
    assert [run.user for run in FakeDownloader.runs] == [["other"]]  # no archive checks for someone

    # Listed again for the userscript to look at Reddit, still without archive checks
    clock.now += 30 * DAY
    user = next(user for user in scheduler.snapshot()["users"] if user["name"] == "someone")
    assert user["gone"] == "deleted" and user["next_check"] is None
    scheduler.note_gone("someone", "deleted")  # still gone: left out for another 30 days
    assert [user["name"] for user in scheduler.snapshot()["users"]] == ["other"]
    scheduler.note_gone("someone", None)  # there after all
    assert "gone" not in scheduler.snapshot()["users"][0]
    assert make_scheduler(serve, tmp_path, clock).state["someone"].get("gone") is None


def test_posts_from_reddit_bring_a_gone_user_back(serve, tmp_path, clock):
    write_watchlist(tmp_path / "watchlist.yaml", "users: [someone]\n", archive_checks=False)
    serve.scheduler = scheduler = make_scheduler(serve, tmp_path, clock)
    scheduler.note_gone("someone", "suspended")
    assert scheduler.snapshot()["users"] == []
    scheduler.note_posts([{"id": "abc0001", "author": "someone", "created_utc": START}])
    assert [user["name"] for user in scheduler.snapshot()["users"]] == ["someone"]


@pytest.mark.parametrize(
    ("name", "reason", "error"),
    (
        ("unknown", "deleted", "isn't on the watchlist"),
        ("old", "deleted", "isn't on the watchlist"),
        ("someone", "gone", "must be"),
    ),
)
def test_note_gone_rejects_bad_reports(serve, tmp_path, clock, name, reason, error):
    write_watchlist(tmp_path / "watchlist.yaml", "users:\n  - name: someone\n    aliases: [old]\n")
    scheduler = make_scheduler(serve, tmp_path, clock)
    with pytest.raises(ValueError, match=error):
        scheduler.note_gone(name, reason)


def test_gone_users_who_posted_recently_are_synced_as_usual_then_hidden(serve, tmp_path, clock, caplog):
    caplog.set_level(logging.INFO)
    write_watchlist(tmp_path / "watchlist.yaml", "users: [someone]\n")
    serve.scheduler = scheduler = make_scheduler(serve, tmp_path, clock)
    scheduler.snapshot()  # reads the watchlist
    scheduler.note_posts([{"id": "abc0001", "author": "someone", "created_utc": clock.now - 2 * DAY}])
    scheduler.note_gone("someone", "deleted")  # maybe shadowbanned
    assert "still synced while their newest post is under 14 days old" in caplog.text
    user = scheduler.snapshot()["users"][0]
    assert user["gone"] == "deleted" and user["next_check"] is None  # listed for the sync, no archive checks
    tick(scheduler, serve)
    assert FakeDownloader.runs == []
    # 14 days after the newest post: left out, and looked at again 30 days after the last report
    clock.now += 12 * DAY
    assert scheduler.snapshot()["users"] == []
    clock.now += 17 * DAY
    assert scheduler.snapshot()["users"] == []
    clock.now += 1 * DAY  # 30 days after the report
    assert scheduler.snapshot()["users"][0]["gone"] == "deleted"
