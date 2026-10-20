#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""`bdfr rename`: renames already downloaded files from one file scheme to another using only what is on disk.

Fields are read back from the old names, and {DATE} from the file's modified time, which BDFR sets to the post's
creation time. Nothing is requested from Reddit, Arctic Shift or any media host. The hash store (hashes.sqlite and
the hash_*.json files) is updated to the new names, and with --build-hash-db a missing hashes.sqlite is created
from the files' contents."""

import json
import logging
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Optional

import praw
import praw.models

from bdfr.connector import RedditConnector, _calc_hash
from bdfr.exceptions import BulkDownloaderException
from bdfr.file_name_formatter import FileNameFormatter
from bdfr.resource import Resource

logger = logging.getLogger(__name__)

# How each key's value looks inside an old file name
KEY_PATTERNS = {
    "POSTID": r"[a-z0-9]{3,13}",
    "REDDITOR": r"[\w-]+?",
    "SUBREDDIT": r"\w+?",
    "UPVOTES": r"-?\d+",
    "TITLE": r".*",
    "FLAIR": r".*?",
    "DATE": r".+?",
}
SUBMISSION_FIELDS = {"POSTID": "id", "REDDITOR": "author", "SUBREDDIT": "subreddit", "UPVOTES": "score",
                     "TITLE": "title", "FLAIR": "link_flair_text"}
