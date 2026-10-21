#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bdfr.configuration import Configuration
from bdfr.connector import Connector
from bdfr.download_filter import DownloadFilter
from bdfr.exceptions import BulkDownloaderException
from bdfr.file_name_formatter import FileNameFormatter


@pytest.fixture()
def args() -> Configuration:
    args = Configuration()
    args.time_format = "ISO"
    return args


@pytest.fixture()
def downloader_mock(args: Configuration):
    downloader_mock = MagicMock()
    downloader_mock.args = args
    downloader_mock.sanitise_subreddit_name = Connector.sanitise_subreddit_name
    downloader_mock.split_args_input = Connector.split_args_input
    downloader_mock.master_hash_list = {}
    return downloader_mock


def test_determine_directories(tmp_path: Path, downloader_mock: MagicMock):
    downloader_mock.args.directory = tmp_path / "test"
    downloader_mock.config_directories.user_config_dir = tmp_path
    Connector.determine_directories(downloader_mock)
    assert Path(tmp_path / "test").exists()


@pytest.mark.parametrize(
    ("skip_extensions", "skip_domains"),
    (
        ([], []),
        (
            [".test"],
            ["test.com"],
        ),
    ),
)
def test_create_download_filter(skip_extensions: list[str], skip_domains: list[str], downloader_mock: MagicMock):
    downloader_mock.args.skip = skip_extensions
    downloader_mock.args.skip_domain = skip_domains
    result = Connector.create_download_filter(downloader_mock)

    assert isinstance(result, DownloadFilter)
    assert result.excluded_domains == skip_domains
    assert result.excluded_extensions == skip_extensions


@pytest.mark.parametrize(
    ("test_time", "expected_window"),
    (
        ("all", None),
        ("hour", 3600),
        ("day", 86400),
        ("week", 604800),
        ("random", None),
        ("", None),
    ),
)
def test_build_listings_time_window(test_time: str, expected_window, downloader_mock: MagicMock):
    downloader_mock.args.time = test_time
    downloader_mock.args.subreddit = ["test"]
    downloader_mock.exported_posts = []
    Connector.build_listings(downloader_mock)
    after = downloader_mock.arctic_shift.submissions.call_args.kwargs["after"]
    if expected_window is None:
        assert after is None
    else:
        assert abs(time.time() - expected_window - after) < 5


@pytest.mark.parametrize(
    ("test_file_scheme", "test_folder_scheme"),
    (
        ("{POSTID}", "{SUBREDDIT}"),
        ("{REDDITOR}_{TITLE}_{POSTID}", "{SUBREDDIT}"),
        ("{POSTID}", "test"),
        ("{POSTID}", ""),
        ("{POSTID}", "{SUBREDDIT}/{REDDITOR}"),
    ),
)
def test_create_file_name_formatter(test_file_scheme: str, test_folder_scheme: str, downloader_mock: MagicMock):
    downloader_mock.args.file_scheme = test_file_scheme
    downloader_mock.args.folder_scheme = test_folder_scheme
    result = Connector.create_file_name_formatter(downloader_mock)

    assert isinstance(result, FileNameFormatter)
    assert result.file_format_string == test_file_scheme
    assert result.directory_format_string == test_folder_scheme.split("/")


@pytest.mark.parametrize(
    ("test_file_scheme", "test_folder_scheme"),
    (
        ("", ""),
        ("", "{SUBREDDIT}"),
        ("test", "{SUBREDDIT}"),
    ),
)
def test_create_file_name_formatter_bad(test_file_scheme: str, test_folder_scheme: str, downloader_mock: MagicMock):
    downloader_mock.args.file_scheme = test_file_scheme
    downloader_mock.args.folder_scheme = test_folder_scheme
    with pytest.raises(BulkDownloaderException):
        Connector.create_file_name_formatter(downloader_mock)


@pytest.mark.parametrize(
    ("test_name", "expected"),
    (
        ("Mindustry", "Mindustry"),
        ("Futurology", "Futurology"),
        ("r/Mindustry", "Mindustry"),
        ("TrollXChromosomes", "TrollXChromosomes"),
        ("r/TrollXChromosomes", "TrollXChromosomes"),
        ("https://www.reddit.com/r/TrollXChromosomes/", "TrollXChromosomes"),
        ("https://www.reddit.com/r/TrollXChromosomes", "TrollXChromosomes"),
        ("https://www.reddit.com/r/Futurology/", "Futurology"),
        ("https://www.reddit.com/r/Futurology", "Futurology"),
    ),
)
def test_sanitise_subreddit_name(test_name: str, expected: str):
    result = Connector.sanitise_subreddit_name(test_name)
    assert result == expected


@pytest.mark.parametrize(
    ("test_subreddit_entries", "expected"),
    (
        (["test1", "test2", "test3"], {"test1", "test2", "test3"}),
        (["test1,test2", "test3"], {"test1", "test2", "test3"}),
        (["test1, test2", "test3"], {"test1", "test2", "test3"}),
        (["test1; test2", "test3"], {"test1", "test2", "test3"}),
        (["test1, test2", "test1,test2,test3", "test4"], {"test1", "test2", "test3", "test4"}),
        ([""], {""}),
        (["test"], {"test"}),
    ),
)
def test_split_subreddit_entries(test_subreddit_entries: list[str], expected: set[str]):
    results = Connector.split_args_input(test_subreddit_entries)
    assert results == expected


def test_read_submission_ids_from_file(downloader_mock: MagicMock, tmp_path: Path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("aaaaaa\nbbbbbb")
    results = Connector.read_id_files([str(test_file)])
    assert results == {"aaaaaa", "bbbbbb"}
