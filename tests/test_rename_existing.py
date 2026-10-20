#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import praw
import pytest

from bdfr.configuration import Configuration
from bdfr.downloader import RedditDownloader
from bdfr.file_name_formatter import FileNameFormatter
from bdfr.resource import Resource


@pytest.fixture()
def downloader_mock(tmp_path: Path) -> MagicMock:
    downloader = MagicMock()
    downloader.args.rename_existing = True
    old = tmp_path / "someone_A title_abc123.jpg"
    old.write_bytes(b"image")
    downloader._get_hashed_item.return_value = str(old)
    return downloader


def make_submission(post_id: str) -> MagicMock:
    submission = MagicMock()
    submission.id = post_id
    return submission


def test_renames_same_submission(downloader_mock: MagicMock, tmp_path: Path):
    destination = tmp_path / "2026-10-20_abc123_A title.jpg"
    assert RedditDownloader._rename_existing(downloader_mock, "hash", destination, make_submission("abc123"))
    assert destination.read_bytes() == b"image"
    assert not (tmp_path / "someone_A title_abc123.jpg").exists()
    downloader_mock._move_hashed_file.assert_called_once_with(
        tmp_path / "someone_A title_abc123.jpg", destination, "hash"
    )


@pytest.mark.parametrize(
    ("post_id", "flag", "destination_exists"),
    (
        ("other1", True, False),  # identical file from another submission keeps its name
        ("abc123", False, False),  # --rename-existing not given
        ("abc123", True, True),  # the new name is taken
    ),
)
def test_does_not_rename(
    downloader_mock: MagicMock, tmp_path: Path, post_id: str, flag: bool, destination_exists: bool
):
    downloader_mock.args.rename_existing = flag
    destination = tmp_path / "2026-10-20_abc123_A title.jpg"
    if destination_exists:
        destination.write_bytes(b"other")
    assert not RedditDownloader._rename_existing(downloader_mock, "hash", destination, make_submission(post_id))
    assert (tmp_path / "someone_A title_abc123.jpg").exists()
    downloader_mock._move_hashed_file.assert_not_called()


def make_store(mode: str, tmp_path: Path) -> RedditDownloader:
    """A downloader with only its hash store set up"""
    store = RedditDownloader.__new__(RedditDownloader)
    store.args = Configuration()
    store.args.keep_hashes = mode == "json"
    store.args.keep_hashes_db = mode == "db"
    store.args.save_hashes_interval = 0
    store.master_hash_list, store.master_file_list, store.master_url_list = {}, {}, {}
    store._RedditConnector__hash_db_conn = None
    if mode == "db":
        connection = sqlite3.connect(tmp_path / "hashes.sqlite")
        connection.executescript(
            "CREATE TABLE hashes (hash TEXT PRIMARY KEY, file_path TEXT NOT NULL);"
            "CREATE TABLE hashes_file (file_path TEXT PRIMARY KEY, hash TEXT NOT NULL);"
            "CREATE TABLE hashes_url (url TEXT PRIMARY KEY, hash TEXT NOT NULL);"
        )
        store._RedditConnector__hash_db_conn = connection
    return store


@pytest.mark.parametrize("mode", ("db", "json", "memory"))
def test_move_hashed_file(mode: str, tmp_path: Path):
    store = make_store(mode, tmp_path)
    old, new = tmp_path / "old_abc123.jpg", tmp_path / "2026_abc123_new.jpg"
    if mode == "db":
        connection = store._RedditConnector__hash_db_conn
        connection.execute("INSERT INTO hashes VALUES (?, ?)", ("h", os.fsencode(str(old))))
        connection.execute("INSERT INTO hashes_file VALUES (?, ?)", (os.fsencode(str(old)), "h"))
        connection.execute("INSERT INTO hashes_url VALUES (?, ?)", ("https://i.redd.it/x.jpg", "h"))
    else:
        store.master_hash_list["h"] = str(old)
        store.master_file_list[str(old)] = "h"
        store.master_url_list["https://i.redd.it/x.jpg"] = "h"
    assert store._get_url_hash("https://i.redd.it/x.jpg") == ("h" if mode != "memory" else None)

    store._move_hashed_file(old, new, "h")

    assert Path(store._get_hashed_item("h")) == new
    if mode == "db":
        files = store._RedditConnector__hash_db_conn.execute("SELECT file_path FROM hashes_file").fetchall()
        assert [os.fsdecode(row[0]) for row in files] == [str(new)]
    elif mode == "json":
        assert store.master_file_list == {str(new): "h"}


@pytest.mark.parametrize(
    ("title", "scheme", "index", "expected"),
    (
        ("Ends with dots...", "{DATE}_{POSTID}_{TITLE}", None, "2021-04-21_abc1234_Ends with dots.jpg"),
        ("Gallery ", "{DATE}_{POSTID}_{TITLE}", 2, "2021-04-21_abc1234_Gallery_2.jpg"),
        ("Ends with dots...", "{REDDITOR}_{TITLE}_{POSTID}", None, "person_Ends with dots..._abc1234.jpg"),
    ),
)
def test_title_at_end_of_name(
    title: str, scheme: str, index, expected: str, reddit_instance: praw.Reddit, tmp_path: Path
):
    data = {"id": "abc1234", "title": title, "author": "person", "subreddit": "test", "created_utc": 1618997400,
            "score": 1, "link_flair_text": None}
    submission = praw.models.Submission(reddit_instance, _data=data)
    submission._fetched = True
    formatter = FileNameFormatter(scheme, "", "%Y-%m-%d", "linux")
    path = formatter.format_path(Resource(submission, "https://i.redd.it/x.jpg", lambda: None), tmp_path, index)
    assert path.name == expected


@pytest.mark.parametrize("mode", ("db", "json"))
def test_url_of_existing_file_is_recorded(mode: str, tmp_path: Path):
    store = make_store(mode, tmp_path)
    path = tmp_path / "1abc234_A title.jpg"
    if mode == "db":
        connection = store._RedditConnector__hash_db_conn
        connection.execute("INSERT INTO hashes VALUES (?, ?)", ("h", os.fsencode(str(path))))
        connection.execute("INSERT INTO hashes_file VALUES (?, ?)", (os.fsencode(str(path)), "h"))
    else:
        store.master_hash_list["h"] = str(path)
        store.master_file_list[str(path)] = "h"

    store._record_url_of_existing_file("https://i.redd.it/a.jpg", path)
    store._record_url_of_existing_file("https://i.redd.it/b.jpg", tmp_path / "not_stored.jpg")

    assert store._get_url_hash("https://i.redd.it/a.jpg") == "h"
    assert store._get_url_hash("https://i.redd.it/b.jpg") is None


def test_url_not_recorded_without_store(tmp_path: Path):
    store = make_store("memory", tmp_path)
    store._record_url_of_existing_file("https://i.redd.it/a.jpg", tmp_path / "x.jpg")
    assert store.master_url_list == {}
