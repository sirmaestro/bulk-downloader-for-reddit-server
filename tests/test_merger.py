#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from bdfr.__main__ import cli
from bdfr.exceptions import BulkDownloaderException
from bdfr.merger import merge_folders
from bdfr.renamer import HASH_DB_SCHEMA


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def make_db(folder: Path, files: dict[str, bytes], urls: dict[str, str]):
    connection = sqlite3.connect(folder / "hashes.sqlite")
    with connection:
        connection.executescript(HASH_DB_SCHEMA)
        for name, data in files.items():
            path = os.fsencode(str(folder / name))
            connection.execute("INSERT INTO hashes VALUES (?, ?)", (md5(data), path))
            connection.execute("INSERT INTO hashes_file VALUES (?, ?)", (path, md5(data)))
        connection.executemany("INSERT INTO hashes_url VALUES (?, ?)", urls.items())
    connection.close()


def read_db(folder: Path) -> tuple[dict[str, str], dict[str, str]]:
    connection = sqlite3.connect(folder / "hashes.sqlite")
    files = {Path(os.fsdecode(p)).name: h for p, h in connection.execute("SELECT file_path, hash FROM hashes_file")}
    urls = dict(connection.execute("SELECT url, hash FROM hashes_url"))
    connection.close()
    return files, urls


@pytest.fixture()
def folders(tmp_path: Path) -> tuple[Path, Path]:
    """old_account: a new file, a copy of new_account's file under another name, and a name new_account already
    uses for something else"""
    old, new = tmp_path / "old_account", tmp_path / "new_account"
    old.mkdir()
    new.mkdir()
    old_files = {"aaa111_one.jpg": b"one", "bbb222_copy.jpg": b"shared", "ccc333_x.jpg": b"old x"}
    new_files = {"ddd444_shared.jpg": b"shared", "ccc333_x.jpg": b"new x"}
    for folder, files in ((old, old_files), (new, new_files)):
        for name, data in files.items():
            (folder / name).write_bytes(data)
    return old, new


def test_merge_moves_new_content_and_links(folders):
    old, new = folders
    make_db(old, {}, {"https://i.redd.it/one.jpg": md5(b"one"), "post:bbb222": md5(b"shared")})
    make_db(new, {"ddd444_shared.jpg": b"shared"}, {"https://i.redd.it/one.jpg": "kept"})
    result = merge_folders(old, new)
    assert result == {"moved": 1, "to_move": 1, "duplicates": 1, "conflicts": 1, "links": 1}
    assert sorted(p.name for p in new.glob("*.jpg")) == ["aaa111_one.jpg", "ccc333_x.jpg", "ddd444_shared.jpg"]
    assert (new / "ccc333_x.jpg").read_bytes() == b"new x"
    assert sorted(p.name for p in old.glob("*.jpg")) == ["bbb222_copy.jpg", "ccc333_x.jpg"]
    files, urls = read_db(new)
    assert files["aaa111_one.jpg"] == md5(b"one")
    # The new folder's own entries win; the old folder's others are added, post keys included
    assert urls == {"https://i.redd.it/one.jpg": "kept", "post:bbb222": md5(b"shared")}
    assert list(new.glob("hashes.sqlite.before-merge-*"))


def test_merge_into_a_json_store(folders):
    old, new = folders
    make_db(old, {}, {"https://i.redd.it/one.jpg": md5(b"one")})
    (new / "hash_url_list.json").write_text("{}", encoding="utf-8")
    merge_folders(old, new)
    file_list = json.loads((new / "hash_file_list.json").read_text(encoding="utf-8"))
    assert file_list == {str(new.resolve() / "aaa111_one.jpg"): md5(b"one")}
    url_list = json.loads((new / "hash_url_list.json").read_text(encoding="utf-8"))
    assert url_list == {"https://i.redd.it/one.jpg": md5(b"one")}
    assert not (new / "hashes.sqlite").exists()


def test_merge_without_stores_only_moves(folders):
    old, new = folders
    merge_folders(old, new)
    assert (new / "aaa111_one.jpg").exists()
    assert sorted(p.name for p in new.iterdir()) == ["aaa111_one.jpg", "ccc333_x.jpg", "ddd444_shared.jpg"]


def test_dry_run_changes_nothing(folders):
    old, new = folders
    before = sorted(p.name for p in old.iterdir()), sorted(p.name for p in new.iterdir())
    assert merge_folders(old, new, dry_run=True)["to_move"] == 1
    assert (sorted(p.name for p in old.iterdir()), sorted(p.name for p in new.iterdir())) == before


@pytest.mark.parametrize("inside", (True, False))
def test_folders_must_be_separate(tmp_path: Path, inside: bool):
    (tmp_path / "a" / "b").mkdir(parents=True)
    with pytest.raises(BulkDownloaderException, match="separate"):
        merge_folders(tmp_path / "a", tmp_path / "a" / "b" if inside else tmp_path / "a")


def test_merge_command(folders):
    old, new = folders
    result = CliRunner().invoke(cli, ["merge", str(old), str(new)])
    assert result.exit_code == 0, result.output
    assert (new / "aaa111_one.jpg").exists()
    assert "aliases: [old_account]" in result.output
