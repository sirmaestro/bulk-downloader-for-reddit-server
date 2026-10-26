#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""serve jobs run with the real downloader against a mock Arctic Shift (tests/mock_arctic_shift.py)"""

import importlib.resources
import os
import shutil
import time
from pathlib import Path

import pytest

from bdfr.__main__ import SERVE_DEFAULTS
from bdfr.configuration import Configuration
from bdfr.downloader import Downloader
from bdfr.resource import TEMP_PREFIX, TEMP_SUFFIX, Resource
from bdfr.retries import HOUR, RetryList
from bdfr.server import DownloadServer, Job
from bdfr.site_downloaders.base_downloader import BaseDownloader
from bdfr.site_downloaders.download_factory import DownloadFactory
from bdfr.site_downloaders.gallery import Gallery
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


def test_requests_are_recorded_with_their_purpose(serve: DownloadServer, archive: MockArcticShift, tmp_path: Path):
    from bdfr.request_log import RequestLog

    serve.requests = RequestLog(tmp_path / "requests.jsonl")
    for number in range(5):
        archive.add_post(f"abc000{number}", NOW - number * 100)
    job = run(serve, {"folder": "someone", "user": "someone", "archive": "all"})
    assert job.state == "done", job.error
    recorded = serve.request_summary()["recent"]
    assert len(recorded) == len(archive.endpoint_requests("posts/search"))
    assert {(e["purpose"], e["job"], e["user"], e["endpoint"]) for e in recorded} == {
        ("Download: whole archive", job.id, "someone", "posts/search")
    }


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


def test_removed_gallery_without_lookups_is_unavailable_until_it_has_its_image_list(
    serve: DownloadServer, archive: MockArcticShift, monkeypatch: pytest.MonkeyPatch
):
    """Without archive lookups (retries), a gallery Reddit dropped the image list of is unavailable (removed), and
    isn't tried again until it comes with its image list"""
    serve.base_args.archive_lookups = False
    archive.add_post("abc0001", NOW, url="https://www.reddit.com/gallery/abc0001")
    post = dict(archive.posts["abc0001"], removed_by_category="moderator")
    post.pop("gallery_data", None)
    job = run(serve, {"folder": "someone", "posts": [post]})
    assert job.state == "done"
    assert job.results == {"abc0001": "unavailable"}
    assert archive.requests == []
    assert serve.known_posts({"folder": "someone", "ids": ["abc0001"]})["unavailable"] == ["abc0001"]
    assert run(serve, {"folder": "someone", "posts": [post]}).results == {"abc0001": "unavailable"}
    # Other data (e.g. the archive's) has the image list
    monkeypatch.setattr(Gallery, "find_image", staticmethod(lambda media_id: f"{archive.base}/media/abc0001.jpg"))
    post["gallery_data"] = {"items": [{"media_id": "m1"}]}
    job = run(serve, {"folder": "someone", "posts": [post]})
    assert job.results == {"abc0001": "new"}
    assert serve.known_posts({"folder": "someone", "ids": ["abc0001"]})["unavailable"] == []


def test_listing_stops_after_a_run_of_unavailable_posts(serve: DownloadServer, archive: MockArcticShift):
    """A user who deleted their older posts: once their media has been gone for --stop-at-unavailable posts in a row,
    the rest of the listing isn't read"""
    serve.base_args.stop_at_unavailable = 3
    archive.add_post("new0001", NOW)
    for number in range(8):
        archive.add_post(f"old000{number}", NOW - 100 - number)
        del archive.media[f"old000{number}.jpg"]
    job = run(serve, {"folder": "someone", "user": "someone"})
    assert job.state == "done"
    assert job.results == {
        "new0001": "new",
        "old0000": "unavailable",
        "old0001": "unavailable",
        "old0002": "unavailable",
    }
    # Pages of 3: the third (old0005 and older) is never fetched
    assert len(archive.endpoint_requests("posts/search")) == 2


def test_whole_archive_stops_after_two_dead_pages(
    serve: DownloadServer, archive: MockArcticShift, monkeypatch: pytest.MonkeyPatch
):
    """A whole-archive read goes on through stretches of dead media, and stops once two pages in a row have under
    10% of their media left"""
    serve.base_args.stop_at_unavailable = 3  # whole-archive reads don't stop at a run of unavailable posts
    monkeypatch.setattr(Downloader, "DEAD_PAGE_POSTS", 3)
    archive.add_post("new0001", NOW)
    for number in range(12):
        archive.add_post(f"old{number:04}", NOW - 100 - number)
        del archive.media[f"old{number:04}.jpg"]
    job = run(serve, {"folder": "someone", "user": "someone", "archive": "all"})
    assert job.state == "done" and job.stop_reason == "dead"
    # Pages of 3: new0001 keeps the first alive (1 of 3); the next two are dead, so old0008 on aren't reached
    assert list(job.results) == ["new0001"] + [f"old{number:04}" for number in range(8)]
    # The mock's pages of 3 overlap by one post: 3 + 2 + 2 + 2 posts, and old0010 on are never fetched
    assert len(archive.endpoint_requests("posts/search")) == 4


