#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import datetime
import hashlib
import json
import os
import socket
import sqlite3
from pathlib import Path

import pytest

from bdfr.exceptions import BulkDownloaderException
from bdfr.renamer import Renamer, parse_name, rename_directory

OLD = "{REDDITOR}_{TITLE}_{POSTID}"
CREATED = 1792454400


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("rename tried to use the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)


def make_file(directory: Path, name: str, content: bytes = b"x") -> Path:
    path = directory / name
    path.write_bytes(content)
    os.utime(path, (CREATED, CREATED))
    return path


@pytest.mark.parametrize(
    ("name", "folder", "expected"),
    (
        ("someone_A title_1abc234.jpg", "someone", {"redditor": "someone", "title": "A title", "postid": "1abc234"}),
        (
            "some_user_Title_with_parts_1abc234_2.png",
            "some_user",
            {"redditor": "some_user", "title": "Title_with_parts", "postid": "1abc234", "index": "2"},
        ),
        ("someone_Pic 12_1abc234.jpg", "elsewhere", {"redditor": "someone", "title": "Pic 12", "postid": "1abc234"}),
        ("someone_Ends with dots..._1abc234.txt", "someone", {"title": "Ends with dots...", "ext": "txt"}),
        ("not a bdfr file.jpg", "someone", None),
    ),
)
def test_parse_name(name: str, folder: str, expected):
    result = parse_name(name, OLD, folder)
    if expected is None:
        assert result is None
    else:
        assert expected.items() <= result.items()


def test_rename_directory_updates_store(tmp_path: Path):
    folder = tmp_path / "someone"
    folder.mkdir()
    single = make_file(folder, "someone_A title_1abc234.jpg", b"a")
    gallery = [make_file(folder, f"someone_Gallery_5def678_{i}.jpg", bytes([i])) for i in (1, 2)]
    unrelated = make_file(folder, "notes.txt")
    connection = sqlite3.connect(folder / "hashes.sqlite")
    connection.executescript(
        "CREATE TABLE hashes (hash TEXT PRIMARY KEY, file_path TEXT NOT NULL);"
        "CREATE TABLE hashes_file (file_path TEXT PRIMARY KEY, hash TEXT NOT NULL);"
    )
    for i, path in enumerate([single, *gallery]):
        connection.execute("INSERT INTO hashes VALUES (?, ?)", (f"h{i}", os.fsencode(str(path))))
        connection.execute("INSERT INTO hashes_file VALUES (?, ?)", (os.fsencode(str(path)), f"h{i}"))
    connection.commit()
    connection.close()
    (folder / "hash_list.json").write_text(json.dumps({"h0": str(single)}))
    (folder / "hash_file_list.json").write_text(json.dumps({str(single): "h0"}))

    renamer = Renamer(OLD, "{POSTID}_{TITLE}", restriction_scheme="linux")
    assert rename_directory(folder, renamer, dry_run=True)["renamed"] == 0
    assert single.exists()

    result = rename_directory(folder, renamer, dry_run=False)

    assert result == {"renamed": 3, "planned": 3, "skipped": 1, "hashed": 0}
    expected = ["1abc234_A title.jpg", "5def678_Gallery_1.jpg", "5def678_Gallery_2.jpg"]
    for name in expected:
        assert (folder / name).exists()
        assert (folder / name).stat().st_mtime == CREATED
    assert unrelated.exists() and not single.exists()
    connection = sqlite3.connect(folder / "hashes.sqlite")
    stored = sorted(Path(os.fsdecode(row[0])).name for row in connection.execute("SELECT file_path FROM hashes"))
    stored_files = sorted(
        Path(os.fsdecode(row[0])).name for row in connection.execute("SELECT file_path FROM hashes_file")
    )
    connection.close()
    assert stored == stored_files == expected
    assert Path(json.loads((folder / "hash_list.json").read_text())["h0"]).name == "1abc234_A title.jpg"
    assert list(json.loads((folder / "hash_file_list.json").read_text())) == [str(folder / "1abc234_A title.jpg")]
    backups = sorted(p.name.split(".before-rename-")[0] for p in folder.glob("*.before-rename-*"))
    assert backups == ["hash_file_list.json", "hash_list.json", "hashes.sqlite"]
    undo = json.loads(next(folder.glob("bdfr-rename-*.json")).read_text())
    assert [Path(old).name for _new, old in undo] == [single.name, gallery[0].name, gallery[1].name]

    # Running again finds nothing to do
    assert rename_directory(folder, renamer, dry_run=False)["planned"] == 0


def test_date_comes_from_modified_time(tmp_path: Path):
    folder = tmp_path / "someone"
    folder.mkdir()
    make_file(folder, "someone_A title_1abc234.jpg")
    renamer = Renamer(OLD, "{DATE}_{POSTID}", time_format="%Y-%m-%d", restriction_scheme="linux")
    rename_directory(folder, renamer, dry_run=False)
    date = datetime.datetime.fromtimestamp(CREATED).strftime("%Y-%m-%d")
    assert (folder / f"{date}_1abc234.jpg").exists()


def test_skips_name_collisions(tmp_path: Path):
    folder = tmp_path / "someone"
    folder.mkdir()
    make_file(folder, "someone_A title_1abc234.jpg")
    make_file(folder, "1abc234_A title.jpg", b"different")
    result = rename_directory(folder, Renamer(OLD, "{POSTID}_{TITLE}", restriction_scheme="linux"), dry_run=False)
    # Skipped: the old file (its new name is taken) and the other file (not in the old scheme)
    assert result["renamed"] == 0 and result["skipped"] == 2
    assert (folder / "someone_A title_1abc234.jpg").exists()


def test_new_scheme_must_use_known_fields():
    with pytest.raises(BulkDownloaderException):
        Renamer(OLD, "{SUBREDDIT}_{POSTID}")
    with pytest.raises(BulkDownloaderException):
        Renamer("{TITLE}", "{POSTID}_{TITLE}")


def stored_rows(folder: Path) -> dict[str, str]:
    connection = sqlite3.connect(folder / "hashes.sqlite")
    try:
        rows = connection.execute("SELECT file_path, hash FROM hashes_file")
        return {Path(os.fsdecode(path)).name: file_hash for path, file_hash in rows}
    finally:
        connection.close()


def test_build_hash_db_after_rename(tmp_path: Path):
    folder = tmp_path / "someone"
    folder.mkdir()
    make_file(folder, "someone_A title_1abc234.jpg", b"a")
    make_file(folder, "someone_Gallery_5def678_1.jpg", b"b")
    renamer = Renamer(OLD, "{POSTID}_{TITLE}", restriction_scheme="linux")

    assert rename_directory(folder, renamer, dry_run=True, build_db=True)["hashed"] == 2
    assert not (folder / "hashes.sqlite").exists()

    result = rename_directory(folder, renamer, dry_run=False, build_db=True)

    assert result["renamed"] == 2 and result["hashed"] == 2
    assert stored_rows(folder) == {
        "1abc234_A title.jpg": hashlib.md5(b"a").hexdigest(),
        "5def678_Gallery_1.jpg": hashlib.md5(b"b").hexdigest(),
    }
    # The new DB has the same tables as one made by --keep-hashes-db, including the empty URL table
    connection = sqlite3.connect(folder / "hashes.sqlite")
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    connection.close()
    assert tables == {"hashes", "hashes_file", "hashes_url"}

    # Only files missing from the DB are added on later runs
    make_file(folder, "9ghi012_Later.jpg", b"c")
    assert rename_directory(folder, renamer, dry_run=False, build_db=True)["hashed"] == 1
    assert len(stored_rows(folder)) == 3


def test_build_hash_db_leaves_json_folders_alone(tmp_path: Path):
    folder = tmp_path / "someone"
    folder.mkdir()
    make_file(folder, "1abc234_A title.jpg")
    (folder / "hash_list.json").write_text("{}")
    renamer = Renamer(OLD, "{POSTID}_{TITLE}", restriction_scheme="linux")
    assert rename_directory(folder, renamer, dry_run=False, build_db=True)["hashed"] == 0
    assert not (folder / "hashes.sqlite").exists()


def test_no_subfolders_and_folder_is_user(tmp_path: Path):
    folder = tmp_path / "some_user"
    (folder / "extra").mkdir(parents=True)
    make_file(folder, "some_user_Mine_1abc234.jpg")
    make_file(folder, "other_name_Theirs_5def678.jpg")
    make_file(folder / "extra", "some_user_Nested_9ghi012.jpg")

    renamer = Renamer(OLD, "{POSTID}_{TITLE}", restriction_scheme="linux", folder_is_user=True)
    result = rename_directory(folder, renamer, dry_run=False, recursive=False)

    assert result["renamed"] == 1 and result["skipped"] == 1
    assert (folder / "1abc234_Mine.jpg").exists()
    # Not from this user's folder name: the user name "other_name" could be split anywhere, so it is left alone
    assert (folder / "other_name_Theirs_5def678.jpg").exists()
    assert (folder / "extra" / "some_user_Nested_9ghi012.jpg").exists()
