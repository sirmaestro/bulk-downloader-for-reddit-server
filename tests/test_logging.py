#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import contextlib
import io
import logging
import time
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bdfr import logs
from bdfr.arctic_shift import ArcticShift
from bdfr.configuration import Configuration
from bdfr.download_filter import DownloadFilter
from bdfr.downloader import Downloader
from bdfr.exceptions import SiteDownloaderError
from bdfr.file_name_formatter import FileNameFormatter
from bdfr.resource import Resource

CONTENT = {"https://i.redd.it/a.jpg": b"a", "https://i.redd.it/copy.jpg": b"a", "https://i.redd.it/c.jpg": b"c"}


def post(post_id: str, url: str, **extra) -> dict:
    data = {
        "id": post_id,
        "title": f"title {post_id}",
        "author": "someone",
        "subreddit": "test",
        "created_utc": 1792454400,
        "url": url,
    }
    return {**data, **extra}


@pytest.mark.parametrize(
    ("data", "expected"),
    (
        (post("abc1234", "https://i.redd.it/a.jpg"), None),
        (post("abc1234", "https://www.reddit.com/gallery/abc1234"), "gallery"),
        (post("abc1234", "https://www.reddit.com/gallery/abc1234", gallery_data={"items": []}), None),
        (post("abc1234", "https://v.redd.it/xyz"), None),
        (
            post("abc1234", "https://www.reddit.com/gallery/abc1234", bdfr_note="Reddit asked to log in"),
            "gallery (Reddit asked to log in)",
        ),
        (post("abc1234", "https://www.reddit.com/r/test/comments/abc1234/title/"), "self post"),
        (post("abc1234", "https://www.reddit.com/r/test/comments/zzz9999/other/"), "crosspost or link to another"),
        ({"id": "abc1234"}, "no URL"),
        (post("abc1234", "https://i.redd.it/a.jpg", author=None), "post data lacks author"),
    ),
)
def test_needs_lookup_says_why(data: dict, expected):
    reason = ArcticShift.needs_lookup(data)
    if expected is None:
        assert reason is None
    else:
        assert reason.startswith(expected)


def test_lookups_are_logged_by_post(caplog: pytest.LogCaptureFixture):
    client = ArcticShift()
    client.MIN_REQUEST_INTERVAL = 0
    client.session = MagicMock()
    gallery = post("g111111", "https://www.reddit.com/gallery/g111111")
    response = MagicMock(status_code=200, headers={})
    response.json.return_value = {"data": [dict(gallery, gallery_data={"items": []})]}
    client.session.get.return_value = response
    self_post = post("s111111", "https://www.reddit.com/r/test/comments/s111111/title/")
    posts = [post("d111111", "https://i.redd.it/a.jpg"), gallery, self_post, post("v111111", "https://v.redd.it/xyz")]

    with caplog.at_level(logging.INFO):
        assert len(list(client.submissions_from_posts(posts))) == 4

    assert "Looking up 2 of 4 posts in Arctic Shift:" in caplog.text
    assert "gallery (1): g111111" in caplog.text
    assert "self post (1): s111111" in caplog.text
    assert "Arctic Shift doesn't have 1 of them: s111111" in caplog.text
    assert "posts/ids (2 IDs) -> 1 posts" in caplog.text
    assert client.summary() == "Made 1 request to Arctic Shift (2 posts looked up: 1 gallery, 1 self post)"


class FakeDownloader:
    def __init__(self, submission, args):
        self.submission = submission

    def find_resources(self) -> list[Resource]:
        url = self.submission.url
        return [Resource(self.submission, url, lambda _params: CONTENT.get(url, url.encode()), ".jpg")]


@contextlib.contextmanager
def context_lines(level: int = logging.INFO):
    """The downloader's lines at `level` and up as the log files show them after the level: context, then message"""
    stream = io.StringIO()
    handler = logs.prepare(logging.StreamHandler(stream), level)
    handler.setFormatter(logging.Formatter("%(context)s%(message)s"))
    downloader_logger = logging.getLogger("bdfr.downloader")
    saved = downloader_logger.level
    downloader_logger.setLevel(level)
    downloader_logger.addHandler(handler)
    lines = []
    try:
        yield lines
    finally:
        downloader_logger.removeHandler(handler)
        downloader_logger.setLevel(saved)
        lines.extend(stream.getvalue().splitlines())


