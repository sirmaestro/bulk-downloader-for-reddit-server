#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""The weekly Arctic Shift baseline behind `bdfr serve`: keeps the folders of the users in watchlist.yaml complete."""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import yaml

from bdfr.server import FOLDER_PATTERN, USER_PATTERN

logger = logging.getLogger(__name__)

DAY = 86400


class BaselineScheduler:
    """Fills what the browser can't see from Arctic Shift, gently.

    The userscript's background sync brings new posts from Reddit day to day. This fills the rest: a user's history on
    their first run, posts removed from Reddit before the browser saw them, and days without a browser. Each user's
    first run reads their whole archive. After that, every `interval_days` a job reads the window since the previous
    successful run, starting `overlap_days` earlier for posts the archive got late, to its end, so gaps between
    downloaded posts are filled (downloaded posts cost no requests). One scheduled job is queued at a time, with a
    pause between them. A failed run leaves the window where it was and is retried after `retry_hours`.

    watchlist.yaml:

        interval_days: 7
        overlap_days: 7
        retry_hours: 24
        users:
          - someone                 # downloads into the folder "someone"
          - name: someone_else
            folder: Some folder
    """

    CHECK_SECONDS = 60
    PAUSE_SECONDS = 60
    DEFAULTS = {"interval_days": 7, "overlap_days": 7, "retry_hours": 24}

    def __init__(self, server, watchlist_path: Path, clock: Callable[[], float] = time.time):
        self.server = server
        self.path = Path(watchlist_path)
        self.state_path = self.path.with_name("watchlist_state.json")
        self.clock = clock
        self.settings = dict(self.DEFAULTS)
        self.users: list[dict] = []
        # Why the watchlist file couldn't be used, shown by GET /watchlist until it's fixed
        self.error: Optional[str] = None
        self._loaded_text: Optional[str] = None
        self._lock = threading.Lock()
        self._pending: Optional[str] = None  # the user whose scheduled job is queued or running
        self._last_finished = 0.0
        self.state: dict[str, dict] = self._load_state()

    def start(self):
        threading.Thread(target=self._loop, name="bdfr-serve-scheduler", daemon=True).start()

    def _loop(self):
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("Checking the watchlist failed")
            time.sleep(self.CHECK_SECONDS)

    # --- the watchlist ---

    def _reload(self):
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self.users, self._loaded_text, self.error = [], None, None
            return
        except OSError as e:
            self._fail(f"Could not read the watchlist {self.path}: {e}; keeping the previous one")
            return
        if text == self._loaded_text:
            return
        self._loaded_text = text
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError as e:
            self._fail(f"Could not parse the watchlist {self.path}: {e}; keeping the previous one")
            return
        if not isinstance(data, dict) or not isinstance(data.get("users", []), list):
            self._fail(f"The watchlist {self.path} needs a 'users' list; keeping the previous one")
            return
        settings = dict(self.DEFAULTS)
        for key in self.DEFAULTS:
            value = data.get(key, settings[key])
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                logger.warning(f"Watchlist: '{key}' must be a positive number, not {value!r}; using {settings[key]}")
                continue
            settings[key] = value
        users, seen = [], set()
        for entry in data.get("users") or []:
            if isinstance(entry, str):
                entry = {"name": entry}
            name = entry.get("name") if isinstance(entry, dict) else None
            folder = str(entry.get("folder") or name or "").strip() if isinstance(entry, dict) else ""
            if not isinstance(name, str) or not USER_PATTERN.fullmatch(name):
                logger.warning(f"Watchlist: skipping {entry!r}, which isn't a Reddit username")
                continue
            if not FOLDER_PATTERN.fullmatch(folder) or folder.strip(".") == "":
                logger.warning(f"Watchlist: skipping u/{name}: {folder!r} isn't a single folder name")
                continue
            if name.lower() in seen:
                logger.warning(f"Watchlist: u/{name} is listed more than once; using the first")
                continue
            seen.add(name.lower())
            users.append({"name": name, "folder": folder})
        self.settings, self.users, self.error = settings, users, None
        logger.info(f"Watchlist: {len(users)} users from {self.path}")

    def _fail(self, message: str):
        logger.warning(message)
        self.error = message

    # --- scheduling ---

    def tick(self):
        """Queues the next due user's job, unless a scheduled job is still queued or running or only just finished"""
        with self._lock:
            self._reload()
            now = self.clock()
            if self._pending is not None or now - self._last_finished < self.PAUSE_SECONDS:
                return
            for user in self.users:
                if self._due(self.state.get(user["name"].lower()), now):
                    self._submit(user, now)
                    return

    def _due(self, state: Optional[dict], now: float) -> bool:
        next_check = self._next_check(state)
        return next_check is None or now >= next_check

    def _next_check(self, state: Optional[dict]) -> Optional[float]:
        if not state or not state.get("last_attempt"):
            return None
        if state.get("last_error"):
            return state["last_attempt"] + self.settings["retry_hours"] * 3600
        return (state.get("last_success") or 0) + self.settings["interval_days"] * DAY

    def _submit(self, user: dict, now: float):
        key = user["name"].lower()
        state = self.state.setdefault(key, {})
        payload = {"folder": user["folder"], "user": user["name"]}
        if state.get("window_end"):
            payload["after"] = int(state["window_end"] - self.settings["overlap_days"] * DAY)
        else:
            payload["archive"] = "all"
        state["last_attempt"] = now
        self._pending = key
        try:
            self.server.submit(payload, on_finish=lambda job: self._finished(user, now, job))
        except ValueError as e:
            self._pending = None
            state["last_error"] = str(e)
            logger.warning(f"Could not queue the baseline for u/{user['name']}: {e}")
        self._save_state()

    def _finished(self, user: dict, started: float, job):
        with self._lock:
            state = self.state.setdefault(user["name"].lower(), {})
            counts = ", ".join(f"{n} {label}" for label, n in job.counts.items())
            if job.state == "done":
                state.update(window_end=started, last_success=self.clock(), last_error=None)
                logger.info(f"Baseline for u/{user['name']} done: {counts}")
            else:
                state["last_error"] = job.error or f"job {job.state}"
                logger.warning(
                    f"Baseline for u/{user['name']} failed ({state['last_error']}); the same window is read again in "
                    f"{self.settings['retry_hours']} hours"
                )
            state["last_counts"] = dict(job.counts)
            self._pending = None
            self._last_finished = self.clock()
            self._save_state()

    # --- state ---

    def _load_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            logger.warning(f"Could not read {self.state_path} ({e}); every watched user's whole archive is read again")
            return {}
        users = data.get("users") if isinstance(data, dict) else None
        return users if isinstance(users, dict) else {}

    def _save_state(self):
        temp = self.state_path.with_name(self.state_path.name + ".tmp")
        try:
            temp.write_text(json.dumps({"users": self.state}, indent=1), encoding="utf-8")
            os.replace(temp, self.state_path)
        except OSError as e:
            logger.warning(f"Could not save {self.state_path}: {e}")

    def snapshot(self) -> dict:
        with self._lock:
            self._reload()
            users = []
            for user in self.users:
                state = self.state.get(user["name"].lower(), {})
                users.append(
                    {
                        "name": user["name"],
                        "folder": user["folder"],
                        "last_success": state.get("last_success"),
                        "last_error": state.get("last_error"),
                        "next_check": self._next_check(state),
                    }
                )
            return {"enabled": True, "users": users, "error": self.error, **self.settings}
