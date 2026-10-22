#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import hashlib
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bdfr.__main__ import make_console_logging_handler
from bdfr.configuration import Configuration
from bdfr.connector import Connector
from bdfr.downloader import Downloader
from bdfr.post import Post


def add_console_handler():
    logging.getLogger().addHandler(make_console_logging_handler(3))


@pytest.fixture()
def args() -> Configuration:
    args = Configuration()
    args.time_format = "ISO"
    return args


@pytest.fixture()
def downloader_mock(args: Configuration):
    downloader_mock = MagicMock()
    downloader_mock.args = args
    downloader_mock._sanitise_subreddit_name = Connector.sanitise_subreddit_name
    downloader_mock._split_args_input = Connector.split_args_input
    downloader_mock.master_hash_list = {}
    downloader_mock._post_link_known.return_value = False
    downloader_mock._post_id_known.return_value = False
    return downloader_mock


@pytest.mark.parametrize(
    ("test_ids", "test_excluded", "expected_len"),
    (
        (("aaaaaa",), (), 1),
        (("aaaaaa",), ("aaaaaa",), 0),
        ((), ("aaaaaa",), 0),
        (("aaaaaa", "bbbbbb"), ("aaaaaa",), 1),
        (("aaaaaa", "bbbbbb", "cccccc"), ("aaaaaa",), 2),
    ),
)
@patch("bdfr.site_downloaders.download_factory.DownloadFactory.pull_lever")
def test_excluded_ids(
    mock_function: MagicMock,
    test_ids: tuple[str],
    test_excluded: tuple[str],
    expected_len: int,
    downloader_mock: MagicMock,
):
    downloader_mock.excluded_submission_ids = test_excluded
    mock_function.return_value = MagicMock()
    mock_function.return_value.__name__ = "test"
    test_submissions = []
    for test_id in test_ids:
        m = MagicMock()
        m.id = test_id
        m.subreddit = "test"
        m.__class__ = Post
        test_submissions.append(m)
    downloader_mock.listings = [test_submissions]
    for submission in test_submissions:
        Downloader._download_submission(downloader_mock, submission)
    assert mock_function.call_count == expected_len


def test_search_existing_files(tmp_path: Path):
    (tmp_path / "abc123_one.jpg").write_bytes(b"one")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "def456_two.jpg").write_bytes(b"two")
    (tmp_path / "hashes.sqlite").write_bytes(b"not a download")
    hashes, _, _ = Downloader.scan_existing_files(tmp_path, False)
    # Every download, in subfolders too, by its MD5; the hash store itself isn't a download
    assert hashes == {
        hashlib.md5(b"one").hexdigest(): tmp_path / "abc123_one.jpg",
        hashlib.md5(b"two").hexdigest(): tmp_path / "sub" / "def456_two.jpg",
    }