def make_downloader(tmp_path: Path, posts: list[dict]) -> Downloader:
    """A downloader that keeps hashes in memory and downloads with FakeDownloader"""
    downloader = Downloader.__new__(Downloader)
    downloader.args = Configuration()
    downloader.args.no_dupes = True
    downloader.args.min_score = 5
    downloader.download_directory = tmp_path
    downloader.excluded_submission_ids = set()
    downloader.download_filter = DownloadFilter([], [])
    downloader.file_name_formatter = FileNameFormatter("{POSTID}_{TITLE}", "", "ISO", "linux")
    downloader.master_hash_list, downloader.master_file_list, downloader.master_url_list = {}, {}, {}
    downloader._hash_db = None
    downloader.progress_callback = None
    downloader.newest_post = None
    downloader.errors, downloader.failed_posts = [], {}
    client = ArcticShift()
    downloader.arctic_shift = client
    downloader.listings = [iter([client.to_submission(data) for data in posts])]
    return downloader


def test_one_line_per_post(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    posts = [
        post("aaa1111", "https://i.redd.it/a.jpg", score=10),
        post("bbb2222", "https://i.redd.it/copy.jpg", score=10),
        post("ccc3333", "https://i.redd.it/c.jpg", score=1),
    ]
    with patch("bdfr.downloader.DownloadFactory.pull_lever", return_value=FakeDownloader):
        with context_lines() as lines:
            make_downloader(tmp_path, posts).download()
        assert lines == [
            "post aaa1111: new - 1 file written (#1, r/test, FakeDownloader)",
            "post bbb2222: duplicate - 1 same as already downloaded aaa1111_title aaa1111.jpg "
            "(#2, r/test, FakeDownloader)",
            "post ccc3333: skipped - score 1 is below --min-score 5 (#3, r/test)",
            "Finished 3 posts: 1 new, 0 already downloaded, 1 duplicate, 1 skipped, 0 failed; 1 file written",
            "Made no requests to Arctic Shift",
        ]

        with context_lines() as lines:
            make_downloader(tmp_path, posts[:1]).download()
    # Known from its ID in the folder's file names, before anything is requested
    assert "post aaa1111: already downloaded - files with its ID already in the folder (#1, r/test)" in lines


class FailingDownloader(FakeDownloader):
    def find_resources(self, authenticator=None):
        raise SiteDownloaderError("HTTP 404")


def test_failed_posts_are_warnings(tmp_path: Path):
    with patch("bdfr.downloader.DownloadFactory.pull_lever", return_value=FailingDownloader):
        with context_lines(logging.WARNING) as lines:
            make_downloader(tmp_path, [post("aaa1111", "https://i.redd.it/a.jpg")]).download()
    assert lines == ["post aaa1111: failed - HTTP 404 (#1, r/test, FailingDownloader)"]


@pytest.mark.parametrize("parallel", (1, 4))
def test_debug_lines_name_their_post(parallel: int, tmp_path: Path):
    """With --parallel, posts' lines interleave; each says which post it's about"""
    posts = [post(f"{n}aa1111", f"https://i.redd.it/{n}.jpg", score=10) for n in range(6)]
    with patch("bdfr.downloader.DownloadFactory.pull_lever", return_value=FakeDownloader):
        with context_lines(logging.DEBUG) as lines:
            downloader = make_downloader(tmp_path, posts)
            downloader.args.parallel = parallel
            with logs.job_context("job1"):
                downloader.download()
    wrote = sorted(line for line in lines if ": Wrote " in line)
    assert len(wrote) == 6
    for n, line in enumerate(wrote):
        assert line.startswith(f"job job1, post {n}aa1111: Wrote ") and line.endswith(f"{n}aa1111.jpg")


def test_given_posts_ignore_stop_at_existing(tmp_path: Path):
    posts = [post(f"p{i}00000", "https://i.redd.it/a.jpg", score=10) for i in range(4)]
    downloader = make_downloader(tmp_path, posts)
    downloader.args.stop_at_existing = 1
    downloader.post_list = downloader.listings[0]
    listing = [downloader.arctic_shift.to_submission(data) for data in posts]
    downloader.listings.append(iter(listing))
    seen = []
    downloader.progress_callback = lambda post_id, result, _label: seen.append((post_id, result))
    with patch("bdfr.downloader.DownloadFactory.pull_lever", return_value=FakeDownloader):
        downloader.download()
    # All four given posts are checked (the last three are duplicates of the first); the listing's copies are skipped
    assert [post_id for post_id, _ in seen] == ["p000000", "p100000", "p200000", "p300000"]


class SlowDownloader(FakeDownloader):
    """Downloads that finish in reverse order, to show results are still reported in listing order"""

    def find_resources(self) -> list[Resource]:
        url = self.submission.url
        delay = {"a": 0.3, "copy": 0.2}.get(url.rsplit("/", 1)[1].split(".")[0], 0.1)

        def download(_params):
            time.sleep(delay)
            return CONTENT[url]

        return [Resource(self.submission, url, download, ".jpg")]


@pytest.mark.parametrize("parallel", (1, 4))
def test_parallel_downloads_report_in_order(parallel: int, tmp_path: Path):
    posts = [
        post("aaa1111", "https://i.redd.it/a.jpg", score=10),
        post("bbb2222", "https://i.redd.it/copy.jpg", score=10),  # same content as aaa1111
        post("ccc3333", "https://i.redd.it/c.jpg", score=10),
    ]
    downloader = make_downloader(tmp_path, posts)
    downloader.args.parallel = parallel
    seen = []
    downloader.progress_callback = lambda post_id, result, _label: seen.append((post_id, result))
    with patch("bdfr.downloader.DownloadFactory.pull_lever", return_value=SlowDownloader):
        downloader.download()
    assert [post_id for post_id, _ in seen] == ["aaa1111", "bbb2222", "ccc3333"]
    results = dict(seen)
    # The two posts with the same content are downloaded at once, but only one of them is written
    assert sorted((results["aaa1111"], results["bbb2222"]), key=str) == [False, None]
    labels = {
        post_id: Downloader.result_label(result, {"files": Counter(duplicate=result is None), "skip_reason": None})
        for post_id, result in seen
    }
    assert sorted(labels.values()) == ["duplicate", "new", "new"]
    assert results["ccc3333"] is False
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        [
            f"{'aaa1111' if results['aaa1111'] is False else 'bbb2222'}_title "
            f"{'aaa1111' if results['aaa1111'] is False else 'bbb2222'}.jpg",
            "ccc3333_title ccc3333.jpg",
        ]
    )


