#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import platform
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Union
from unittest.mock import MagicMock

import pytest

from bdfr.file_name_formatter import FileNameFormatter
from bdfr.post import Post
from bdfr.resource import Resource


@pytest.fixture()
def submission() -> MagicMock:
    test = MagicMock()
    test.title = "name"
    test.subreddit = "randomreddit"
    test.author = "person"
    test.id = "12345"
    test.score = 1000
    test.link_flair_text = "test_flair"
    test.created_utc = datetime(2021, 4, 21, 9, 30, 0).timestamp()
    test.__class__ = Post
    return test


@pytest.fixture()
def test_formatter() -> FileNameFormatter:
    out = FileNameFormatter("{TITLE}", "", "ISO")
    return out


def check_valid_windows_path(test_string: str):
    return test_string == FileNameFormatter._format_for_windows(test_string)


def do_test_string_equality(result: Union[Path, str], expected: str) -> bool:
    if platform.system() == "Windows":
        expected = FileNameFormatter._format_for_windows(expected)
    return str(result).endswith(expected)


def do_test_path_equality(result: Path, expected: str) -> bool:
    if platform.system() == "Windows":
        expected = expected.split("/")
        expected = [FileNameFormatter._format_for_windows(part) for part in expected]
        expected = Path(*expected)
    else:
        expected = Path(expected)
    return str(result).endswith(str(expected))  # noqa: FURB123


@pytest.mark.parametrize(
    ("test_format_string", "expected"),
    (
        ("{SUBREDDIT}", "randomreddit"),
        ("{REDDITOR}", "person"),
        ("{POSTID}", "12345"),
        ("{UPVOTES}", "1000"),
        ("{FLAIR}", "test_flair"),
        ("{DATE}", "2021-04-21T09:30:00"),
        ("{REDDITOR}_{TITLE}_{POSTID}", "person_name_12345"),
    ),
)
def test_format_name_mock(test_format_string: str, expected: str, submission: MagicMock):
    test_formatter = FileNameFormatter(test_format_string, "", "ISO")
    result = test_formatter._format_name(submission, test_format_string)
    assert do_test_string_equality(result, expected)


@pytest.mark.parametrize(
    ("test_string", "expected"),
    (
        ("", False),
        ("test", False),
        ("{POSTID}", True),
        ("POSTID", False),
        ("{POSTID}_test", True),
        ("test_{TITLE}", True),
        ("TITLE_POSTID", False),
    ),
)
def test_check_format_string_validity(test_string: str, expected: bool):
    result = FileNameFormatter.validate_string(test_string)
    assert result == expected


def test_format_multiple_resources():
    mocks = []
    for _i in range(1, 5):
        new_mock = MagicMock()
        new_mock.url = "https://example.com/test.png"
        new_mock.extension = ".png"
        new_mock.source_submission.title = "test"
        new_mock.source_submission.__class__ = Post
        mocks.append(new_mock)
    test_formatter = FileNameFormatter("{TITLE}", "", "ISO")
    results = test_formatter.format_resource_paths(mocks, Path())
    results = set([str(res[0].name) for res in results])
    expected = {"test_1.png", "test_2.png", "test_3.png", "test_4.png"}
    assert results == expected


@pytest.mark.parametrize(
    ("test_filename", "test_ending"),
    (
        ("A" * 300, ".png"),
        ("A" * 300, "_1.png"),
        ("a" * 300, "_1000.jpeg"),
        ("😍💕✨" * 100, "_1.png"),
    ),
)
def test_limit_filename_length(test_filename: str, test_ending: str, test_formatter: FileNameFormatter):
    result = test_formatter.limit_file_name_length(test_filename, test_ending, Path())
    assert len(result.name) <= 255
    assert len(result.name.encode("utf-8")) <= 255
    assert len(str(result)) <= test_formatter.max_path
    assert isinstance(result, Path)