def test_whole_archive_stops_at_the_storage_limit(serve: DownloadServer, archive: MockArcticShift):
    for number in range(4):
        archive.add_post(f"abc{number:04}", NOW - number * 100)
    job = run(serve, {"folder": "someone", "user": "someone", "archive": "all", "limit_bytes": 1})
    assert job.state == "done" and job.stop_reason == "limit"
    assert job.results == {"abc0000": "new"}
    assert job.bytes_written == (serve.root / "someone" / files(serve, "someone")[0]).stat().st_size
    assert job.oldest_post == NOW


@pytest.mark.parametrize("store", ("db", "json"))
def test_removed_galleries_looked_up_by_id(
    serve: DownloadServer, archive: MockArcticShift, monkeypatch: pytest.MonkeyPatch, store: str
):
    """Removed galleries recorded without their image list are downloaded when the archive has it; ones the archive
    has without it, or doesn't have, are recorded as gone and not looked up again"""
    serve.base_args.keep_hashes_db = store == "db"
    serve.base_args.keep_hashes = store == "json"
    serve.base_args.archive_lookups = False
    folder = serve.root / "someone"
    posts = []
    for post_id in ("has0001", "lacks01", "missing"):
        archive.add_post(post_id, NOW, url=f"https://www.reddit.com/gallery/{post_id}")
        posts.append(dict(archive.posts[post_id], removed_by_category="reddit"))
    job = run(serve, {"folder": "someone", "posts": posts})  # as the userscript sends them, archive lookups off
    assert set(job.results.values()) == {"unavailable"}
    assert DownloadServer.removed_galleries(folder) == ["has0001", "lacks01", "missing"]

    archive.posts["has0001"]["gallery_data"] = {"items": [{"media_id": "m1"}]}
    del archive.posts["missing"]
    monkeypatch.setattr(Gallery, "find_image", staticmethod(lambda media_id: f"{archive.base}/media/has0001.jpg"))
    serve.base_args.archive_lookups = True
    ids = [{"id": post_id} for post_id in DownloadServer.removed_galleries(folder)]
    job = run(serve, {"folder": "someone", "posts": ids})
    assert job.state == "done"
    assert job.results == {"has0001": "new", "lacks01": "unavailable"}
    assert len(archive.endpoint_requests("posts/ids")) == 1  # all three in one request
    assert DownloadServer.removed_galleries(folder) == []
    known = serve.known_posts({"folder": "someone", "ids": ["has0001", "lacks01", "missing"]})
    assert known["known"] == ["has0001"] and known["unavailable"] == ["lacks01", "missing"]


@pytest.mark.parametrize("store", ("db", "json"))
def test_gallery_whose_files_were_all_downloaded_by_another_post_is_remembered(
    serve: DownloadServer, archive: MockArcticShift, monkeypatch: pytest.MonkeyPatch, store: str
):
    """A gallery posted again (or by an old account) has nothing with its ID in the folder; once its files' links are
    all found downloaded before, /known and later runs know it without asking the host"""
    serve.base_args.keep_hashes_db = store == "db"
    serve.base_args.keep_hashes = store == "json"
    urls = [f"{archive.base}/media/abc0001.jpg", f"{archive.base}/media/abc0001_b.jpg"]
    archive.media["abc0001_b.jpg"] = b"second image"

    class TwoFiles(BaseDownloader):
        def find_resources(self) -> list[Resource]:
            return [Resource(self.post, url, Resource.retry_download(url)) for url in urls]

    monkeypatch.setattr(DownloadFactory, "pull_lever", staticmethod(lambda url: TwoFiles))
    first, again = archive.add_post("abc0001", NOW), archive.add_post("def0002", NOW + 10)
    run(serve, {"folder": "someone", "posts": [first]})
    job = run(serve, {"folder": "someone", "posts": [again]})
    assert job.results == {"def0002": "existing"}
    known = serve.known_posts({"folder": "someone", "ids": ["def0002"]})
    assert known["known"] == [] and known["duplicates"] == ["def0002"]
    archive.media.clear()  # known before any request
    assert run(serve, {"folder": "someone", "posts": [again]}).results == {"def0002": "existing"}


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

    serve.retries = RetryList(serve, serve.root / "failed_posts.json")
    job = run(serve, {"folder": "someone", "posts": [post]})  # the mock has no second file yet: 404
    assert job.results == {"abc0001": "failed"}
    assert len(files(serve, "someone")) == 1
    assert serve.known_posts(known)["known"] == []
    assert serve.known_posts(known)["unavailable"] == []
    # The same 404 again can't change anything, so it isn't retried; Download (the userscript) still resends it
    assert serve.status()["retries"] == {"waiting": 0, "given_up": 0}

    archive.media["abc0001_b.jpg"] = b"second image"
    job = run(serve, {"folder": "someone", "posts": [post]})
    assert job.state == "done"
    assert len(files(serve, "someone")) == 2
    assert serve.known_posts(known)["known"] == ["abc0001"]


