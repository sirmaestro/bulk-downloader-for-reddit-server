#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import configparser
import logging
from pathlib import Path

from bdfr.configuration import Configuration
from bdfr.downloader import RedditDownloader


def make_connector(log_path: Path, rotate_bytes: int = 0) -> RedditDownloader:
    connector = RedditDownloader.__new__(RedditDownloader)
    connector.args = Configuration()
    connector.args.log = str(log_path)
    connector.args.log_rotate_bytes = rotate_bytes
    connector.cfg_parser = configparser.ConfigParser()
    connector.config_directory = log_path.parent
    return connector


def write(handler: logging.Handler, message: str):
    handler.emit(logging.LogRecord("bdfr", logging.INFO, __file__, 0, message, None, None))
    handler.close()


def test_each_run_starts_a_new_log_by_default(tmp_path: Path):
    log_path = tmp_path / "log.txt"
    write(make_connector(log_path).create_file_logger(), "first run")
    write(make_connector(log_path).create_file_logger(), "second run")
    assert "second run" in log_path.read_text(encoding="utf-8")
    assert "first run" not in log_path.read_text(encoding="utf-8")
    assert "first run" in (tmp_path / "log.txt.1").read_text(encoding="utf-8")


def test_size_rotated_log_keeps_earlier_runs(tmp_path: Path):
    """bdfr serve runs a job per downloader; rolling over for each one kept only the last few jobs' logs"""
    log_path = tmp_path / "serve_log.txt"
    write(make_connector(log_path, 10_000).create_file_logger(), "first job")
    write(make_connector(log_path, 10_000).create_file_logger(), "second job")
    text = log_path.read_text(encoding="utf-8")
    assert "first job" in text and "second job" in text
    assert not (tmp_path / "serve_log.txt.1").exists()


def test_size_rotated_log_rotates_when_full(tmp_path: Path):
    log_path = tmp_path / "serve_log.txt"
    handler = make_connector(log_path, 500).create_file_logger()
    for number in range(20):
        handler.emit(logging.LogRecord("bdfr", logging.INFO, __file__, 0, f"line {number} " + "x" * 40, None, None))
    handler.close()
    assert (tmp_path / "serve_log.txt.1").exists()
    assert log_path.stat().st_size <= 500