@pytest.mark.parametrize(
    ("test_filename", "test_ending", "expected_end"),
    (
        ("test_aaaaaa", "_1.png", "test_aaaaaa_1.png"),
        ("test_aataaa", "_1.png", "test_aataaa_1.png"),
        ("test_abcdef", "_1.png", "test_abcdef_1.png"),
        ("test_aaaaaa", ".png", "test_aaaaaa.png"),
        ("test", "_1.png", "test_1.png"),
        ("test_m1hqw6", "_1.png", "test_m1hqw6_1.png"),
        ("A" * 300 + "_bbbccc", ".png", "_bbbccc.png"),
        ("A" * 300 + "_bbbccc", "_1000.jpeg", "_bbbccc_1000.jpeg"),
        ("😍💕✨" * 100 + "_aaa1aa", "_1.png", "_aaa1aa_1.png"),
    ),
)
def test_preserve_id_append_when_shortening(
    test_filename: str, test_ending: str, expected_end: str, test_formatter: FileNameFormatter
):
    result = test_formatter.limit_file_name_length(test_filename, test_ending, Path())
    assert len(result.name) <= 255
    assert len(result.name.encode("utf-8")) <= 255
    assert result.name.endswith(expected_end)
    assert len(str(result)) <= test_formatter.max_path


@pytest.mark.parametrize(("index", "expected_end"), ((None, "_abc1234.png"), (2, "_abc1234_2.png")))
def test_shortening_keeps_seven_character_post_id(
    submission: MagicMock, tmp_path: Path, index: Optional[int], expected_end: str
):
    submission.title = "A" * 300
    submission.id = "abc1234"
    resource = Resource(submission, "https://example.com/x.png", lambda: None, ".png")
    formatter = FileNameFormatter("{REDDITOR}_{TITLE}_{POSTID}", "", "ISO")
    result = formatter.format_path(resource, tmp_path, index)
    assert result.name.endswith(expected_end)
    assert len(result.name.encode("utf-8")) <= 255


def test_shortening_reuses_existing_name_that_lost_the_post_id(submission: MagicMock, tmp_path: Path):
    """Files shortened before 7-character IDs were kept stay recognised by their path"""
    submission.title = "A" * 300
    submission.id = "abc1234"
    resource = Resource(submission, "https://example.com/x.png", lambda: None, ".png")
    formatter = FileNameFormatter("{REDDITOR}_{TITLE}_{POSTID}", "", "ISO")
    legacy = formatter.limit_file_name_length("person_" + "A" * 300 + "_abc1234", ".png", tmp_path)
    assert "abc1234" not in legacy.name
    legacy.touch()
    assert formatter.format_path(resource, tmp_path) == legacy


@pytest.mark.skipif(sys.platform == "win32", reason="Test broken on windows github")
def test_shorten_filename_real(submission: MagicMock, tmp_path: Path):
    submission.title = "A" * 500
    submission.author = "test"
    submission.subreddit = "test"
    submission.id = "BBBBBB"
    test_resource = Resource(submission, "www.example.com/empty", lambda: None, ".jpeg")
    test_formatter = FileNameFormatter("{REDDITOR}_{TITLE}_{POSTID}", "{SUBREDDIT}", "ISO")
    result = test_formatter.format_path(test_resource, tmp_path)
    result.parent.mkdir(parents=True)
    result.touch()


@pytest.mark.parametrize(
    ("test_name", "test_ending"),
    (
        ("a", "b"),
        ("a", "_bbbbbb.jpg"),
        ("a" * 20, "_bbbbbb.jpg"),
        ("a" * 50, "_bbbbbb.jpg"),
        ("a" * 500, "_bbbbbb.jpg"),
    ),
)
def test_shorten_path(test_name: str, test_ending: str, tmp_path: Path, test_formatter: FileNameFormatter):
    result = test_formatter.limit_file_name_length(test_name, test_ending, tmp_path)
    assert len(str(result.name)) <= 255
    assert len(str(result.name).encode("UTF-8")) <= 255
    assert len(str(result.name).encode("cp1252")) <= 255
    assert len(str(result)) <= test_formatter.max_path


@pytest.mark.parametrize(
    ("test_string", "expected"),
    (
        ("test", "test"),
        ("test😍", "test"),
        ("test.png", "test.png"),
        ("test*", "test"),
        ("test**", "test"),
        ("test?*", "test"),
        ("test_???.png", "test_.png"),
        ("test_???😍.png", "test_.png"),
    ),
)
def test_format_file_name_for_windows(test_string: str, expected: str):
    result = FileNameFormatter._format_for_windows(test_string)
    assert result == expected