@pytest.mark.parametrize("store", ("db", "json"))
def test_dead_link_is_unavailable_and_not_tried_again(
    serve: DownloadServer, archive: MockArcticShift, tmp_path: Path, store: str
):
    """A post whose media the host answers 404 for is gone for good: it isn't retried, and later jobs and /known skip it
    without asking the host again"""
    serve.base_args.keep_hashes_db = store == "db"
    serve.base_args.keep_hashes = store == "json"
    serve.retries = RetryList(serve, tmp_path / "failed_posts.json")
    post = archive.add_post("abc0001", NOW)
    del archive.media["abc0001.jpg"]
    job = run(serve, {"folder": "someone", "posts": [post]})
    assert job.state == "done"
    assert job.results == {"abc0001": "unavailable"} and job.counts["unavailable"] == 1
    assert serve.status()["retries"] == {"waiting": 0, "given_up": 0}
    known = serve.known_posts({"folder": "someone", "ids": ["abc0001"]})
    assert known["unavailable"] == ["abc0001"] and known["known"] == []

    # Back on the host (e.g. a 404 by mistake): still skipped, without a request
    archive.media["abc0001.jpg"] = b"image"
    job = run(serve, {"folder": "someone", "posts": [post]})
    assert job.results == {"abc0001": "unavailable"}
    assert files(serve, "someone") == []


def test_link_without_downloader_is_unavailable(serve: DownloadServer, archive: MockArcticShift):
    post = archive.add_post("abc0001", NOW, url="https://onlyfans.com/someone")
    job = run(serve, {"folder": "someone", "posts": [post]})
    assert job.results == {"abc0001": "unavailable"}
    assert serve.known_posts({"folder": "someone", "ids": ["abc0001"]})["unavailable"] == ["abc0001"]


def test_failed_post_is_retried_without_archive_requests(
    serve: DownloadServer, archive: MockArcticShift, tmp_path: Path
):
    clock = [NOW]
    serve.retries = RetryList(serve, tmp_path / "failed_posts.json", lambda: clock[0])
    post = archive.add_post("abc0001", NOW)
    archive.fail("media", status=503)
    job = run(serve, {"folder": "someone", "posts": [post]})  # the media host is down: 503
    assert job.results == {"abc0001": "failed"}
    assert serve.status()["retries"] == {"waiting": 1, "given_up": 0}

    archive.requests.clear()
    clock[0] += HOUR
    serve.retries.tick()
    retry = wait(next(j for j in serve.jobs.values() if j.retry))
    assert retry.results == {"abc0001": "new"}
    assert len(files(serve, "someone")) == 1
    assert archive.requests == []
    assert serve.status()["retries"] == {"waiting": 0, "given_up": 0}


def test_downloads_leave_no_temporary_files(serve: DownloadServer, archive: MockArcticShift):
    """Downloads go through temporary files in the folder: kept ones are renamed into place, duplicates deleted, and
    ones a stopped run left behind are cleared once they are a day old"""
    folder = serve.root / "someone"
    folder.mkdir()
    stale, recent = folder / f"{TEMP_PREFIX}old{TEMP_SUFFIX}", folder / f"{TEMP_PREFIX}new{TEMP_SUFFIX}"
    stale.write_bytes(b"left over")
    recent.write_bytes(b"maybe another run's")
    os.utime(stale, (time.time() - 2 * 24 * 3600,) * 2)
    first, second = archive.add_post("abc0001", NOW), archive.add_post("abc0002", NOW - 60)
    archive.media["abc0002.jpg"] = archive.media["abc0001.jpg"]
    job = run(serve, {"folder": "someone", "posts": [first, second]})
    assert job.results == {"abc0001": "new", "abc0002": "duplicate"}
    assert files(serve, "someone") == [p.name for p in folder.glob("*abc0001*")]
    assert sorted(p.name for p in folder.glob(f"{TEMP_PREFIX}*")) == [recent.name]
