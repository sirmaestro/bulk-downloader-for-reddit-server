#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""serve jobs run with the real downloader against a mock Arctic Shift (tests/mock_arctic_shift.py)"""

import importlib.resources
import shutil
import time
from pathlib import Path

import pytest

from bdfr.__main__ import SERVE_DEFAULTS
from bdfr.configuration import Configuration
from bdfr.resource import Resource
from bdfr.retries import HOUR, RetryList
from bdfr.scheduler import BaselineScheduler
from bdfr.server import DownloadServer, Job
from bdfr.site_downloaders.base_downloader import BaseDownloader
from bdfr.site_downloaders.download_factory import DownloadFactory
from tests.mock_arctic_shift import MockArcticShift

NOW = 1_790_000_000


@pytest.fixture()
def archive():
    mock = MockArcticShift(page_size=3)
    with mock.running():
        yield mock


@pytest.fixture()
def serve(tmp_path: Path, archive: MockArcticShift) -> DownloadServer:
    config = tmp_path / "config.cfg"
    with importlib.resources.path("bdfr", "default_config.cfg") as path:
        shutil.copy(path, config)
    args = Configuration()
    args.config = str(config)
    for key, value in SERVE_DEFAULTS.items():
        setattr(args, key, value)
    args.parallel = 1
    root = tmp_path / "root"
    root.mkdir()
    return DownloadServer(args, root, "token")


def run(serve: DownloadServer, payload: dict) -> Job:
    return wait(serve.submit(payload))


def wait(job: Job) -> Job:
    deadline = time.time() + 30
    while not job.finished:
        if time.time() > deadline:
            raise AssertionError("job did not finish")
        time.sleep(0.05)
    return job


def files(serve: DownloadServer, folder: str) -> list[str]:
    return sorted(p.name for p in (serve.root / folder).iterdir() if p.suffix == ".jpg")


def test_user_job_downloads_from_the_archive(serve: DownloadServer, archive: MockArcticShift):
    for number in range(5):
        archive.add_post(f"abc000{number}", NOW - number * 100)
    job = run(serve, {"folder": "someone", "user": "someone", "archive": "all"})
    assert job.state == "done", job.error
    assert job.counts["new"] == 5
    assert len(files(serve, "someone")) == 5
    # Listing pages only; the posts link straight to media, so nothing is looked up
    assert archive.endpoint_requests("posts/search")
    assert archive.endpoint_requests("posts/ids") == []
    # The scheduler's measure of how quiet the user is
    assert job.newest_post == NOW


@pytest.mark.parametrize(
    "failure",
    (
        {"status": 400, "error": "Invalid parameter"},  # rejected at once
        {"status": 503, "times": 5},  # still failing after every retry
        {"drop": True, "times": 5},  # connection closed without an answer
        {"invalid_json": True, "status": 200, "times": 5},  # e.g. a proxy's HTML error page
    ),
    ids=("rejected", "unavailable", "dropped", "not-json"),
)
def test_failed_listing_fails_the_job(serve: DownloadServer, archive: MockArcticShift, failure: dict):
    """A listing that couldn't be read must not look like a user with nothing new"""
    archive.add_post("abc0001", NOW)
    archive.fail("posts/search", **failure)
    job = run(serve, {"folder": "someone", "user": "someone", "archive": "all"})
    assert job.state == "failed"
    assert "Arctic Shift" in job.error
    assert files(serve, "someone") == []


def test_listing_failing_part_way_keeps_what_was_downloaded(serve: DownloadServer, archive: MockArcticShift):
    for number in range(5):
        archive.add_post(f"abc000{number}", NOW - number * 100)
    job_payload = {"folder": "someone", "user": "someone", "archive": "all"}
    # The first page is answered, every attempt at the second fails
    archive.failures["posts/search"] = [None] + [
        {"status": 503, "error": None, "drop": False, "invalid_json": False}
    ] * 5
    job = run(serve, job_payload)
    assert job.state == "failed"
    assert job.counts["new"] == 3
    assert len(files(serve, "someone")) == 3


DAY = 86400


def test_user_job_after_reads_only_the_window(serve: DownloadServer, archive: MockArcticShift):
    archive.add_post("old0001", NOW - 10 * DAY)
    archive.add_post("new0001", NOW - 5 * DAY)
    archive.add_post("new0002", NOW - DAY)
    job = run(serve, {"folder": "someone", "user": "someone", "after": NOW - 7 * DAY})
    assert job.state == "done", job.error
    assert sorted(job.results) == ["new0001", "new0002"]
    assert job.newest_post == NOW - DAY
    searches = archive.endpoint_requests("posts/search")
    assert searches and all(params.get("after") == str(NOW - 7 * DAY) for params in searches)


