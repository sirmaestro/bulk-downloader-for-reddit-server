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

    The userscript's background sync brings new posts from Reddit day to day, and by default that is all: the archive
    is only read for users with archive checks on (`archive: true` on the user, or `archive_checks: true` for every
    user without their own `archive`). For those, this fills the rest: their history, posts removed from Reddit
    before the browser saw them, and days without a browser. The first check reads the user's whole archive (once;
    `full_archive_done` in the state). After that, every `interval_days` a job reads the window since the previous
    successful run, starting `overlap_days` earlier for posts the archive got late, to its end, so gaps between
    downloaded posts are filled (downloaded posts cost no requests). One scheduled job is queued at a time, with a
    pause between them. A failed run leaves the window where it was and is retried after `retry_hours`.

    Users whose newest known post is more than `hibernate_days` old (stopped posting, banned, moved to another account)
    hibernate: their archive windows aren't read, and only the userscript's free weekly check of Reddit looks for new
    posts. A post sent by the userscript (any job's posts, through `note_posts`) wakes them up again. A user's own
    `archive: true` still gets their whole archive read once while they hibernate. A user's old accounts (`aliases`)
    are never checked; they only say which folder those accounts' posts belong in.

    Accounts the userscript found suspended or deleted on Reddit (`note_gone`) get no archive checks. Those that
    posted within `gone_active_days` stay listed in GET /watchlist, marked `gone`, and are synced as usual: a
    temporary suspension or a shadowban (Reddit answers 404 for those too, so "deleted" may not last) can be lifted.
    Older ones are left out, so the userscript doesn't read them, and listed once more every `gone_recheck_days` for
    another look. A post of theirs sent to the server (`note_posts`) brings them back.

    watchlist.yaml:

        archive_checks: false       # read the archive for every user (default: only users with archive: true)
        interval_days: 7
        overlap_days: 7
        retry_hours: 24
        hibernate_days: 60
        gone_active_days: 14        # suspended or deleted accounts that posted within this long: synced as usual
        gone_recheck_days: 30       # other suspended or deleted accounts: looked at on Reddit again after this long
        users:
          - someone                 # downloads into the folder "someone"
          - name: someone_else
            folder: Some folder
            archive: true           # read someone_else's whole archive once, then weekly windows
          - name: new_account
            aliases: [old_account]  # old_account's posts are in the folder "new_account"; old_account isn't checked
    """

    CHECK_SECONDS = 60
    PAUSE_SECONDS = 60
    DEFAULTS = {
        "interval_days": 7,
        "overlap_days": 7,
        "retry_hours": 24,
        "hibernate_days": 60,
        "gone_active_days": 14,
        "gone_recheck_days": 30,
    }
    FLAG_DEFAULTS = {"archive_checks": False}

    def __init__(self, server, watchlist_path: Path, clock: Callable[[], float] = time.time):
        self.server = server
        self.path = Path(watchlist_path)
        self.state_path = self.path.with_name("watchlist_state.json")
        self.clock = clock
        self.settings = dict(self.DEFAULTS)
        self.users: list[dict] = []
        # Alias name (lower case) -> its folder; replaced whole on reload, so folder_for reads it without the lock
        self.alias_folders: dict[str, str] = {}
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
            self.alias_folders = {}
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
        for key, default in self.FLAG_DEFAULTS.items():
            value = data.get(key, default)
            if not isinstance(value, bool):
                logger.warning(f"Watchlist: '{key}' must be true or false, not {value!r}; using {default}")
                value = default
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
            archive = entry.get("archive")
            if archive is not None and not isinstance(archive, bool):
                logger.warning(f"Watchlist: u/{name}'s 'archive' must be true or false, not {archive!r}; ignoring it")
                archive = None
            users.append(
                {
                    "name": name,
                    "folder": folder,
                    "archive": settings["archive_checks"] if archive is None else archive,
                    "archive_own": archive is True,  # asked for on the user itself: read even while hibernating
                }
            )
            aliases = entry.get("aliases") or []
            if not isinstance(aliases, list):
                logger.warning(f"Watchlist: u/{name}'s aliases must be a list of usernames; ignoring them")
                aliases = []
            for alias in aliases:
                if not isinstance(alias, str) or not USER_PATTERN.fullmatch(alias):
                    logger.warning(f"Watchlist: skipping u/{name}'s alias {alias!r}, which isn't a Reddit username")
                elif alias.lower() in seen:
                    logger.warning(f"Watchlist: u/{alias} is listed more than once; using the first")
                else:
                    seen.add(alias.lower())
                    users.append({"name": alias, "folder": folder, "alias_of": name, "archive": False})
        self.settings, self.users, self.error = settings, users, None
        self.alias_folders = {user["name"].lower(): user["folder"] for user in users if "alias_of" in user}
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
                state = self.state.get(user["name"].lower())
                if self._checked(user, state, now) and self._due(state, now):
                    self._submit(user, now)
                    return

    def _checked(self, user: dict, state: Optional[dict], now: float) -> bool:
        """Whether the scheduler reads this user's archive at all (now)"""
        if not user["archive"]:
            return False  # aliases, and users without archive checks: only the userscript's posts
        if (state or {}).get("gone"):
            return False  # suspended or deleted on Reddit
        if not (state or {}).get("full_archive_done") and user.get("archive_own"):
            return True
        return not self._hibernating(state, now)

    def _gone_hidden(self, state: Optional[dict], now: float) -> bool:
        """Whether a suspended or deleted user is left out of GET /watchlist: unless they posted within
        gone_active_days, until gone_recheck_days after the last look"""
        state = state or {}
        if not state.get("gone") or self._gone_active(state, now):
            return False
        return now - (state.get("gone_checked") or 0) < self.settings["gone_recheck_days"] * DAY

    def _gone_active(self, state: dict, now: float) -> bool:
        """Whether a suspended or deleted user posted recently enough to be synced as usual (maybe only for now)"""
        newest = state.get("newest_post")
        return isinstance(newest, (int, float)) and now - newest < self.settings["gone_active_days"] * DAY

    def _hibernating(self, state: Optional[dict], now: float) -> bool:
        newest = (state or {}).get("newest_post")
        return isinstance(newest, (int, float)) and now - newest > self.settings["hibernate_days"] * DAY

    def _due(self, state: Optional[dict], now: float) -> bool:
        next_check = self._next_check(state)
        return next_check is None or now >= next_check

    def _next_check(self, state: Optional[dict]) -> Optional[float]:
        """When the user's next check is due; None: now"""
        if not state or not state.get("last_attempt"):
            return None
        if state.get("last_error"):
            return state["last_attempt"] + self.settings["retry_hours"] * 3600
        if not state.get("full_archive_done"):
            return None
        return (state.get("last_success") or 0) + self.settings["interval_days"] * DAY

    def folder_for(self, folder: str) -> str:
        """The folder an alias's posts go in when `folder` is an alias's name (the userscript names a user page's folder
        after the user), otherwise `folder`. Lock-free: server.submit calls it while tick() holds the lock."""
        return self.alias_folders.get(folder.lower(), folder)

    def archive_lookups(self, folder: str) -> bool:
        """Whether posts the userscript sends for this folder may be looked up in Arctic Shift (e.g. galleries Reddit
        dropped the images of): only for users with archive checks on, or for unwatched folders with `archive_checks:
        true`. Without a watchlist file, always. Lock-free like folder_for."""
        if self._loaded_text is None:
            return True
        users, settings = self.users, self.settings
        for user in users:
            if "alias_of" not in user and user["folder"].lower() == folder.lower():
                return user["archive"]
        return settings.get("archive_checks", False)

    def note_posts(self, posts: list[dict]):
        """Remembers watched users' newest posts from posts sent to the server (the userscript reads them from
        Reddit), so a hibernating user who posts again is checked again"""
        with self._lock:
            changed = False
            for post in posts:
                author, created = post.get("author"), post.get("created_utc")
                if not isinstance(author, str) or isinstance(created, bool) or not isinstance(created, (int, float)):
                    continue
                state = self.state.get(author.lower())
                if state is None and not any(user["name"].lower() == author.lower() for user in self.users):
                    continue
                state = self.state.setdefault(author.lower(), {})
                if state.get("gone"):
                    # Read from Reddit just now, so the account is there after all
                    logger.info(f"u/{author} has posts on Reddit again; no longer {state['gone']}")
                    state.pop("gone", None)
                    state.pop("gone_checked", None)
                    changed = True
                if created > (state.get("newest_post") or 0):
                    if self._hibernating(state, self.clock()) and not self._hibernating(
                        {"newest_post": created}, self.clock()
                    ):
                        logger.info(f"u/{author} posted again; no longer hibernating")
                    state["newest_post"] = created
                    changed = True
            if changed:
                self._save_state()

    def note_gone(self, name: str, reason: Optional[str]):
        """Records what the userscript found on Reddit for a watched user: "suspended", "deleted", or None (there
        after all). Gone users get no archive checks, and once they haven't posted for gone_active_days, they aren't
        listed in GET /watchlist until gone_recheck_days after the last look."""
        if reason not in (None, "suspended", "deleted"):
            raise ValueError('\'gone\' must be "suspended", "deleted" or null')
        with self._lock:
            self._reload()
            user = next((u for u in self.users if u["name"].lower() == name.lower() and "alias_of" not in u), None)
            if user is None:
                raise ValueError(f"u/{name} isn't on the watchlist")
            state = self.state.setdefault(name.lower(), {})
            if reason is None:
                if state.pop("gone", None):
                    logger.info(f"u/{user['name']} is on Reddit again; synced and checked again")
                state.pop("gone_checked", None)
            else:
                if state.get("gone") != reason:
                    if self._gone_active(state, self.clock()):
                        days = self.settings["gone_active_days"]
                        later = f"still synced while their newest post is under {days:g} days old"
                    else:
                        later = f"not synced or checked for {self.settings['gone_recheck_days']:g} days"
                    logger.info(f"u/{user['name']} is {reason} on Reddit; {later}")
                    self.server.notify(f"u/{user['name']} is {reason} on Reddit")
                state.update(gone=reason, gone_checked=self.clock())
            self._save_state()

    def _submit(self, user: dict, now: float):
        key = user["name"].lower()
        state = self.state.setdefault(key, {})
        payload = {"folder": user["folder"], "user": user["name"]}
        if state.get("full_archive_done") and state.get("window_end"):
            payload["after"] = int(state["window_end"] - self.settings["overlap_days"] * DAY)
        else:
            payload["archive"] = "all"
        state["last_attempt"] = now
        self._pending = key
        try:
            self.server.submit(payload, on_finish=lambda job: self._finished(user, now, job), scheduled=True)
        except ValueError as e:
            self._pending = None
            state["last_error"] = str(e)
            logger.warning(f"Could not queue the baseline for u/{user['name']}: {e}")
        self._save_state()

    def _finished(self, user: dict, started: float, job):
        with self._lock:
            state = self.state.setdefault(user["name"].lower(), {})
            counts = ", ".join(f"{n} {label}" for label, n in job.counts.items())
            newest = getattr(job, "newest_post", None)
            if newest is not None and newest > (state.get("newest_post") or 0):
                state["newest_post"] = newest
            if job.state == "done":
                if job.user and job.full_archive:
                    state["full_archive_done"] = self.clock()
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
            users, gone, now = [], [], self.clock()
            for user in self.users:
                state = self.state.get(user["name"].lower(), {})
                if self._gone_hidden(state, now):
                    recheck = state["gone_checked"] + self.settings["gone_recheck_days"] * DAY
                    gone.append({"name": user["name"], "gone": state["gone"], "recheck": recheck})
                    continue
                checked = self._checked(user, state, now)
                users.append(
                    {
                        "name": user["name"],
                        "folder": user["folder"],
                        "archive": user["archive"],
                        "full_archive_done": state.get("full_archive_done"),
                        "last_success": state.get("last_success"),
                        "last_error": state.get("last_error"),
                        # None: due now, or not checked (no archive checks, hibernating, aliases)
                        "next_check": self._next_check(state) if checked else None,
                        "newest_post": state.get("newest_post"),
                        "hibernating": self._hibernating(state, now),
                        **({"alias_of": user["alias_of"]} if "alias_of" in user else {}),
                        # Due for another look on Reddit: the userscript reports what it finds
                        **({"gone": state["gone"]} if state.get("gone") else {}),
                    }
                )
            # Suspended or deleted users, left out of "users" until their recheck time
            return {"enabled": True, "users": users, "gone": gone, "error": self.error, **self.settings}