def test_parallel_stop_at_existing(tmp_path: Path):
    posts = [post(f"p{i}00000", f"https://i.redd.it/{i}.jpg", score=10) for i in range(10)]
    for data in posts:
        CONTENT[data["url"]] = data["id"].encode()
        (tmp_path / f"{data['id']}_title {data['id']}.jpg").write_bytes(b"x")
    downloader = make_downloader(tmp_path, posts)
    downloader.args.parallel = 2
    downloader.args.stop_at_existing = 3
    seen = []
    downloader.progress_callback = lambda post_id, result, _label: seen.append(post_id)
    with patch("bdfr.downloader.DownloadFactory.pull_lever", return_value=FakeDownloader):
        downloader.download()
    # Stops after the third existing post; posts already started are reported, the rest are never touched
    assert seen[:3] == ["p000000", "p100000", "p200000"]
    assert len(seen) <= 3 + 2 * 2


@pytest.mark.parametrize("parallel", (1, 3))
def test_shared_link_downloaded_once(parallel: int, tmp_path: Path):
    """Posts linking the same media (reposts, the same post twice on a page) download it once, in parallel and
    without a hash store"""
    calls = []

    class CountingDownloader(FakeDownloader):
        def find_resources(self) -> list[Resource]:
            url = self.submission.url

            def download(_params):
                calls.append(url)
                time.sleep(0.2)
                return CONTENT[url]

            return [Resource(self.submission, url, download, ".jpg")]

    posts = [post(f"s{i}11111", "https://i.redd.it/a.jpg", score=10) for i in range(3)]
    downloader = make_downloader(tmp_path, posts)
    downloader.args.parallel = parallel
    seen = []
    downloader.progress_callback = lambda post_id, result, _label: seen.append((post_id, result))
    with patch("bdfr.downloader.DownloadFactory.pull_lever", return_value=CountingDownloader):
        downloader.download()
    assert calls == ["https://i.redd.it/a.jpg"]
    # Reported in listing order; with threads, any of them may be the one that downloads it
    assert [post_id for post_id, _ in seen] == ["s011111", "s111111", "s211111"]
    assert sorted(result for _, result in seen) == [False, True, True]