UNDO_PREFIX = "bdfr-rename-"
# The same tables RedditConnector creates for --keep-hashes-db
HASH_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS hashes (hash TEXT PRIMARY KEY, file_path TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hashes_file (file_path TEXT PRIMARY KEY, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hashes_url (url TEXT PRIMARY KEY, hash TEXT NOT NULL);
"""


def scheme_regex(scheme: str, fixed: Optional[dict[str, str]] = None) -> re.Pattern:
    """A regex matching file names made with the scheme, with the gallery index and extension"""
    fixed = fixed or {}
    parts = re.split(r"\{(\w+)\}", scheme)
    pattern, seen = "", set()
    for i, part in enumerate(parts):
        if i % 2 == 0:
            pattern += re.escape(part)
        elif part in seen:
            pattern += f"(?P={part.lower()})"
        else:
            seen.add(part)
            value = re.escape(fixed[part]) if part in fixed else KEY_PATTERNS.get(part, ".+?")
            pattern += f"(?P<{part.lower()}>{value})"
    return re.compile(pattern + r"(?:_(?P<index>\d{1,2}))?\.(?P<ext>[^.]+)$")


def parse_name(name: str, scheme: str, folder_name: str, folder_is_user: bool = False) -> Optional[dict]:
    # A user's folder is usually named after them, which settles where a name containing "_" ends. Without that,
    # a user name containing "_" can't be told apart from the title, so folder_is_user skips such files.
    for fixed in ({"REDDITOR": folder_name},) if folder_is_user else ({"REDDITOR": folder_name}, {}):
        match = scheme_regex(scheme, fixed).match(name)
        if match:
            return {key: value for key, value in match.groupdict().items() if value is not None}
    return None


class Renamer:
    def __init__(
        self,
        old_scheme: str,
        new_scheme: str,
        time_format: str = "ISO",
        restriction_scheme: Optional[str] = None,
        character_set: Optional[str] = None,
        folder_is_user: bool = False,
    ):
        old_keys = {key for key in re.findall(r"\{(\w+)\}", old_scheme)}
        needed = {key for key in re.findall(r"\{(\w+)\}", new_scheme)} - {"DATE"}
        if "POSTID" not in old_keys:
            raise BulkDownloaderException("The old scheme must contain {POSTID}")
        if missing := needed - old_keys:
            missing_keys = ", ".join("{" + key + "}" for key in sorted(missing))
            raise BulkDownloaderException(f"The new scheme needs {missing_keys}, which the old names don't contain")
        self.old_scheme = old_scheme
        self.folder_is_user = folder_is_user
        self.formatter = FileNameFormatter(new_scheme, "", time_format, restriction_scheme, character_set)
        # Only used to build submission objects; never makes a request
        self.reddit = praw.Reddit(client_id="offline", client_secret="offline", user_agent="bdfr rename",
                                  check_for_updates=False)

    def plan(self, directory: Path, recursive: bool = True) -> tuple[list[tuple[Path, Path]], list[tuple[Path, str]]]:
        renames, skipped, targets = [], [], set()
        for path in media_files(directory, recursive):
            fields = parse_name(path.name, self.old_scheme, path.parent.name, self.folder_is_user)
            if not fields:
                skipped.append((path, "name doesn't match the old scheme"))
                continue
            new_path = self._new_path(path, fields)
            if new_path == path:
                continue
            if new_path.exists() or new_path in targets:
                skipped.append((path, f"{new_path.name} already exists"))
                continue
            targets.add(new_path)
            renames.append((path, new_path))
        return renames, skipped

    def _new_path(self, path: Path, fields: dict) -> Path:
        data = {"id": fields["postid"], "title": "", "author": "[deleted]", "subreddit": "unknown", "score": 0,
                "link_flair_text": None, "created_utc": path.stat().st_mtime}
        for key, attribute in SUBMISSION_FIELDS.items():
            if key.lower() in fields:
                data[attribute] = int(fields[key.lower()]) if key == "UPVOTES" else fields[key.lower()]
        submission = praw.models.Submission(self.reddit, _data=data)
        submission._fetched = True
        resource = Resource(submission, "", lambda: None, fields["ext"])
        index = int(fields["index"]) if fields.get("index") else None
        return self.formatter.format_path(resource, path.parent, index)


def media_files(directory: Path, recursive: bool = True) -> list[Path]:
    """Every file in directory (and its subfolders, if recursive) except BDFR's own hash stores, backups and undo
    lists"""
    files = []
    for dirpath, dirnames, filenames in os.walk(directory):
        files.extend(Path(dirpath, name) for name in sorted(filenames) if not RedditConnector._is_own_file(name))
        if not recursive:
            dirnames.clear()
    return files


def build_hash_db(directory: Path, dry_run: bool, recursive: bool = True) -> int:
    """Creates hashes.sqlite if missing and adds the content hash of every file not already in it. Media URLs can't
    be known offline; downloads fill them in as posts come up again."""
    db_path = directory / "hashes.sqlite"
    if not db_path.exists() and (directory / "hash_list.json").exists():
        logger.warning(
            "This folder keeps its hashes in hash_*.json files, so no hash DB was built. To switch to a hash DB, run a "
            "download into it once with both --keep-hashes and --keep-hashes-db, which converts the files"
        )
        return 0
    stored = set()
    if db_path.exists():
        connection = sqlite3.connect(db_path)
        try:
            stored = {os.fsdecode(row[0]) for row in connection.execute("SELECT file_path FROM hashes_file")}
        finally:
            connection.close()
    files = [path for path in media_files(directory, recursive) if str(path) not in stored]
    new_db = "a new " if not db_path.exists() else ""
    logger.info(f"{'Would add' if dry_run else 'Adding'} {len(files)} files to {new_db}{db_path.name}")
    if dry_run or not files:
        return len(files)
    rows = [(file_hash, os.fsencode(str(path))) for path, file_hash in map(_calc_hash, files)]
    connection = sqlite3.connect(db_path)
    try:
        with connection:
            connection.executescript(HASH_DB_SCHEMA)
            connection.executemany("INSERT OR IGNORE INTO hashes(hash, file_path) VALUES (?, ?)", rows)
            connection.executemany(
                "INSERT OR IGNORE INTO hashes_file(file_path, hash) VALUES (?, ?)", [(p, h) for h, p in rows]
            )
    finally:
        connection.close()
    return len(files)


def back_up_hash_store(directory: Path, stamp: str) -> list[Path]:
    backups = []
    for name in ("hashes.sqlite", "hash_list.json", "hash_file_list.json", "hash_url_list.json"):
        source = directory / name
        if source.exists():
            backup = directory / f"{name}.before-rename-{stamp}"
            shutil.copy2(source, backup)
            backups.append(backup)
    return backups


def update_hash_store(directory: Path, renames: list[tuple[Path, Path]]):
    if not renames:
        return
    db_path = directory / "hashes.sqlite"
    if db_path.exists():
        connection = sqlite3.connect(db_path)
        try:
            with connection:
                for old, new in renames:
                    old_key, new_key = os.fsencode(str(old)), os.fsencode(str(new))
                    connection.execute("UPDATE hashes SET file_path = ? WHERE file_path = ?", (new_key, old_key))
                    connection.execute("UPDATE hashes_file SET file_path = ? WHERE file_path = ?", (new_key, old_key))
        finally:
            connection.close()
    mapping = {str(old): str(new) for old, new in renames}
    hash_list_path, file_list_path = directory / "hash_list.json", directory / "hash_file_list.json"
    if hash_list_path.exists():
        hash_list = json.loads(hash_list_path.read_text(encoding="utf-8"))
        RedditConnector._write_json(str(hash_list_path), {h: mapping.get(p, p) for h, p in hash_list.items()})
    if file_list_path.exists():
        file_list = json.loads(file_list_path.read_text(encoding="utf-8"))
        RedditConnector._write_json(str(file_list_path), {mapping.get(p, p): h for p, h in file_list.items()})


def rename_directory(
    directory: Path, renamer: Renamer, dry_run: bool, build_db: bool = False, recursive: bool = True
) -> dict:
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        raise BulkDownloaderException(f"{directory} is not a folder")
    renames, skipped = renamer.plan(directory, recursive)
    for path, reason in skipped:
        logger.warning(f"Skipping {path.name}: {reason}")
    for old, new in renames:
        logger.info(f"{'Would rename' if dry_run else 'Renaming'} {old.name} -> {new.name}")
    result = {"renamed": 0, "planned": len(renames), "skipped": len(skipped), "hashed": 0}
    if not dry_run and renames:
        result["renamed"] = _apply_renames(directory, renames)
    if build_db:
        # After renaming, so the stored paths are the new names
        result["hashed"] = build_hash_db(directory, dry_run, recursive)
    return result


def _apply_renames(directory: Path, renames: list[tuple[Path, Path]]) -> int:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backups = back_up_hash_store(directory, stamp)
    undo_path = directory / f"{UNDO_PREFIX}{stamp}.json"
    done = []
    try:
        for old, new in renames:
            old.rename(new)
            done.append((old, new))
    finally:
        # Record what actually happened, even if a rename failed part way
        undo_path.write_text(json.dumps([[str(new), str(old)] for old, new in done], indent=1), encoding="utf-8")
        update_hash_store(directory, done)
    logger.info(f"Renamed {len(done)} files; hash store backed up as {', '.join(b.name for b in backups) or 'n/a'}")
    logger.info(f"Undo list (new name -> old name) saved as {undo_path.name}")
    return len(done)
