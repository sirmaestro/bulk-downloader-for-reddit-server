#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from unittest.mock import Mock

import pytest
import requests

from bdfr.configuration import Configuration
from bdfr.exceptions import SiteDownloaderError
from bdfr.resource import Resource
from bdfr.site_downloaders.direct import Direct


@pytest.mark.online
@pytest.mark.parametrize(
    ("test_url", "expected_hash"),
    (
        ("https://i.redd.it/q6ebualjxzea1.jpg", "6ec154859c777cb401132bb991cb3635"),
        (
            "https://file-examples.com/wp-content/uploads/2017/11/file_example_MP3_700KB.mp3",
            "3caa342e241ddb7d76fd24a834094101",
        ),
    ),
)
def test_download_resource(test_url: str, expected_hash: str):
    mock_submission = Mock()
    mock_submission.url = test_url
    test_site = Direct(mock_submission, Configuration())
    resources = test_site.find_resources()
    assert len(resources) == 1
    assert isinstance(resources[0], Resource)
    resources[0].download()
    assert resources[0].hash.hexdigest() == expected_hash


def test_bare_reddit_image_gets_extension(monkeypatch: pytest.MonkeyPatch):
    # Some posts link i.redd.it images without an extension; the URL is found like a gallery image's (.jpg first)
    tried = []

    def head(url, **kwargs):
        tried.append(url)
        return Mock(status_code=200 if url.endswith(".png") else 404)

    monkeypatch.setattr(requests, "head", head)
    mock_submission = Mock()
    mock_submission.url = "https://i.redd.it/ndmbdgcpf9hg1"
    resources = Direct(mock_submission, Configuration()).find_resources()
    assert tried == ["https://i.redd.it/ndmbdgcpf9hg1.jpg", "https://i.redd.it/ndmbdgcpf9hg1.png"]
    assert resources[0].url == "https://i.redd.it/ndmbdgcpf9hg1.png"
    assert resources[0].extension == ".png"


def test_bare_reddit_image_gone(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(requests, "head", lambda url, **kwargs: Mock(status_code=404))
    mock_submission = Mock()
    mock_submission.url = "https://i.redd.it/ndmbdgcpf9hg1"
    with pytest.raises(SiteDownloaderError, match="not found on i.redd.it"):
        Direct(mock_submission, Configuration()).find_resources()
