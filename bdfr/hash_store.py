#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import hashlib
import json
import logging
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from sqlite3 import Error, OperationalError
from typing import Optional

from bdfr import exceptions as errors
from bdfr import logs
from bdfr.resource import TEMP_PREFIX

logger = logging.getLogger(__name__)

HASH_DB_NAME = "hashes.sqlite"


def _calc_hash(existing_file: Path):
    chunk_size = 1024 * 1024
    md5_hash = hashlib.md5()
    with existing_file.open("rb") as file:
        chunk = file.read(chunk_size)
        while chunk:
            md5_hash.update(chunk)
            chunk = file.read(chunk_size)
    file_hash = md5_hash.hexdigest()
    return existing_file, file_hash


def hash_files(files: list[Path]) -> list[tuple[Path, str]]:
    """(path, MD5) of each file, several at once: reading and MD5 release the GIL, so threads keep the disk and CPUs
    busy without a process pool's start-up"""
    with ThreadPoolExecutor(min(32, (os.cpu_count() or 1) + 4), thread_name_prefix="bdfr-hash") as pool:
        return list(pool.map(_calc_hash, files))


class HashStore:
    """The download folder's record of what was downloaded: content hash -> file, file -> hash and media URL -> hash.

    Kept either as three JSON files (--keep-hashes; the master_*_list dicts, saved by _save_hash_list) or as
    hashes.sqlite (--keep-hashes-db; tables hashes, hashes_file and hashes_url, paths stored os.fsencode'd). Both modes
    must behave the same, so change both, via _check_hash_exists_or_add / _check_url_exists_or_add.

    Mixed into Connector, which provides self.args and self.download_directory."""

    # When set, _check_hash_exists_or_add and _check_url_exists_or_add leave their changes uncommitted, for the caller
    # to commit once per post with _commit_hash_db: a commit costs ~13 ms through a Docker Desktop bind mount, and a
    # new file needs two or three
    _defer_commits = False

    def _open_hash_store(self):
        self._hash_db = None
        self.master_hash_list = {}
        self._master_hash_list_cnt = self._master_file_list_cnt = self._master_url_list_cnt = 0
        self._check_hash_params()

    def _load_hash_store(self):
        if self.args.search_existing:
            self.master_hash_list, self.master_file_list, self.master_url_list = self.scan_existing_files(
                self.download_directory, self.args.keep_hashes, self._hashed_files_in_db()
            )
            if self.args.keep_hashes_db:
                # The DB is what duplicate checks read in DB mode, so the scan results go there
                self._add_scanned_hashes_to_db(self.master_hash_list)
        elif self.args.keep_hashes:
            self.master_hash_list, self.master_file_list, self.master_url_list = self._load_hash_list(
                self.download_directory
            )
        if self.args.keep_hashes:
            self._master_hash_list_cnt = len(self.master_hash_list)
            self._master_file_list_cnt = len(self.master_file_list)
            self._master_url_list_cnt = len(self.master_url_list)

    def _check_hash_params(self):
        if self.args.keep_hashes_db:
            fn = os.path.join(self.download_directory, HASH_DB_NAME)
            if self.args.keep_hashes:
                # one-time conversion, if hash files and no db file yet
                if not os.path.isfile(os.path.join(self.download_directory, "hash_list.json")):
                    raise Exception("option '--keep-hashes' specified, but no hash files found")
                elif os.path.isfile(fn):
                    raise Exception("Cannot convert hash files, a hash DB already exists.")
                else:
                    self._convert_hash_files_to_db(fn)
            if not os.path.isfile(fn):
                self._create_hash_db(fn)
            self._open_hash_db(fn)

    def _convert_hash_files_to_db(self, db_path: Path):
        # Converted into a temporary file that replaces db_path only once complete, so a failure leaves no partial DB
        # (which would block the next conversion)
        temp_path = f"{db_path}.converting"
        if os.path.exists(temp_path):
            os.remove(temp_path)
        try:
            self._fill_hash_db(temp_path)
            self.close_hash_db()
            os.replace(temp_path, db_path)
        except BaseException:
            self.close_hash_db()
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise

    def _fill_hash_db(self, db_path: str):
        self._create_hash_db(db_path)

        logger.info("Converting hash files...")
        cursor = self._hash_db.cursor()

        fn = os.path.join(self.download_directory, "hash_list.json")
        if os.path.isfile(fn):
            with open(fn) as fp:
                dict_json = json.load(fp)
            data = [(key, os.fsencode(value)) for key, value in dict_json.items()]
            logger.info(f"Loaded {len(dict_json)} hashes")
            query = "INSERT INTO hashes(hash, file_path) VALUES (?, ?)"
            try:
                cursor.executemany(query, data)
            except OperationalError as e:
                raise errors.BulkDownloaderException(f"Could not convert hash_list.json: {e}")
            self._hash_db.commit()
            logger.info(f"Converted {len(dict_json)} hashes")
            dict_json = None
        else:
            raise Exception("hash_list.json missing")

        fn = os.path.join(self.download_directory, "hash_file_list.json")
        if os.path.isfile(fn):
            with open(fn) as fp:
                dict_json = json.load(fp)
            data = [(os.fsencode(key), value) for key, value in dict_json.items()]
            logger.info(f"Loaded {len(dict_json)} file entries")
            query = "INSERT INTO hashes_file(file_path, hash) VALUES (?, ?)"
            try:
                cursor.executemany(query, data)
            except OperationalError as e:
                raise errors.BulkDownloaderException(f"Could not convert hash_file_list.json: {e}")
            self._hash_db.commit()
            logger.info(f"Converted {len(dict_json)} file entries")
            dict_json = None
        else:
            raise Exception("hash_file_list.json missing")

        fn = os.path.join(self.download_directory, "hash_url_list.json")
        if os.path.isfile(fn):
            with open(fn) as fp:
                dict_json = json.load(fp)
            data = [(key, value) for key, value in dict_json.items()]
            logger.info(f"Loaded {len(dict_json)} url entries")
            query = "INSERT OR IGNORE INTO hashes_url(url, hash) VALUES (?, ?)"
            try:
                cursor.executemany(query, data)
            except OperationalError as e:
                raise errors.BulkDownloaderException(f"Could not convert hash_url_list.json: {e}")
            self._hash_db.commit()
            logger.info(f"Converted {len(dict_json)} url entries")
            dict_json = None
        else:
            raise Exception("hash_url_list.json missing")

        self.args.keep_hashes = False

    def _open_hash_db(self, path: Path):
        if self._hash_db is None:
            try:
                # Used from download threads with --parallel, always under Downloader._store_lock
                self._hash_db = sqlite3.connect(path, check_same_thread=False)
            except Error as e:
                raise errors.BulkDownloaderException(f"Could not open hash DB {path}: {e}")

    def _commit_hash_db(self):
        """Commits what the per-row methods left uncommitted (see _defer_commits)"""
        hash_db = getattr(self, "_hash_db", None)
        if hash_db is not None and hash_db.in_transaction:
            hash_db.commit()

    def close_hash_db(self):
        if self._hash_db is not None:
            # Closing discards uncommitted changes
            self._commit_hash_db()
            self._hash_db.close()
            self._hash_db = None

    def _create_hash_db(self, path: Path):
        self._open_hash_db(path)

        cursor = self._hash_db.cursor()
        try:
            query = """
            CREATE TABLE IF NOT EXISTS hashes (
              hash TEXT PRIMARY KEY,
              file_path TEXT NOT NULL
            );
            """
            cursor.execute(query)
            query = """
            CREATE TABLE IF NOT EXISTS hashes_file (
              file_path TEXT PRIMARY KEY,
              hash TEXT NOT NULL
            );
            """
            cursor.execute(query)
            query = """
            CREATE TABLE IF NOT EXISTS hashes_url (
              url TEXT PRIMARY KEY,
              hash TEXT NOT NULL
            );
            """
            cursor.execute(query)
            self._hash_db.commit()
            logger.log(logs.TRACE, "Hash tables created")
        except Error as e:
            logger.critical(f"Error creating hash tables: '{e}'")

    @staticmethod
    def scan_existing_files(
        directory: Path, keep_hashes: bool, known_files: Optional[set[str]] = None
    ) -> (dict[str, Path], dict[str, str], dict[str, str]):
        """Hashes the files in directory, skipping those already in the JSON hash files (keep_hashes) or in
        known_files"""
        if keep_hashes:
            hash_list_loaded, filename_list_loaded, url_list = HashStore._load_hash_list(directory)
        files = []
        for dirpath, _dirnames, filenames in os.walk(directory):
            files.extend([Path(dirpath, file) for file in filenames if not HashStore._is_own_file(file)])
        if known_files:
            files = [file for file in files if str(file) not in known_files]
        if keep_hashes:
            files_new = list()
            for file in files:
                if str(file) not in filename_list_loaded:
                    files_new.append(file)
            files = []
            files.extend(files_new)
        if files:
            logger.info(f"Calculating hashes for {len(files)} files not in the hash store yet")
            results = hash_files(files)
        else:
            logger.log(logs.TRACE, "Every file in the download folder is already in the hash store")
            results = []

        hash_list = {res[1]: res[0] for res in results}
        filename_list = {}
        if keep_hashes:
            filename_list = {str(res[0]): res[1] for res in results}
            filename_list_loaded.update(filename_list)
            filename_list = filename_list_loaded
            hash_list_loaded.update(hash_list)
            hash_list = hash_list_loaded
            if len(files_new) > 0:
                HashStore._save_hash_list(directory, hash_list, filename_list, url_list)
        else:
            url_list = {}
        return (hash_list, filename_list, url_list)

    @staticmethod
    def _load_hash_list(directory: Path) -> (dict[str, Path], dict[str, str], dict[str, str]):
        logger.info("Loading hashes...")

        fn = os.path.join(directory, "hash_list.json")
        hash_list = {}
        if os.path.isfile(fn):
            with open(fn) as fp:
                dict_json = json.load(fp)
            for x in dict_json:
                hash_list[x] = Path(dict_json[x])
        logger.info(f"Loaded {len(hash_list)} hashes")

        fn = os.path.join(directory, "hash_file_list.json")
        filename_list = {}
        if os.path.isfile(fn):
            with open(fn) as fp:
                filename_list = json.load(fp)
        logger.info(f"Loaded {len(filename_list)} file entries")

        fn = os.path.join(directory, "hash_url_list.json")
        url_list = {}
        if os.path.isfile(fn):
            with open(fn) as fp:
                url_list = json.load(fp)
        logger.info(f"Loaded {len(url_list)} url entries")

        return (hash_list, filename_list, url_list)

    @staticmethod
    def _save_hash_list(
        directory: Path, hash_list: dict[str, Path], filename_list: dict[str, str], url_list: dict[str, str]
    ):
        hash_json = {file_hash: str(path) for file_hash, path in hash_list.items()}
        HashStore._write_json(os.path.join(directory, "hash_list.json"), hash_json)
        logger.info(f"Saved {len(hash_list)} hashes")
        HashStore._write_json(os.path.join(directory, "hash_file_list.json"), filename_list)
        logger.info(f"Saved {len(filename_list)} file entries")
        HashStore._write_json(os.path.join(directory, "hash_url_list.json"), url_list)
        logger.info(f"Saved {len(url_list)} url entries")

    @staticmethod
    def _write_json(fn: str, data: dict):
        # Write to a temporary file first so an interrupted save never leaves a truncated file behind
        with open(fn + ".tmp", "w") as fp:
            json.dump(data, fp)
        if os.path.exists(fn):
            os.replace(fn, fn + ".bak")
        os.replace(fn + ".tmp", fn)

    @staticmethod
    def _is_own_file(name: str) -> bool:
        """BDFR's hash stores, their backups, `bdfr rename` undo lists, downloads in progress and `bdfr similar`'s
        cache live in the download directory but are not downloads"""
        own = (
            "hash_list.json",
            "hash_file_list.json",
            "hash_url_list.json",
            "bdfr-rename-",
            TEMP_PREFIX,
            "phashes.sqlite",
        )
        return name.startswith(own) or name.startswith(HASH_DB_NAME)

    def _hashed_files_in_db(self) -> Optional[set[str]]:
        if not self.args.keep_hashes_db:
            return None
        rows = self._hash_db.execute("SELECT file_path FROM hashes_file").fetchall()
        return {os.fsdecode(row[0]) for row in rows}

    def _add_scanned_hashes_to_db(self, hash_list: dict[str, Path]):
        data = [(file_hash, os.fsencode(str(path))) for file_hash, path in hash_list.items()]
        with self._hash_db:
            self._hash_db.executemany("INSERT OR IGNORE INTO hashes(hash, file_path) VALUES (?, ?)", data)
            self._hash_db.executemany(
                "INSERT OR IGNORE INTO hashes_file(file_path, hash) VALUES (?, ?)", [(p, h) for h, p in data]
            )
        if data:
            logger.info(f"Added {len(data)} scanned hashes to the hash DB")

    def _hash_list_save(self, periodic: bool):
        # no save necessary for keep_hashes_db, only for keep_hashes
        if self.args.keep_hashes and (
            not periodic
            or periodic
            and self.args.save_hashes_interval > 0
            and (
                len(self.master_hash_list) - self._master_hash_list_cnt > self.args.save_hashes_interval
                or len(self.master_file_list) - self._master_file_list_cnt > self.args.save_hashes_interval
                or len(self.master_url_list) - self._master_url_list_cnt > self.args.save_hashes_interval
            )
        ):
            self._save_hash_list(
                self.download_directory, self.master_hash_list, self.master_file_list, self.master_url_list
            )
            self._master_hash_list_cnt = len(self.master_hash_list)
            self._master_file_list_cnt = len(self.master_file_list)
            self._master_url_list_cnt = len(self.master_url_list)

    def _check_hash_exists_or_add(self, file_path_obj: Path, hash: str):
        file_path = str(file_path_obj)
        if self.args.keep_hashes_db:
            file_path = os.fsencode(file_path)
            cursor = self._hash_db.cursor()
            if file_path_obj is None:
                query = "SELECT file_path FROM hashes WHERE hash = ?"
                cursor.execute(query, [hash])
                row = cursor.fetchone()
                cursor.close()
                return row is not None
            query = "SELECT hash FROM hashes_file WHERE file_path = ?"
            cursor.execute(query, [file_path])
            hash_found = cursor.fetchone()
            if hash_found and hash == hash_found[0]:
                cursor.close()
                return True
            if hash is not None:
                query = "INSERT OR REPLACE INTO hashes(hash, file_path) VALUES (?, ?)"
                cursor.execute(query, (hash, file_path))
                query = "INSERT OR REPLACE INTO hashes_file(file_path, hash) VALUES (?, ?)"
                cursor.execute(query, (file_path, hash))
                if not self._defer_commits:
                    self._hash_db.commit()
            cursor.close()
        elif self.args.keep_hashes:
            if file_path_obj is None:
                return hash in self.master_hash_list
            hash_found = self.master_file_list.get(file_path, None)
            if hash_found is not None and hash == hash_found:
                return True
            if hash is not None:
                self.master_hash_list[hash] = file_path
                self.master_file_list[file_path] = hash
                self._hash_list_save(True)
        return False

    def _check_url_exists_or_add(self, url: str, hash: str):
        if self.args.keep_hashes_db:
            query = "SELECT hash FROM hashes_url WHERE url = ?"
            cursor = self._hash_db.cursor()
            cursor.execute(query, [url])
            row = cursor.fetchone()
            if row:
                cursor.close()
                return True
            if hash is not None:
                query = "INSERT OR IGNORE INTO hashes_url(url, hash) VALUES (?, ?)"
                cursor.execute(query, (url, hash))
                if not self._defer_commits:
                    self._hash_db.commit()
            cursor.close()
        elif self.args.keep_hashes:
            if url in self.master_url_list:
                return True
            if hash is not None:
                self.master_url_list[url] = hash
                self._hash_list_save(True)
        return False

    def _forget_url(self, url: str):
        """Removes a key from the URL table"""
        if self.args.keep_hashes_db:
            self._hash_db.execute("DELETE FROM hashes_url WHERE url = ?", (url,))
            if not self._defer_commits:
                self._hash_db.commit()
        elif self.args.keep_hashes:
            if self.master_url_list.pop(url, None) is not None:
                self._hash_list_save(True)

    def _get_hashed_item(self, hash: str) -> Path:
        if self.args.keep_hashes_db:
            query = "SELECT file_path FROM hashes WHERE hash = ?"
            cursor = self._hash_db.cursor()
            cursor.execute(query, [hash])
            row = cursor.fetchone()
            cursor.close()
            if row:
                return Path(os.fsdecode(row[0]))
            else:
                return None
        else:
            return self.master_hash_list.get(hash)

    def _get_url_hash(self, url: str) -> Optional[str]:
        """The hash stored for a downloaded media URL, if any"""
        if self.args.keep_hashes_db:
            row = self._hash_db.execute("SELECT hash FROM hashes_url WHERE url = ?", [url]).fetchone()
            return row[0] if row else None
        if self.args.keep_hashes:
            return self.master_url_list.get(url)
        return None

    def _get_file_hash(self, path: Path) -> Optional[str]:
        """The hash stored for a file, if any"""
        if self.args.keep_hashes_db:
            query = "SELECT hash FROM hashes_file WHERE file_path = ?"
            row = self._hash_db.execute(query, [os.fsencode(str(path))]).fetchone()
            return row[0] if row else None
        if self.args.keep_hashes:
            return self.master_file_list.get(str(path))
        return None

    def _move_hashed_file(self, old_path: Path, new_path: Path, file_hash: str):
        """Points the hash store at a file's new name after it was renamed"""
        if self.args.keep_hashes_db:
            old, new = os.fsencode(str(old_path)), os.fsencode(str(new_path))
            with self._hash_db:
                self._hash_db.execute("UPDATE hashes SET file_path = ? WHERE hash = ?", (new, file_hash))
                self._hash_db.execute("DELETE FROM hashes_file WHERE file_path = ?", (old,))
                self._hash_db.execute(
                    "INSERT OR REPLACE INTO hashes_file(file_path, hash) VALUES (?, ?)", (new, file_hash)
                )
        elif self.args.keep_hashes:
            self.master_hash_list[file_hash] = str(new_path)
            self.master_file_list.pop(str(old_path), None)
            self.master_file_list[str(new_path)] = file_hash
            self._hash_list_save(True)
        else:
            self.master_hash_list[file_hash] = new_path
