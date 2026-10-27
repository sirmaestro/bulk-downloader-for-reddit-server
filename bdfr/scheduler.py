#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""The weekly Arctic Shift baseline behind `bdfr serve`: keeps the folders of the users in watchlist.yaml complete."""

import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import yaml

from bdfr.server import FOLDER_PATTERN, USER_PATTERN

logger = logging.getLogger(__name__)

DAY = 86400
GB = 10**9


class BaselineScheduler:
    """Fills what the browser can't see from Arctic Shift, gently.

    The userscript's background sync brings new posts from Reddit day to day; this fills the rest from the archive for
    every watched user: their history, posts removed from Reddit before the browser saw them, and days without a
    browser. A user's whole archive is read once (`full_archive_done` in the state), newest first, but only after the
    userscript has read the user's Reddit listing to its end (`reddit_read`, POST /watchlist/reddit; Reddit lists the
    newest 1000 posts), so the archive is only needed for what Reddit can't show. The read stops after two pages of
    100 posts in a row with under 10% of their media left (older posts are then very likely gone too), or once it has
    written the user's storage limit (`archive_limit_gb`, raised per user with POST /watchlist/continue, the dashboard's
    Continue; `archive_bytes` in the state): then it waits (`full_archive_capped`) and later carries on below the oldest
    post it reached (`full_archive_before`). A user's whole-archive read can also be stopped for good (POST
    /watchlist/stop, the dashboard's Stop; `full_archive_stopped`): then only the weekly windows are read, from when
    it was stopped if they hadn't begun, until Continue. No archive is read while the disk holding the folders has under
    `min_free_gb` free. Every `interval_days` a job reads the window since the previous successful run (or since the
    whole-archive read began), starting `overlap_days` earlier for posts the archive got late, to its end, so gaps
    between downloaded posts are filled (downloaded posts cost no requests). One scheduled job is queued at a time,
    with a pause between them. A failed run leaves the window where it was and is retried after `retry_hours`; a failed
    whole-archive read carries on below the oldest post it reached (`full_archive_before`; `full_archive_began` keeps
    when a read from the newest post began, where the windows start), and posts that failed above it aren't read
    again, so its listing pages aren't asked for twice.

    Removed galleries the folder knows of without their image list (Reddit drops it; see unavailable_post_key), and
    failed galleries in the retry list without one, are looked up in the archive by ID (100 per request), at most
    once every `retry_hours` per user, hibernating or not. Ones the archive doesn't have, or has without an image list,
    are recorded as gone and not looked up again.

    Users whose newest known post is more than `hibernate_days` old (stopped posting, banned, moved to another account)
    hibernate: no weekly windows are read for them, and only the userscript's free weekly check of Reddit looks for
    new posts. A post sent by the userscript (any job's posts, through `note_posts`) wakes them up again. Their whole
    archive is still read once if it hasn't been (they may have been quiet when added), after active users' reads. A
    user's old accounts (`aliases`) are never checked; they only say which folder those accounts' posts belong in.

    Accounts the userscript found suspended or deleted on Reddit (`note_gone`) get no archive checks. Those that
    posted within `gone_active_days` stay listed in GET /watchlist, marked `gone`, and are synced as usual: a
    temporary suspension or a shadowban (Reddit answers 404 for those too, so "deleted" may not last) can be lifted.
    Older ones are left out, so the userscript doesn't read them, and listed once more every `gone_recheck_days` for
    another look. A post of theirs sent to the server (`note_posts`) brings them back.

    watchlist.yaml:

        interval_days: 7
        overlap_days: 7
        retry_hours: 24
        hibernate_days: 60
        gone_active_days: 14        # suspended or deleted accounts that posted within this long: synced as usual
        gone_recheck_days: 30       # other suspended or deleted accounts: looked at on Reddit again after this long
        archive_limit_gb: 10        # a whole-archive read pauses once it has written this much for a user
        min_free_gb: 50             # no archive is read while the disk has less free
        users:
          - someone                 # downloads into the folder "someone"
          - name: someone_else
            folder: Some folder
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
        "archive_limit_gb": 10,
        "min_free_gb": 50,
    }
    # Settings from before every watched user's archive was read; warned about and ignored
    OBSOLETE = ("archive_checks",)

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
        # Whether archive reads are paused for want of disk space (warned about once)
        self.low_disk = False
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
        obsolete = [repr(key) for key in self.OBSOLETE if key in data]
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
            if "archive" in entry and "'archive' on users" not in obsolete:
                obsolete.append("'archive' on users")
            users.append({"name": name, "folder": folder})
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
                    users.append({"name": alias, "folder": folder, "alias_of": name})
        if obsolete:
            verb = "are" if len(obsolete) > 1 else "is"
            logger.warning(
                f"Watchlist: {' and '.join(obsolete)} {verb} no longer used (every watched user's archive is "
                "checked); remove them"
            )
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
            # Known removed galleries first: a few requests, before any listing is read
            for user in self.users:
                if self._lookups_due(user, self.state.get(user["name"].lower()), now):
                    state = self.state.setdefault(user["name"].lower(), {})
                    state["lookups_checked"] = now
                    self._save_state()
                    ids = self._lookup_ids(user)
                    if ids:
                        self._submit_lookups(user, ids)
                        return
            free = self._free_bytes()
            if free is not None and free < self.settings["min_free_gb"] * GB:
                if not self.low_disk:
                    logger.warning(
                        f"Only {free / GB:.0f} GB free where the folders are (under min_free_gb: "
                        f"{self.settings['min_free_gb']:g}); no archive is read until there's more"
                    )
                    self.server.notify(f"Archive reads paused: only {free / GB:.0f} GB free")
                self.low_disk = True
                return
            self.low_disk = False
            # Active users first: a hibernating user's one whole read waits behind theirs
            by_activity = sorted(self.users, key=lambda u: self._hibernating(self.state.get(u["name"].lower()), now))
            for user in by_activity:
                state = self.state.get(user["name"].lower())
                if self._checked(user, state, now) and self._due(user, state, now):
                    self._submit(user, now, free)
                    return

    def _free_bytes(self) -> Optional[float]:
        """Free space on the disk holding the folders, or None if it can't be told"""
        path = Path(self.server.root)
        while not path.exists() and path.parent != path:
            path = path.parent
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return None

    def _checked(self, user: dict, state: Optional[dict], now: float) -> bool:
        """Whether the scheduler reads this user's archive at all (now)"""
        if "alias_of" in user:
            return False  # old accounts: only which folder their posts go in
        if (state or {}).get("gone"):
            return False  # suspended or deleted on Reddit
        if self._next_read(state) == "full":
            return True  # read once, hibernating or not (a user may have been quiet when added)
        return not self._hibernating(state, now) and self._next_read(state) is not None

    @staticmethod
    def _next_read(state: Optional[dict]) -> Optional[str]:
        """What the user's next check reads: "full" (the whole archive, or the rest of it below where it stopped),
        "window" (since the previous check), or None: nothing yet, until the userscript has read the user's Reddit
        listing to its end"""
        state = state or {}
        if (
            not state.get("full_archive_done")
            and not state.get("full_archive_capped")
            and not state.get("full_archive_stopped")
            and state.get("reddit_read")
        ):
            return "full"
        return "window" if state.get("window_end") else None

    def _limit_gb(self, state: Optional[dict]) -> float:
        """The storage limit of the user's whole-archive read: their own (raised with Continue), or archive_limit_gb"""
        return (state or {}).get("archive_limit_gb") or self.settings["archive_limit_gb"]

    def _lookups_due(self, user: dict, state: Optional[dict], now: float) -> bool:
        """Whether to look for the user's removed galleries to look up by ID (see the class docstring)"""
        state = state or {}
        if "alias_of" in user or state.get("gone"):
            return False
        return now - (state.get("lookups_checked") or 0) >= self.settings["retry_hours"] * 3600

    # Removed galleries looked up per job: 10 requests
    MAX_LOOKUPS = 1000

    def _lookup_ids(self, user: dict) -> list[str]:
        ids = set(self.server.removed_galleries(self.server.root / user["folder"]))
        if getattr(self.server, "retries", None) is not None:
            ids.update(self.server.retries.galleries_without_data(user["folder"]))
        return sorted(ids)[: self.MAX_LOOKUPS]

    def _submit_lookups(self, user: dict, ids: list[str]):
        """Queues a posts-only job looking the user's removed galleries up in the archive by ID"""
        requests = -(-len(ids) // 100)
        logger.info(
            f"Looking up {len(ids)} removed galleries of u/{user['name']} in the archive by ID ({requests} "
            f"request{'s' if requests != 1 else ''})"
        )
        self._pending = user["name"].lower()
        try:
            self.server.submit(
                {"folder": user["folder"], "posts": [{"id": post_id} for post_id in ids]},
                on_finish=lambda job: self._lookups_finished(user, job),
                scheduled=True,
            )
        except ValueError as e:
            self._pending = None
            logger.warning(f"Could not queue the lookups for u/{user['name']}: {e}")

    def _lookups_finished(self, user: dict, job):
        with self._lock:
            counts = ", ".join(f"{n} {label}" for label, n in job.counts.items() if n)
            if job.state == "done":
                logger.info(f"Lookups for u/{user['name']} done: {counts or 'nothing'}")
            else:
                logger.warning(
                    f"Lookups for u/{user['name']} failed ({job.error or job.state}); tried again in "
                    f"{self.settings['retry_hours']} hours"
                )
            self._pending = None
            self._last_finished = self.clock()

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

    def _due(self, user: dict, state: Optional[dict], now: float) -> bool:
        next_check = self._next_check(user, state)
        return next_check is None or now >= next_check

    def _next_check(self, user: dict, state: Optional[dict]) -> Optional[float]:
        """When the user's next check is due; None: now"""
        if not state or not state.get("last_attempt"):
            return None
        if state.get("last_error"):
            return state["last_attempt"] + self.settings["retry_hours"] * 3600
        if self._next_read(state) == "full":
            return None
        return (state.get("last_success") or 0) + self.settings["interval_days"] * DAY

    def folder_for(self, folder: str) -> str:
        """The folder an alias's posts go in when `folder` is an alias's name (the userscript names a user page's folder
        after the user), otherwise `folder`. Lock-free: server.submit calls it while tick() holds the lock."""
        return self.alias_folders.get(folder.lower(), folder)

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
                if state.get("gone") and created > (state.get("gone_checked") or 0):
                    # Posted since the account was found gone, so it's there after all (older posts prove nothing:
                    # they may come from anywhere, e.g. posts saved before the account went)
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

    def note_reddit_read(self, name: str, oldest: Optional[float]) -> dict:
        """Records that the userscript read the user's Reddit listing to its end (oldest: its oldest post's time), so
        their whole archive may be read now"""
        with self._lock:
            self._reload()
            user = self._watched(name)
            state = self.state.setdefault(user["name"].lower(), {})
            first = not state.get("reddit_read")
            state.update(reddit_read=self.clock(), reddit_oldest=oldest)
            self._save_state()
            if first and not state.get("full_archive_done"):
                logger.info(f"u/{user['name']}'s Reddit listing was read to its end; their whole archive is read next")
            return {"user": user["name"], "reddit_read": state["reddit_read"]}

    def continue_archive(self, name: str, limit_gb: float) -> dict:
        """Raises the storage limit of the user's whole-archive read to limit_gb, so a read that stopped at the old
        limit carries on from where it got to"""
        with self._lock:
            self._reload()
            user = self._watched(name)
            state = self.state.setdefault(user["name"].lower(), {})
            if state.get("full_archive_done"):
                raise ValueError(f"u/{user['name']}'s whole archive has already been read")
            used = (state.get("archive_bytes") or 0) / GB
            if limit_gb <= used:
                raise ValueError(f"The new limit must be over the {used:.1f} GB already written for u/{user['name']}")
            state["archive_limit_gb"] = limit_gb
            capped, stopped = state.pop("full_archive_capped", None), state.pop("full_archive_stopped", None)
            if capped or stopped:
                logger.info(f"u/{user['name']}'s whole-archive read carries on, up to {limit_gb:g} GB")
            self._save_state()
            return {"user": user["name"], "archive_limit_gb": limit_gb}

    def stop_archive(self, name: str) -> dict:
        """Stops the user's whole-archive read for good (until Continue): from now on only the weekly windows are read,
        from now if they hadn't begun. A whole read already queued or running isn't stopped."""
        with self._lock:
            self._reload()
            user = self._watched(name)
            state = self.state.setdefault(user["name"].lower(), {})
            if state.get("full_archive_done"):
                raise ValueError(f"u/{user['name']}'s whole archive has already been read")
            if not state.get("full_archive_stopped"):
                state["full_archive_stopped"] = self.clock()
                state.setdefault("window_end", self.clock())
                logger.info(f"u/{user['name']}'s whole-archive read is stopped; only weekly checks from now on")
                self._save_state()
            return {"user": user["name"], "full_archive_stopped": state["full_archive_stopped"]}

    def _watched(self, name: str) -> dict:
        user = next((u for u in self.users if u["name"].lower() == name.lower() and "alias_of" not in u), None)
        if user is None:
            raise ValueError(f"u/{name} isn't on the watchlist")
        return user

    def _submit(self, user: dict, now: float, free: Optional[float] = None):
        key = user["name"].lower()
        state = self.state.setdefault(key, {})
        payload = {"folder": user["folder"], "user": user["name"]}
        room = None if free is None else int(free - self.settings["min_free_gb"] * GB)
        if self._next_read(state) == "full":
            payload["archive"] = "all"
            if state.get("full_archive_before"):
                payload["before"] = int(state["full_archive_before"])
            left = int(self._limit_gb(state) * GB - (state.get("archive_bytes") or 0))
            if left <= 0:
                state["full_archive_capped"] = now
                self._save_state()
                return
            payload["limit_bytes"] = left if room is None else max(1, min(left, room))
        else:
            payload["after"] = int(state["window_end"] - self.settings["overlap_days"] * DAY)
            if room is not None:
                payload["limit_bytes"] = max(1, room)
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
            stop_reason = getattr(job, "stop_reason", None)
            if job.full_archive:
                # Failed reads count too: their files are on disk all the same
                state["archive_bytes"] = (state.get("archive_bytes") or 0) + (getattr(job, "bytes_written", 0) or 0)
            if job.state == "done" and job.full_archive:
                if not job.before:
                    # Read from the newest post: the weekly windows start here
                    state["window_end"] = started
                elif not state.get("window_end") and state.get("full_archive_began"):
                    # Carried on from a read from the newest post that failed part way: the windows start where it began
                    state["window_end"] = state["full_archive_began"]
                if state.get("window_end"):
                    state.pop("full_archive_began", None)
                if stop_reason == "limit":
                    oldest = getattr(job, "oldest_post", None)
                    if oldest:
                        # Arctic Shift's before is exclusive; posts sharing that second are already downloaded
                        state["full_archive_before"] = int(oldest) + 1
                    if state["archive_bytes"] < self._limit_gb(state) * GB:
                        # Stopped by the room left above min_free_gb: carries on by itself once there's more
                        logger.warning(f"Whole archive of u/{user['name']} stopped: the disk is nearly full ({counts})")
                    else:
                        state["full_archive_capped"] = self.clock()
                        message = (
                            f"u/{user['name']}'s whole-archive read stopped at its storage limit "
                            f"({state['archive_bytes'] / GB:.1f} of {self._limit_gb(state):g} GB); raise it in the "
                            "dashboard to carry on"
                        )
                        logger.info(f"{message}: {counts}")
                        self.server.notify(message)
                else:
                    state["full_archive_done"] = self.clock()
                    state.pop("full_archive_before", None)
                    why = "its media was gone further back" if stop_reason == "dead" else "read to its start"
                    logger.info(f"Whole archive of u/{user['name']} done ({why}): {counts}")
                state.update(last_success=self.clock(), last_error=None)
            elif job.state == "done" and stop_reason == "limit":
                # The window wasn't read to its end, so it's read again once there's room
                state["last_error"] = "stopped: the disk is nearly full"
                logger.warning(f"Baseline for u/{user['name']} stopped: the disk is nearly full ({counts})")
            elif job.state == "done":
                state.update(window_end=started, last_success=self.clock(), last_error=None)
                logger.info(f"Baseline for u/{user['name']} done: {counts}")
            elif job.full_archive and getattr(job, "oldest_post", None):
                # Carried on below the oldest post it reached rather than read again from the top, so its listing pages
                # aren't asked for again. Posts that failed above it aren't tried again: fewer requests win.
                state["last_error"] = job.error or f"job {job.state}"
                if not job.before:
                    state.setdefault("full_archive_began", started)
                state["full_archive_before"] = int(job.oldest_post) + 1
                failed = job.counts.get("failed", 0)
                skipped = f"; {failed} posts that failed above it aren't read again" if failed else ""
                logger.warning(
                    f"Whole archive of u/{user['name']} failed ({state['last_error']}); it carries on below "
                    f"{time.strftime('%Y-%m-%d', time.gmtime(job.oldest_post))} in {self.settings['retry_hours']} "
                    f"hours{skipped}"
                )
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
                    gone.append(
                        {"name": user["name"], "folder": user["folder"], "gone": state["gone"], "recheck": recheck}
                    )
                    continue
                checked = self._checked(user, state, now)
                users.append(
                    {
                        "name": user["name"],
                        "folder": user["folder"],
                        "full_archive_done": state.get("full_archive_done"),
                        # The whole-archive read stopped at the storage limit (written so far, of the limit)
                        "full_archive_capped": state.get("full_archive_capped"),
                        # Stopped for good (the dashboard's Stop): only weekly windows, until Continue
                        "full_archive_stopped": state.get("full_archive_stopped"),
                        "archive_bytes": state.get("archive_bytes") or 0,
                        "archive_limit_gb": self._limit_gb(state),
                        # The userscript read the user's Reddit listing to its end; until then (and when the whole
                        # archive still has to be read), its sync reads to the end rather than until known posts
                        "reddit_read": state.get("reddit_read"),
                        "read_reddit_to_end": "alias_of" not in user
                        and not state.get("reddit_read")
                        and not state.get("full_archive_done")
                        and not state.get("full_archive_stopped"),
                        "last_success": state.get("last_success"),
                        "last_error": state.get("last_error"),
                        # Whether the archive is read at all now (not for aliases or gone users, nor for hibernating
                        # ones once their whole archive was read, nor before the userscript has read Reddit to the end
                        # unless weekly windows have begun)
                        "checked": checked,
                        # None: due now, or not checked
                        "next_check": self._next_check(user, state) if checked else None,
                        "newest_post": state.get("newest_post"),
                        "hibernating": self._hibernating(state, now),
                        **({"alias_of": user["alias_of"]} if "alias_of" in user else {}),
                        # Due for another look on Reddit: the userscript reports what it finds
                        **({"gone": state["gone"]} if state.get("gone") else {}),
                    }
                )
            # Suspended or deleted users, left out of "users" until their recheck time
            free = self._free_bytes()
            return {
                "enabled": True,
                "users": users,
                "gone": gone,
                "error": self.error,
                "free_bytes": free,
                "low_disk": free is not None and free < self.settings["min_free_gb"] * GB,
                **self.settings,
            }
