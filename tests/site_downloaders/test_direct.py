#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from unittest.mock import Mock

import pytest
import requests

from bdfr.configuration import Configuration
from bdfr.exceptions import SiteDownloaderError
from bdfr.site_downloaders.direct import Direct


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