@pytest.mark.parametrize(
    ("test_string", "expected"),
    (
        ("test", "test"),
        ("test😍", "test"),
        ("😍", ""),
    ),
)
def test_strip_emojies(test_string: str, expected: str):
    result = FileNameFormatter._strip_emojis(test_string)
    assert result == expected


@pytest.mark.parametrize(
    ("test_folder_scheme", "expected"),
    (
        ("{REDDITOR}/{SUBREDDIT}", "person/randomreddit"),
        ("{POSTID}/{SUBREDDIT}/{REDDITOR}", "12345/randomreddit/person"),
    ),
)
def test_multilevel_folder_scheme(
    test_folder_scheme: str,
    expected: str,
    tmp_path: Path,
    submission: MagicMock,
):
    test_formatter = FileNameFormatter("{POSTID}", test_folder_scheme, "ISO")
    test_resource = MagicMock()
    test_resource.source_submission = submission
    test_resource.extension = ".png"
    result = test_formatter.format_path(test_resource, tmp_path)
    result = result.relative_to(tmp_path)
    assert do_test_path_equality(result.parent, expected)
    assert len(result.parents) == (len(expected.split("/")) + 1)


@pytest.mark.parametrize(
    ("test_name_string", "expected"),
    (
        ("test", "test"),
        ("😍", "😍"),
        ("test😍", "test😍"),
        ("test😍 ’", "test😍 ’"),
        ("test😍 \\u2019", "test😍 ’"),
        ("Using that real good [1\\4]", "Using that real good [1\\4]"),
    ),
)
def test_preserve_emojis(test_name_string: str, expected: str, submission: MagicMock):
    submission.title = test_name_string
    test_formatter = FileNameFormatter("{TITLE}", "", "ISO")
    result = test_formatter._format_name(submission, "{TITLE}")
    assert do_test_string_equality(result, expected)


@pytest.mark.parametrize(
    ("test_string", "expected"),
    (
        ("test \\u2019", "test ’"),
        ("My cat\\u2019s paws are so cute", "My cat’s paws are so cute"),
    ),
)
def test_convert_unicode_escapes(test_string: str, expected: str):
    result = FileNameFormatter._convert_unicode_escapes(test_string)
    assert result == expected


@pytest.mark.parametrize(
    ("test_datetime", "expected"),
    (
        (datetime(2020, 1, 1, 8, 0, 0), "2020-01-01T08:00:00"),
        (datetime(2020, 1, 1, 8, 0), "2020-01-01T08:00:00"),
        (datetime(2021, 4, 21, 8, 30, 21), "2021-04-21T08:30:21"),
    ),
)
def test_convert_timestamp(test_datetime: datetime, expected: str):
    test_timestamp = test_datetime.timestamp()
    test_formatter = FileNameFormatter("{POSTID}", "", "ISO")
    result = test_formatter._convert_timestamp(test_timestamp)
    assert result == expected


@pytest.mark.parametrize(
    ("test_time_format", "expected"),
    (
        ("ISO", "2021-05-02T13:33:00"),
        ("%Y_%m", "2021_05"),
        ("%Y-%m-%d", "2021-05-02"),
    ),
)
def test_time_string_formats(test_time_format: str, expected: str):
    test_time = datetime(2021, 5, 2, 13, 33)
    test_formatter = FileNameFormatter("{TITLE}", "", test_time_format)
    result = test_formatter._convert_timestamp(test_time.timestamp())
    assert result == expected


def test_windows_max_path(tmp_path: Path):
    mock = MagicMock()
    mock.max_path = 260
    result = FileNameFormatter.limit_file_name_length(mock, "test" * 100, "_1.png", tmp_path)
    assert len(str(result)) <= 260
    assert len(result.name) <= (260 - len(str(tmp_path)))


def test_shortening_stops_when_no_name_fits(tmp_path: Path, test_formatter: FileNameFormatter):
    # A folder so deep that no name fits used to shorten the name forever (it hung the tests on Windows when pytest's
    # temporary folder was long)
    test_formatter.max_path = len(str(tmp_path.resolve())) + 5
    result = []
    worker = threading.Thread(
        target=lambda: result.append(test_formatter.limit_file_name_length("a" * 50, ".jpg", tmp_path)), daemon=True
    )
    worker.start()
    worker.join(5)
    assert result, "limit_file_name_length did not return"
    assert result[0].name == ".jpg"