def test_same_post_link_needs_no_request(tmp_path: Path):
    """Posts sharing a Redgifs link, written any of its ways, ask the host once per run, and not at all once the link
    is in the hash store"""
    lookups = []
    media = "https://media.redgifs.com/SomeVideo.mp4"
    CONTENT[media] = b"video"

    class RedgifsLike(FakeDownloader):
        def find_resources(self) -> list[Resource]:
            lookups.append(self.submission.id)
            return [Resource(self.submission, media, lambda _params: CONTENT[media], ".mp4")]

    links = [
        "https://redgifs.com/watch/somevideo",
        "https://www.redgifs.com/watch/SomeVideo",
        "https://i.redgifs.com/i/somevideo",
    ]
    posts = [post(f"r{i}11111", link, score=10) for i, link in enumerate(links)]
    downloader = make_downloader(tmp_path, posts)
    downloader.args.keep_hashes = True
    seen = []
    downloader.progress_callback = lambda post_id, result, _label: seen.append(result)
    with patch("bdfr.downloader.DownloadFactory.pull_lever", return_value=RedgifsLike):
        downloader.download()
        assert lookups == ["r011111"]
        assert seen == [False, True, True]
        assert (
            downloader.master_url_list["https://www.redgifs.com/watch/somevideo"] == downloader.master_url_list[media]
        )

        # A later run with the same store: a new post with the link is recognised without any request
        later = make_downloader(tmp_path, [post("r911111", links[0], score=10)])
        later.args.keep_hashes = True
        later.master_url_list = dict(downloader.master_url_list)
        later.master_hash_list = dict(downloader.master_hash_list)
        later.master_file_list = dict(downloader.master_file_list)
        later.download()
    assert lookups == ["r011111"]


def test_galleries_are_not_remembered_by_post_link(tmp_path: Path):
    class TwoFiles(FakeDownloader):
        def find_resources(self) -> list[Resource]:
            return [
                Resource(self.submission, url, lambda _params, u=url: CONTENT[u], ".jpg")
                for url in ("https://i.redd.it/a.jpg", "https://i.redd.it/c.jpg")
            ]

    downloader = make_downloader(tmp_path, [post("g011111", "https://www.reddit.com/gallery/g011111", score=10)])
    downloader.args.keep_hashes = True
    with patch("bdfr.downloader.DownloadFactory.pull_lever", return_value=TwoFiles):
        downloader.download()
    assert "https://www.reddit.com/gallery/g011111" not in downloader.master_url_list
    assert "https://i.redd.it/a.jpg" in downloader.master_url_list


def test_known_posts_cost_no_requests(tmp_path: Path):
    """Posts with files in the folder, and posts that were all duplicates, are recognised before any host request"""
    lookups = []

    class Counting(FakeDownloader):
        def find_resources(self) -> list[Resource]:
            lookups.append(self.submission.id)
            return super().find_resources()

    posts = [
        post("ori1111", "https://i.redd.it/a.jpg", score=10),
        post("dup1111", "https://i.redd.it/copy.jpg", score=10),  # same content, another link
    ]
    first = make_downloader(tmp_path, posts)
    first.args.keep_hashes = True
    seen = []
    first.progress_callback = lambda post_id, result, label: seen.append(label)
    with patch("bdfr.downloader.DownloadFactory.pull_lever", return_value=Counting):
        first.download()
        assert seen == ["new", "duplicate"] and lookups == ["ori1111", "dup1111"]
        assert "post:dup1111" in first.master_url_list

        later = make_downloader(tmp_path, posts)
        later.args.keep_hashes = True
        later.master_url_list = dict(first.master_url_list)
        later.master_hash_list = dict(first.master_hash_list)
        later.master_file_list = dict(first.master_file_list)
        results = []
        later.progress_callback = lambda post_id, result, label: results.append(result)
        later.download()
    assert lookups == ["ori1111", "dup1111"]  # nothing new asked of the host
    assert results == [True, True]


def test_media_only(tmp_path: Path):
    posts = [
        post("sel1111", "https://www.reddit.com/r/test/comments/sel1111/x/", score=10, is_self=True),
        post("xpo1111", "https://i.redd.it/a.jpg", score=10, crosspost_parent="t3_orig111"),
        post("med1111", "https://i.redd.it/c.jpg", score=10),
    ]
    downloader = make_downloader(tmp_path, posts)
    downloader.args.media_only = True
    seen = []
    downloader.progress_callback = lambda post_id, result, label: seen.append((post_id, label))
    with patch("bdfr.downloader.DownloadFactory.pull_lever", return_value=FakeDownloader):
        downloader.download()
    assert seen == [("sel1111", "skipped"), ("xpo1111", "skipped"), ("med1111", "new")]
