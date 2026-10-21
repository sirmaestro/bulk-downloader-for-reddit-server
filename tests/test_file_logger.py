#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import configparser
import logging
import sys
from pathlib import Path

from bdfr import logs
from bdfr.configuration import Configuration
from bdfr.downloader import Downloader


def make_connector(log_path: Path) -> Downloader:
    connector = Downloader.__new__(Downloader)
    connector.args = Configuration()
    connector.args.log = str(log_path)
    connector.cfg_parser = configparser.ConfigParser()
    connector.config_directory = log_path.parent
    return connector


def record(message: str, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord("bdfr.downloader", level, __file__, 0, message, None, None)


def write(handler: logging.Handler, message: str):
    handler.handle(record(message))
    handler.close()


def test_each_run_starts_a_new_log_by_default(tmp_path: Path):
    log_path = tmp_path / "log.txt"
    write(make_connector(log_path).create_file_logger(), "first run")
    write(make_connector(log_path).create_file_logger(), "second run")
    assert "second run" in log_path.read_text(encoding="utf-8")
    assert "first run" not in log_path.read_text(encoding="utf-8")
    assert "first run" in (tmp_path / "log.txt.1").read_text(encoding="utf-8")


def test_serve_log_keeps_earlier_runs(tmp_path: Path):
    """Restarting serve appends to serve_log.txt; it only rotates when full"""
    log_path = tmp_path / "serve_log.txt"
    write(logs.serve_log_handler(log_path, 10_000, 3, 0), "first start")
    write(logs.serve_log_handler(log_path, 10_000, 3, 0), "second start")
    text = log_path.read_text(encoding="utf-8")
    assert "first start" in text and "second start" in text
    assert not (tmp_path / "serve_log.txt.1").exists()


def test_serve_log_rotates_when_full(tmp_path: Path):
    log_path = tmp_path / "serve_log.txt"
    handler = logs.serve_log_handler(log_path, 500, 3, 0)
    for number in range(20):
        handler.handle(record(f"line {number} " + "x" * 40))
    handler.close()
    assert (tmp_path / "serve_log.txt.1").exists()
    assert log_path.stat().st_size <= 500


def test_lines_name_their_job_and_post(tmp_path: Path):
    log_path = tmp_path / "serve_log.txt"
    handler = logs.serve_log_handler(log_path, 10_000, 3, 0)
    handler.handle(record("Listening"))
    with logs.job_context("3fd9e1485ded"):
        handler.handle(record("Started"))
        with logs.post_context("1xbgyxi"):
            handler.handle(record("Wrote a.jpg", logging.DEBUG))
    handler.close()
    lines = [line[20:] for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert lines == [
        "INFO    Listening",
        "INFO    job 3fd9e1485ded: Started",
        "DEBUG   job 3fd9e1485ded, post 1xbgyxi: Wrote a.jpg",
    ]


def test_trace_lines_only_with_vv(tmp_path: Path):
    for verbosity, level in ((0, logging.DEBUG), (1, logging.DEBUG), (2, logs.TRACE)):
        handler = logs.serve_log_handler(tmp_path / "serve_log.txt", 10_000, 3, verbosity)
        handler.close()
        assert handler.level == level


def test_console_leaves_out_tracebacks(capsys):
    handler = logs.console_handler(0)
    try:
        raise ValueError("boom")
    except ValueError:
        error = logging.LogRecord("bdfr", logging.ERROR, __file__, 0, "Unexpected error: boom", None, sys.exc_info())
    handler.handle(error)
    out = capsys.readouterr().out
    assert "ERROR   Unexpected error: boom" in out and "Traceback" not in out
