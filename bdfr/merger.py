#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""`bdfr merge`: moves a user's old folder into the folder of their new account (an account move), offline.

Files whose content is already in the new folder stay where they are; the rest are moved, and the new folder's hash
store (hashes.sqlite, or the hash_*.json files) gains their hashes and every link the old folder's store recorded
(including the post:/incomplete: keys), so posts downloaded under the old account are still recognised. Nothing is
requested from anywhere."""

import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path

from bdfr.exceptions import BulkDownloaderException
from bdfr.hash_store import HashStore, hash_files
from bdfr.renamer import HASH_DB_SCHEMA, back_up_hash_store, media_files

logger = logging.getLogger(__name__)

DB_NAME = "hashes.sqlite"
JSON_NAMES = ("hash_list.json", "hash_file_list.json", "hash_url_list.json")


def _store_mode(directory: Path) -> str | None:
    if (directory / DB_NAME).exists():
        return "db"
    if any((directory / name).exists() for name in JSON_NAMES):
        return "json"
    return None


def _read_store(directory: Path) -> tuple[dict[str, str], dict[str, str]]:
    """The folder's stored (file path -> hash) and (link -> hash) entries, from either kind of store"""
    files, urls = {}, {}
    if _store_mode(directory) == "db":
        connection = sqlite3.connect(f"{(directory / DB_NAME).as_uri()}?mode=ro", uri=True)
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "hashes_file" in tables:
                files = {
                    os.fsdecode(path): h for path, h in connection.execute("SELECT file_path, hash FROM hashes_file")
                }
            if "hashes_url" in tables:
                urls = dict(connection.execute("SELECT url, hash FROM hashes_url"))
        finally:
            connection.close()
    elif _store_mode(directory) == "json":
        _hashes, files, urls = HashStore._load_hash_list(directory)
    return files, urls


def merge_folders(old: Path, new: Path, dry_run: bool = False) -> dict:
    """Moves old's files into new, except content new already has, and merges old's hash store into new's"""
    old, new = old.expanduser().resolve(), new.expanduser().resolve()
    for folder in (old, new):
        if not folder.is_dir():
            raise BulkDownloaderException(f"{folder} is not a folder")
    if old == new or old in new.parents or new in old.parents:
        raise BulkDownloaderException("The two folders must be separate, neither inside the other")

    old_files, old_urls = _read_store(old)
    new_files, new_urls = _read_store(new)
    # The new folder's content: its stored hashes, plus the hashes of files the store doesn't know (stored paths
    # can differ, e.g. a store written in Docker read outside it; those files are simply hashed)
    present = media_files(new)
    unknown = [path for path in present if str(path) not in new_files]
    if unknown:
        logger.info(f"Hashing {len(unknown)} files in {new.name} that its hash store doesn't list")
    new_hashes = set(new_files.values()) | {h for _path, h in hash_files(unknown)}

    candidates = media_files(old)
    logger.info(f"Hashing {len(candidates)} files in {old.name}")
    moves, duplicates, conflicts = [], [], []
    for path, file_hash in hash_files(candidates):
        target = new / path.relative_to(old)
        if file_hash in new_hashes:
            duplicates.append(path)
        elif target.exists():
            conflicts.append(path)
        else:
            moves.append((path, target, file_hash))
            new_hashes.add(file_hash)
    links = {url: h for url, h in old_urls.items() if url not in new_urls}
    for path in conflicts:
        logger.warning(f"Not moving {path.name}: {new.name} has a different file with that name")
    for path in duplicates:
        logger.debug(f"Not moving {path.name}: its content is already in {new.name}")
    action = "Would move" if dry_run else "Moving"
    logger.info(f"{action} {len(moves)} files and {len(links)} stored links from {old.name} into {new.name}")
    result = {"moved": 0, "to_move": len(moves), "duplicates": len(duplicates), "conflicts": len(conflicts)}
    result["links"] = len(links)
    if dry_run:
        return result

    mode = _store_mode(new) or _store_mode(old)
    backups = back_up_hash_store(new, time.strftime("%Y%m%d-%H%M%S"), "merge")
    done = []
    try:
        for source, target, file_hash in moves:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(source, target)
            done.append((target, file_hash))
    finally:
        # Whatever was moved is recorded, even if a move failed part way
        if mode == "db":
            _add_to_db(new, done, links)
        elif mode == "json":
            _add_to_json(new, done, links)
    result["moved"] = len(done)
    if backups:
        logger.info(f"Hash store backed up as {', '.join(b.name for b in backups)}")
    left = len(media_files(old))
    if left:
        logger.info(f"{left} files left in {old} (already in {new.name}, or a name taken); check, then delete it")
    else:
        logger.info(f"Nothing left to keep in {old}; it can be deleted")
    logger.info(f"In watchlist.yaml, under the user of {new.name}:  aliases: [{old.name}]")
    return result


def _add_to_db(directory: Path, files: list[tuple[Path, str]], links: dict[str, str]):
    connection = sqlite3.connect(directory / DB_NAME)
    try:
        with connection:
            connection.executescript(HASH_DB_SCHEMA)
            rows = [(file_hash, os.fsencode(str(path))) for path, file_hash in files]
            connection.executemany("INSERT OR IGNORE INTO hashes(hash, file_path) VALUES (?, ?)", rows)
            connection.executemany(
                "INSERT OR REPLACE INTO hashes_file(file_path, hash) VALUES (?, ?)", [(p, h) for h, p in rows]
            )
            connection.executemany("INSERT OR IGNORE INTO hashes_url(url, hash) VALUES (?, ?)", links.items())
    finally:
        connection.close()


def _add_to_json(directory: Path, files: list[tuple[Path, str]], links: dict[str, str]):
    hash_list, file_list, url_list = HashStore._load_hash_list(directory)
    for path, file_hash in files:
        hash_list.setdefault(file_hash, path)
        file_list[str(path)] = file_hash
    for url, file_hash in links.items():
        url_list.setdefault(url, file_hash)
    HashStore._save_hash_list(directory, hash_list, file_list, url_list)


def merge_summary(result: dict, dry_run: bool) -> str:
    moved = f"{result['to_move']} to move" if dry_run else f"{result['moved']} moved"
    return (
        f"{moved}, {result['duplicates']} already there, {result['conflicts']} with a name taken, "
        f"{result['links']} stored links {'to add' if dry_run else 'added'}"
    )
