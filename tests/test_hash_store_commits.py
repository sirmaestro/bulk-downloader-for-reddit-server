#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bdfr.configuration import Configuration
from bdfr.downloader import Downloader


def make_downloader(tmp_path: Path) -> Downloader:
    """A downloader with only its SQLite hash store set up"""
    downloader = Downloader.__new__(Downloader)
    downloader.args = Configuration()
    downloader.args.keep_hashes_db = True
    downloader._hash_db = None
    downloader._create_hash_db(tmp_path / "hashes.sqlite")
    return downloader


def stored_urls(tmp_path: Path) -> set[str]:
    """What another connection (e.g. serve's /known) sees"""
    connection = sqlite3.connect(tmp_path / "hashes.sqlite")
    try:
        return {row[0] for row in connection.execute("SELECT url FROM hashes_url")}
    finally:
        connection.close()


@pytest.mark.parametrize("raises", (False, True))
def test_rows_are_committed_once_per_submission(tmp_path: Path, raises: bool):
    downloader = make_downloader(tmp_path)
    seen_during = []

    def download_submission(submission):
        downloader._check_hash_exists_or_add(tmp_path / "abc123_title.jpg", "hash1")
        downloader._check_url_exists_or_add("https://i.redd.it/a.jpg", "hash1")
        seen_during.append(stored_urls(tmp_path))
        if raises:
            raise RuntimeError("boom")
        return False

    downloader._download_submission = download_submission
    downloader._run_one(MagicMock(id="abc123"))
    # Nothing was committed row by row; all of it was committed when the submission ended, even after an error
    assert seen_during == [set()]
    assert stored_urls(tmp_path) == {"https://i.redd.it/a.jpg"}
    downloader.close_hash_db()


def test_closing_commits(tmp_path: Path):
    downloader = make_downloader(tmp_path)
    downloader._check_url_exists_or_add("https://i.redd.it/a.jpg", "hash1")
    assert stored_urls(tmp_path) == set()
    downloader.close_hash_db()
    assert stored_urls(tmp_path) == {"https://i.redd.it/a.jpg"}
