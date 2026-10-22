#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""The failed-post retry list behind `bdfr serve`: posts that failed in a job (a host down, a 429, a gallery stopped
part way) are kept in failed_posts.json in BDFR's configuration directory and tried again later, with growing waits, as
low-priority posts-only jobs that never ask Arctic Shift for anything."""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

from bdfr.server import MAX_POSTS_PER_JOB

logger = logging.getLogger(__name__)

HOUR = 3600


class RetryList:
    # The wait before each retry; after the last one fails the post is given up on (still listed, and dropped once a
    # job downloads it after all)
    BACKOFF_HOURS = (1, 6, 24, 72, 168)
    CHECK_SECONDS = 60

    def __init__(self, server, path: Path, clock: Callable[[], float] = time.time):
        self.server = server
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.Lock()
        # "folder/post ID" -> {folder, post, error, attempts, failed, next_try (None once given up)}
        self.entries: dict[str, dict] = self._load()
        # Entries in a retry job that is queued or running, so they aren't queued again
        self._pending: set[str] = set()

    @staticmethod
    def key(folder: str, post_id: str) -> str:
        return f"{folder}/{post_id}"

    def start(self):
        threading.Thread(target=self._loop, name="bdfr-serve-retries", daemon=True).start()

    def _loop(self):
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("Retrying failed posts failed")
            time.sleep(self.CHECK_SECONDS)

    def note_job(self, folder: str, results: dict[str, str], failed_posts: dict[str, tuple[dict, str]]):
        """Records a finished job's posts: failed ones (ID -> (post data, reason)) are kept for a retry, and any other
        result for a kept post (downloaded, already there, filtered out) drops it"""
        now = self.clock()
        changed = False
        with self._lock:
            for post_id, label in results.items():
                key = self.key(folder, post_id)
                if label != "failed":
                    changed |= self.entries.pop(key, None) is not None
                    continue
                if post_id not in failed_posts:
                    continue
                data, reason = failed_posts[post_id]
                entry = self.entries.get(key) or {"folder": folder, "attempts": 0}
                entry.update(post=data, error=reason, failed=now)
                self._schedule(entry, post_id, now)
                self.entries[key] = entry
                changed = True
            if changed:
                self._save()

    def tick(self):
        """Queues the posts that are due, one job per folder (of up to MAX_POSTS_PER_JOB posts)"""
        now = self.clock()
        with self._lock:
            due: dict[str, list[str]] = {}
            for key, entry in self.entries.items():
                if key not in self._pending and self._due(entry, now):
                    due.setdefault(entry["folder"], []).append(key)
            batches = []
            for folder, keys in due.items():
                for start in range(0, len(keys), MAX_POSTS_PER_JOB):
                    chunk = keys[start : start + MAX_POSTS_PER_JOB]
                    batches.append((folder, chunk, [self.entries[key]["post"] for key in chunk]))
                self._pending.update(keys)
        for folder, chunk, posts in batches:
            try:
                self.server.submit(
                    {"folder": folder, "posts": posts},
                    on_finish=lambda job, chunk=chunk: self._finished(chunk),
                    scheduled=True,
                    retry=True,
                )
            except ValueError as e:
                # E.g. a folder name that's no longer accepted: dropped rather than tried every minute
                logger.warning(f"Dropping {len(chunk)} failed posts in {folder} from the retry list: {e}")
                with self._lock:
                    for key in chunk:
                        self.entries.pop(key, None)
                    self._pending.difference_update(chunk)
                    self._save()

    def _finished(self, keys: list[str]):
        """After a retry job (whose results note_job has recorded): posts it didn't get to, e.g. because it stopped
        with an error, count as failed again, so they aren't queued again at once"""
        now = self.clock()
        with self._lock:
            self._pending.difference_update(keys)
            unreported = [key for key in keys if key in self.entries and self._due(self.entries[key], now)]
            for key in unreported:
                self._schedule(self.entries[key], key.split("/", 1)[1], now)
            if unreported:
                self._save()

    def _schedule(self, entry: dict, name: str, now: float):
        """Counts a failure of the entry and sets its next try, or gives up after the last"""
        entry["attempts"] = entry.get("attempts", 0) + 1
        if entry["attempts"] > len(self.BACKOFF_HOURS):
            if entry.get("next_try") is not None:
                logger.warning(f"Giving up on post {name} in {entry['folder']} after {entry['attempts']} failures")
            entry["next_try"] = None
        else:
            entry["next_try"] = now + self.BACKOFF_HOURS[entry["attempts"] - 1] * HOUR

    @staticmethod
    def _due(entry: dict, now: float) -> bool:
        return entry.get("next_try") is not None and entry["next_try"] <= now

    def snapshot(self) -> dict:
        """The counts for GET /status"""
        with self._lock:
            waiting = sum(1 for entry in self.entries.values() if entry.get("next_try") is not None)
            return {"waiting": waiting, "given_up": len(self.entries) - waiting}

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            logger.warning(f"Could not read {self.path} ({e}); starting a new retry list")
            return {}
        entries = data.get("posts") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            return {}
        return {
            key: entry
            for key, entry in entries.items()
            if isinstance(entry, dict) and isinstance(entry.get("post"), dict) and isinstance(entry.get("folder"), str)
        }

    def _save(self):
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(json.dumps({"posts": self.entries}, indent=1), encoding="utf-8")
            os.replace(temp, self.path)
        except OSError as e:
            logger.warning(f"Could not save {self.path}: {e}")
