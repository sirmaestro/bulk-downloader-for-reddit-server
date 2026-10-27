#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""The record of `bdfr serve`'s requests to Arctic Shift: every attempt (failed ones and retries included), with the
job it was for and why, appended to arctic_shift_requests.jsonl in BDFR's configuration directory and kept for
KEEP_DAYS (the file is rewritten without older ones at start and about once a day). The dashboard's Archive tab
shows it (GET /requests); reading it never makes a request."""

import json
import logging
import os
import threading
import time
from collections import Counter, deque
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

DAY = 24 * 3600
# GET /requests' periods, by name
PERIODS = {"day": DAY, "week": 7 * DAY, "month": 30 * DAY}


class RequestLog:
    KEEP_DAYS = 30
    # The most recent requests listed one by one
    RECENT = 200
    # Users listed by requests in the last week
    TOP_USERS = 50

    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.Lock()
        # When the file was last rewritten without requests older than KEEP_DAYS
        self._rewritten = self.clock()
        self.entries: deque[dict] = deque(self._load())

    def record(self, entry: dict):
        """Adds one request (ArcticShift.on_request's dict plus the job's id, folder, user and purpose)"""
        with self._lock:
            self.entries.append(entry)
            now = self.clock()
            oldest = now - self.KEEP_DAYS * DAY
            trimmed = False
            while self.entries and self.entries[0].get("time", 0) < oldest:
                self.entries.popleft()
                trimmed = True
            if trimmed and now - self._rewritten >= DAY:
                # A serve running for months would otherwise only ever add to the file
                self._rewritten = now
                self._rewrite(self.entries)
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as file:
                    file.write(json.dumps(entry) + "\n")
            except OSError as e:
                logger.warning(f"Could not add to {self.path}: {e}")

    def summary(self) -> dict:
        """GET /requests: counts per period (all, failed), per purpose, per lookup reason (posts), per user (last week)
        and failure reason (last week), and the latest requests, newest first"""
        now = self.clock()
        with self._lock:
            entries = list(self.entries)

        def period_counts() -> dict:
            return {name: {"requests": 0, "failed": 0, "posts": 0} for name in PERIODS}

        totals = period_counts()
        purposes: dict[str, dict] = {}
        lookups: dict[str, dict] = {}
        users: dict[str, dict] = {}
        errors: Counter = Counter()
        for entry in entries:
            age = now - entry.get("time", 0)
            failed = entry.get("status") != 200
            for name, seconds in PERIODS.items():
                if age > seconds:
                    continue
                for counts in (
                    totals[name],
                    purposes.setdefault(entry.get("purpose") or "other", period_counts())[name],
                ):
                    counts["requests"] += 1
                    counts["failed"] += failed
                    counts["posts"] += entry.get("posts") or 0
                if not failed:
                    for reason, n in (entry.get("lookups") or {}).items():
                        lookups.setdefault(reason, {period: 0 for period in PERIODS})[name] += n
            if age <= PERIODS["week"]:
                if failed:
                    errors[entry.get("error") or f"HTTP {entry.get('status')}"] += 1
                if entry.get("user"):
                    user = users.setdefault(entry["user"], {"user": entry["user"], "requests": 0, "failed": 0})
                    user["requests"] += 1
                    user["failed"] += failed
                    user["last"] = entry.get("time")
        return {
            "path": str(self.path),
            "since": entries[0].get("time") if entries else None,
            "keep_days": self.KEEP_DAYS,
            "totals": totals,
            "purposes": sorted(
                ({"purpose": purpose, **counts} for purpose, counts in purposes.items()),
                key=lambda row: -row["month"]["requests"],
            ),
            "lookups": sorted(
                ({"reason": reason, **counts} for reason, counts in lookups.items()), key=lambda row: -row["month"]
            ),
            "users": sorted(users.values(), key=lambda row: -row["requests"])[: self.TOP_USERS],
            "errors": [{"error": error, "count": n} for error, n in errors.most_common()],
            "recent": entries[-self.RECENT :][::-1],
        }

    def _load(self) -> list[dict]:
        """Reads the kept requests, and rewrites the file without ones older than KEEP_DAYS"""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError as e:
            logger.warning(f"Could not read {self.path} ({e}); starting a new record of Arctic Shift requests")
            return []
        oldest = self.clock() - self.KEEP_DAYS * DAY
        entries = []
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and isinstance(entry.get("time"), (int, float)) and entry["time"] >= oldest:
                entries.append(entry)
        if len(entries) < len(lines):
            self._rewrite(entries)
        return entries

    def _rewrite(self, entries):
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
            os.replace(temp, self.path)
        except OSError as e:
            logger.warning(f"Could not save {self.path}: {e}")
