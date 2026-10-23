#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from unittest.mock import Mock

import pytest

from bdfr.configuration import Configuration
from bdfr.exceptions import ContentGone, SiteDownloaderError
from bdfr.resource import http_session
from bdfr.site_downloaders.direct import Direct


def test_bare_reddit_image_gets_extension(monkeypatch: pytest.MonkeyPatch):
    # Some posts link i.redd.it images without an extension; the URL is found like a gallery image's (.jpg first)
    tried = []

    def head(url, **kwargs):
        tried.append(url)
        return Mock(status_code=200 if url.endswith(".png") else 404)

    monkeypatch.setattr(http_session(), "head", head)
    mock_submission = Mock()
    mock_submission.url = "https://i.redd.it/ndmbdgcpf9hg1"
    resources = Direct(mock_submission, Configuration()).find_resources()
    assert tried == ["https://i.redd.it/ndmbdgcpf9hg1.jpg", "https://i.redd.it/ndmbdgcpf9hg1.png"]
    assert resources[0].url == "https://i.redd.it/ndmbdgcpf9hg1.png"
    assert resources[0].extension == ".png"


def test_bare_reddit_image_gone(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(http_session(), "head", lambda url, **kwargs: Mock(status_code=404))
    mock_submission = Mock()
    mock_submission.url = "https://i.redd.it/ndmbdgcpf9hg1"
    with pytest.raises(ContentGone, match="not found on i.redd.it"):
        Direct(mock_submission, Configuration()).find_resources()


def test_bare_reddit_image_rate_limited_is_not_gone(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(http_session(), "head", lambda url, **kwargs: Mock(status_code=429))
    mock_submission = Mock()
    mock_submission.url = "https://i.redd.it/ndmbdgcpf9hg1"
    with pytest.raises(SiteDownloaderError, match="HTTP 429") as raised:
        Direct(mock_submission, Configuration()).find_resources()
    assert not isinstance(raised.value, ContentGone)