def test_user_job_after_finds_gaps_behind_downloaded_posts(serve: DownloadServer, archive: MockArcticShift):
    """The window is read to its end: a missing post older than a run of downloaded ones is still found"""
    for number in range(6):
        archive.add_post(f"abc000{number}", NOW - number * 100)
    assert run(serve, {"folder": "someone", "user": "someone", "archive": "all"}).counts["new"] == 6
    # A post the archive only has now (ingested late), older than six downloaded ones
    archive.add_post("late001", NOW - 1000)
    plain = run(serve, {"folder": "someone", "user": "someone"})
    assert "late001" not in plain.results  # stops after 5 downloaded posts in a row
    job = run(serve, {"folder": "someone", "user": "someone", "after": NOW - DAY})
    assert job.state == "done", job.error
    assert job.results["late001"] == "new"
    assert job.counts["existing"] == 6


def test_failed_lookup_of_given_posts_fails_the_job(serve: DownloadServer, archive: MockArcticShift):
    """Galleries without gallery_data are looked up in the archive; if that fails the job must say so"""
    archive.add_post("abc0001", NOW, url="https://www.reddit.com/gallery/abc0001")
    archive.fail("posts/ids", status=400, error="Too many ids")
    job = run(serve, {"folder": "someone", "posts": [dict(archive.posts["abc0001"])]})
    assert job.state == "failed"
    assert "Arctic Shift" in job.error


def test_posts_for_users_without_archive_checks_are_not_looked_up(
    serve: DownloadServer, archive: MockArcticShift, tmp_path: Path
):
    """A gallery Reddit dropped the image list of fails as removed instead of being looked up in the archive"""
    (tmp_path / "watchlist.yaml").write_text("users: [someone]\n", encoding="utf-8")
    serve.scheduler = BaselineScheduler(serve, tmp_path / "watchlist.yaml")
    serve.scheduler.tick()
    archive.add_post("abc0001", NOW, url="https://www.reddit.com/gallery/abc0001")
    post = dict(archive.posts["abc0001"], removed_by_category="moderator")
    post.pop("gallery_data", None)
    job = run(serve, {"folder": "someone", "posts": [post]})
    assert job.state == "done"
    assert job.results == {"abc0001": "failed"}
    assert archive.requests == []


@pytest.mark.parametrize("store", ("db", "json"))
def test_post_failing_part_way_is_not_known_until_finished(
    serve: DownloadServer, archive: MockArcticShift, monkeypatch: pytest.MonkeyPatch, store: str
):
    """A post whose second file fails after the first was written (e.g. a gallery) has its ID in a file name, but must
    not count as downloaded until a later job finishes it"""
    serve.base_args.keep_hashes_db = store == "db"
    serve.base_args.keep_hashes = store == "json"
    post = archive.add_post("abc0001", NOW)
    urls = [f"{archive.base}/media/abc0001.jpg", f"{archive.base}/media/abc0001_b.jpg"]

    class TwoFiles(BaseDownloader):
        def find_resources(self) -> list[Resource]:
            return [Resource(self.post, url, Resource.retry_download(url)) for url in urls]

    monkeypatch.setattr(DownloadFactory, "pull_lever", staticmethod(lambda url: TwoFiles))
    known = {"folder": "someone", "ids": ["abc0001"]}

    run(serve, {"folder": "someone", "posts": [post]})  # the mock has no second file yet: 404
    assert len(files(serve, "someone")) == 1
    assert serve.known_posts(known)["known"] == []

    archive.media["abc0001_b.jpg"] = b"second image"
    job = run(serve, {"folder": "someone", "posts": [post]})
    assert job.state == "done"
    assert len(files(serve, "someone")) == 2
    assert serve.known_posts(known)["known"] == ["abc0001"]


def test_failed_post_is_retried_without_archive_requests(
    serve: DownloadServer, archive: MockArcticShift, tmp_path: Path
):
    clock = [NOW]
    serve.retries = RetryList(serve, tmp_path / "failed_posts.json", lambda: clock[0])
    post = archive.add_post("abc0001", NOW)
    media = archive.media.pop("abc0001.jpg")
    job = run(serve, {"folder": "someone", "posts": [post]})  # the media is missing: 404
    assert job.results == {"abc0001": "failed"}
    assert serve.status()["retries"] == {"waiting": 1, "given_up": 0}

    archive.media["abc0001.jpg"] = media
    archive.requests.clear()
    clock[0] += HOUR
    serve.retries.tick()
    retry = wait(next(j for j in serve.jobs.values() if j.retry))
    assert retry.results == {"abc0001": "new"}
    assert len(files(serve, "someone")) == 1
    assert archive.requests == []
    assert serve.status()["retries"] == {"waiting": 0, "given_up": 0}
