#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from bdfr.configuration import Configuration
from bdfr.downloader import Downloader


def make_connector(directory: Path) -> Downloader:
    """A connector with only what the hash store conversion needs"""
    connector = Downloader.__new__(Downloader)
    connector.args = Configuration()
    connector.args.keep_hashes = True
    connector.args.keep_hashes_db = True
    connector.download_directory = directory
    connector._hash_db = None
    return connector


def write_hash_files(directory: Path, urls: bool = True):
    file_path = str(directory / "a_abc1234.jpg")
    (directory / "hash_list.json").write_text(json.dumps({"h1": file_path}), encoding="utf-8")
    (directory / "hash_file_list.json").write_text(json.dumps({file_path: "h1"}), encoding="utf-8")
    if urls:
        (directory / "hash_url_list.json").write_text(json.dumps({"https://i.redd.it/a.jpg": "h1"}), encoding="utf-8")


def convert(connector: Downloader):
    connector._check_hash_params()


def test_conversion_creates_complete_db(tmp_path: Path):
    write_hash_files(tmp_path)
    connector = make_connector(tmp_path)
    convert(connector)
    connector.close_hash_db()
    connection = sqlite3.connect(tmp_path / "hashes.sqlite")
    assert connection.execute("SELECT hash, file_path FROM hashes").fetchall() == [
        ("h1", os.fsencode(str(tmp_path / "a_abc1234.jpg")))
    ]
    assert connection.execute("SELECT url, hash FROM hashes_url").fetchall() == [("https://i.redd.it/a.jpg", "h1")]
    connection.close()
    assert connector.args.keep_hashes is False
    assert [p.name for p in tmp_path.iterdir() if "sqlite" in p.name] == ["hashes.sqlite"]


def test_failed_conversion_leaves_no_db_and_can_be_retried(tmp_path: Path):
    write_hash_files(tmp_path, urls=False)
    with pytest.raises(Exception, match="hash_url_list.json missing"):
        convert(make_connector(tmp_path))
    assert not (tmp_path / "hashes.sqlite").exists()

    write_hash_files(tmp_path)
    connector = make_connector(tmp_path)
    convert(connector)
    connector.close_hash_db()
    assert (tmp_path / "hashes.sqlite").exists()
    assert [p.name for p in tmp_path.iterdir() if "sqlite" in p.name] == ["hashes.sqlite"]


def test_unreadable_hash_file_leaves_no_db(tmp_path: Path):
    write_hash_files(tmp_path)
    (tmp_path / "hash_file_list.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        convert(make_connector(tmp_path))
    assert not (tmp_path / "hashes.sqlite").exists()


class FailingCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def executemany(self, query, data):
        if "hashes_url" in query:
            raise sqlite3.OperationalError("disk I/O error")
        return self._cursor.executemany(query, data)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class FailingConnection:
    def __init__(self, connection):
        self._connection = connection

    def cursor(self):
        return FailingCursor(self._connection.cursor())

    def __getattr__(self, name):
        return getattr(self._connection, name)


def test_database_error_during_conversion_is_not_swallowed(tmp_path: Path):
    """An error writing one table used to be logged and the conversion carried on, leaving a DB missing that table's
    rows that later runs trusted"""
    write_hash_files(tmp_path)
    real_connect = sqlite3.connect
    with patch("bdfr.hash_store.sqlite3.connect", lambda *a, **k: FailingConnection(real_connect(*a, **k))):
        with pytest.raises(Exception, match="disk I/O error"):
            convert(make_connector(tmp_path))
    assert not (tmp_path / "hashes.sqlite").exists()
