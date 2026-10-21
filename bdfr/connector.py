#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import configparser
import importlib.resources
import itertools
import logging
import logging.handlers
import re
import shutil
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Optional

import platformdirs

from bdfr import exceptions as errors
from bdfr.arctic_shift import ArcticShift, read_post_json, submission_id_from_link
from bdfr.configuration import Configuration
from bdfr.download_filter import DownloadFilter
from bdfr.file_name_formatter import FileNameFormatter
from bdfr.hash_store import HashStore

logger = logging.getLogger(__name__)

# How far back --time reaches; listings are always newest first
TIME_WINDOWS = {"hour": 3600, "day": 86400, "week": 604800, "month": 2592000, "year": 31536000}


class Connector(HashStore):
    """Loads the configuration, sets up logging and the download folder's hash store, and builds `self.listings`: lazy
    generators of posts, one per source (given posts first, then each subreddit and user listing)"""

    def __init__(self, args: Configuration, logging_handlers: Iterable[logging.Handler] = ()):
        self.args = args
        self.config_directories = platformdirs.PlatformDirs("bdfr", "BDFR")
        self.determine_directories()
        self.load_config()
        self.read_config()
        file_log = self.create_file_logger()
        self._apply_logging_handlers(itertools.chain(logging_handlers, [file_log]))
        self.run_time = datetime.now().isoformat()
        self._setup_internal_objects()

        self.listings = self.build_listings()
        self._load_hash_store()

    def _setup_internal_objects(self):
        self.parse_disabled_modules()

        self.download_filter = self.create_download_filter()
        logger.log(9, "Created download filter")
        self.file_name_formatter = self.create_file_name_formatter()
        logger.log(9, "Create file name formatter")

        self.arctic_shift = ArcticShift()
        self.args.user = [user for user in self.args.user if user]
        self.exported_posts = read_post_json(self.args.post_json) if self.args.post_json else []

        self.excluded_submission_ids = set.union(
            set(self.read_id_files(self.args.exclude_id_file)),
            set(self.args.exclude_id),
        )

        self.args.link = list(itertools.chain(self.args.link, self.read_id_files(self.args.include_id_file)))

        self._open_hash_store()

        self.args.skip_subreddit = self.split_args_input(self.args.skip_subreddit)
        self.args.skip_subreddit = {sub.lower() for sub in self.args.skip_subreddit}

    @staticmethod
    def _apply_logging_handlers(handlers: Iterable[logging.Handler]):
        main_logger = logging.getLogger()
        for handler in handlers:
            main_logger.addHandler(handler)

    def read_config(self):
        """Read any cfg values that need to be processed"""
        if self.args.max_wait_time is None:
            self.args.max_wait_time = self.cfg_parser.getint("DEFAULT", "max_wait_time", fallback=120)
            logger.debug(f"Setting maximum download wait time to {self.args.max_wait_time} seconds")
        if self.args.time_format is None:
            option = self.cfg_parser.get("DEFAULT", "time_format", fallback="ISO")
            if re.match(r"^[\s\'\"]*$", option):
                option = "ISO"
            logger.debug(f"Setting datetime format string to {option}")
            self.args.time_format = option
        if not self.args.disable_module:
            self.args.disable_module = [self.cfg_parser.get("DEFAULT", "disabled_modules", fallback="")]
        if not self.args.filename_restriction_scheme:
            self.args.filename_restriction_scheme = self.cfg_parser.get(
                "DEFAULT", "filename_restriction_scheme", fallback=None
            )
            logger.debug(f"Setting filename restriction scheme to '{self.args.filename_restriction_scheme}'")

    def parse_disabled_modules(self):
        disabled_modules = self.args.disable_module
        disabled_modules = self.split_args_input(disabled_modules)
        disabled_modules = {name.strip().lower() for name in disabled_modules}
        self.args.disable_module = disabled_modules
        if self.args.disable_module:
            logger.debug(f'Disabling the following modules: {", ".join(self.args.disable_module)}')

    def build_listings(self) -> list[Iterator]:
        after = None
        if (self.args.time or "all") in TIME_WINDOWS:
            after = int(datetime.now().timestamp()) - TIME_WINDOWS[self.args.time]

        out = []
        for subreddit in self.split_args_input(self.args.subreddit):
            logger.debug(f"Added submissions from subreddit {subreddit} via Arctic Shift")
            out.append(self.arctic_shift.submissions(subreddit=subreddit, limit=self.args.limit, after=after))
        if self.args.submitted:
            if not self.args.user:
                logger.warning("At least one user must be supplied to download user data")
            user_after = max(filter(None, (after, getattr(self.args, "listing_after", None))), default=None)
            for user in self.args.user:
                logger.debug(f"Retrieving submitted posts of user {user} via Arctic Shift")
                out.append(
                    self.arctic_shift.submissions(
                        author=user, limit=self.args.limit, after=user_after, before=self.args.listing_before
                    )
                )

        posts = {post["id"]: post for post in self.exported_posts}
        for link in self.args.link:
            if submission_id := submission_id_from_link(link):
                posts.setdefault(submission_id, {"id": submission_id})
            else:
                logger.error(f"Could not find a submission ID in {link}")
        if posts:
            filter_on_ratio = self.args.min_score_ratio or self.args.max_score_ratio

            def needs_lookup(post: dict) -> Optional[str]:
                # Exported posts carry no upvote ratio, so the archive is needed to filter on it
                if filter_on_ratio and "upvote_ratio" not in post:
                    return "upvote ratio needed for --min/--max-score-ratio"
                return ArcticShift.needs_lookup(post)

            # Given posts go first: they are what was asked for, and may be newer than the archive's listings
            self.post_list = self.arctic_shift.submissions_from_posts(
                posts.values(), needs_lookup, lookups=getattr(self.args, "archive_lookups", True)
            )
            out.insert(0, self.post_list)
        return out

    def determine_directories(self):
        self.download_directory = Path(self.args.directory).resolve().expanduser()
        self.config_directory = Path(self.config_directories.user_config_dir)

        self.download_directory.mkdir(exist_ok=True, parents=True)
        self.config_directory.mkdir(exist_ok=True, parents=True)

    def load_config(self):
        self.cfg_parser = configparser.ConfigParser()
        if self.args.config:
            if not (cfg_path := Path(self.args.config)).exists():
                raise errors.BulkDownloaderException(f"Config file {cfg_path} does not exist")
            self.cfg_parser.read(cfg_path)
            self.config_location = cfg_path
            return
        possible_paths = [
            Path("./config.cfg"),
            Path("./default_config.cfg"),
            Path(self.config_directory, "config.cfg"),
            Path(self.config_directory, "default_config.cfg"),
        ]
        self.config_location = None
        for path in possible_paths:
            if path.resolve().expanduser().exists():
                self.config_location = path
                logger.debug(f"Loading configuration from {path}")
                break
        if not self.config_location:
            # Use a copy in the user's config directory so the packaged file is never written to
            self.config_location = Path(self.config_directory, "default_config.cfg")
            with importlib.resources.as_file(importlib.resources.files("bdfr") / "default_config.cfg") as path:
                shutil.copy(path, self.config_location)
            logger.debug(f"Created configuration at {self.config_location}")
        self.cfg_parser.read(self.config_location)

    def create_file_logger(self) -> logging.handlers.RotatingFileHandler:
        if self.args.log is None:
            log_path = Path(self.config_directory, "log_output.txt")
        else:
            log_path = Path(self.args.log).resolve().expanduser()
            if not log_path.parent.exists():
                raise errors.BulkDownloaderException("Designated location for logfile does not exist")
        backup_count = self.cfg_parser.getint("DEFAULT", "backup_log_count", fallback=3)
        rotate_bytes = getattr(self.args, "log_rotate_bytes", 0) or 0
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, mode="a", maxBytes=rotate_bytes, backupCount=backup_count, encoding="utf-8"
        )
        if log_path.exists() and not rotate_bytes:
            try:
                file_handler.doRollover()
            except PermissionError:
                logger.critical(
                    "Cannot rollover logfile, make sure this is the only "
                    "BDFR process or specify alternate logfile location"
                )
                raise
        formatter = logging.Formatter("[%(asctime)s - %(name)s - %(levelname)s] - %(message)s")
        file_handler.setFormatter(formatter)
        # The file always gets debug lines; the finer level 9 tracing (filters, file name indexes, request URLs)
        # only with -vv, as on the console
        file_handler.setLevel(0 if (self.args.verbose or 0) >= 2 else logging.DEBUG)
        return file_handler

    @staticmethod
    def sanitise_subreddit_name(subreddit: str) -> str:
        pattern = re.compile(r"^(?:https://www\.reddit\.com/)?(?:r/)?(.*?)/?$")
        match = re.match(pattern, subreddit)
        if not match:
            raise errors.BulkDownloaderException(f"Could not find subreddit name in string {subreddit}")
        return match.group(1)

    @staticmethod
    def split_args_input(entries: list[str]) -> set[str]:
        all_entries = []
        split_pattern = re.compile(r"[,;]\s?")
        for entry in entries:
            results = re.split(split_pattern, entry)
            all_entries.extend([Connector.sanitise_subreddit_name(name) for name in results])
        return set(all_entries)

    def create_file_name_formatter(self) -> FileNameFormatter:
        return FileNameFormatter(
            self.args.file_scheme,
            self.args.folder_scheme,
            self.args.time_format,
            self.args.filename_restriction_scheme,
            self.args.filename_character_set,
        )

    def create_download_filter(self) -> DownloadFilter:
        return DownloadFilter(self.args.skip, self.args.skip_domain)

    @staticmethod
    def read_id_files(file_locations: list[str]) -> list[str]:
        out = {}
        for id_file in file_locations:
            id_file = Path(id_file).resolve().expanduser()
            if not id_file.exists():
                logger.warning(f"ID file at {id_file} does not exist")
                continue
            with id_file.open("r") as file:
                for line in file:
                    out[line.strip()] = None
        return out.keys()
