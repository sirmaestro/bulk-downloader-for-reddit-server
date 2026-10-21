#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""The log format shared by the console and the log files.

Lines read `2026-10-21 09:09:15 INFO    job 3fd9e1485ded, post 1xbgyxi: new - 1 file written (#1, r/pics, Direct)`. The
job and post come from context variables set around the work (`job_context`, `post_context`), so lines from posts
downloading in parallel threads, and from the scheduler between jobs, say what they belong to without repeating it in
every message. Threads started for a post must run in a copy of the starting thread's context
(`contextvars.copy_context().run`)."""

import contextlib
import contextvars
import logging
import logging.handlers
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Optional

# Below DEBUG: request URLs, file name indexes, hashes; shown with -vv
TRACE = 9
logging.addLevelName(TRACE, "TRACE")

FORMAT = "%(asctime)s %(levelname)-7s %(context)s%(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_job = contextvars.ContextVar("bdfr_job", default=None)
_post = contextvars.ContextVar("bdfr_post", default=None)


@contextlib.contextmanager
def job_context(job_id: str) -> Iterator[None]:
    token = _job.set(job_id)
    try:
        yield
    finally:
        _job.reset(token)


@contextlib.contextmanager
def post_context(post_id: Optional[str]) -> Iterator[None]:
    token = _post.set(post_id)
    try:
        yield
    finally:
        _post.reset(token)


class ContextFilter(logging.Filter):
    """Adds `context` ("job 3fd9e1485ded, post 1xbgyxi: ", or "") to every record a handler writes"""

    def filter(self, record: logging.LogRecord) -> bool:
        parts = []
        if job := _job.get():
            parts.append(f"job {job}")
        if post := _post.get():
            parts.append(f"post {post}")
        record.context = f"{', '.join(parts)}: " if parts else ""
        return True


class _NoTracebackFormatter(logging.Formatter):
    """The console's: errors without their tracebacks (those go to the log file)"""

    def format(self, record: logging.LogRecord) -> str:
        saved = record.exc_info, record.exc_text
        record.exc_info, record.exc_text = None, None
        try:
            return super().format(record)
        finally:
            record.exc_info, record.exc_text = saved


def prepare(handler: logging.Handler, level: int, tracebacks: bool = True) -> logging.Handler:
    handler.setFormatter((logging.Formatter if tracebacks else _NoTracebackFormatter)(FORMAT, DATE_FORMAT))
    handler.addFilter(ContextFilter())
    handler.setLevel(level)
    return handler


def console_handler(verbosity: int) -> logging.Handler:
    """Info and up; debug lines with -v, trace lines and tracebacks with -vv"""
    logging.getLogger().setLevel(1)
    level = logging.INFO if verbosity <= 0 else logging.DEBUG if verbosity == 1 else TRACE
    return prepare(logging.StreamHandler(sys.stdout), level, tracebacks=verbosity >= 2)


def file_level(verbosity: int) -> int:
    """Log files always get debug lines, and trace lines with -vv"""
    return TRACE if verbosity >= 2 else logging.DEBUG


def serve_log_handler(path: Path, max_bytes: int, backup_count: int, verbosity: int) -> logging.Handler:
    """serve_log.txt: one file for everything the server does, rotated by size rather than per job"""
    handler = logging.handlers.RotatingFileHandler(
        path, mode="a", maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    return prepare(handler, file_level(verbosity))
