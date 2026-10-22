#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import pytest

from bdfr.configuration import Configuration
from bdfr.site_downloaders.youtube import Youtube

VIDEO = b"\x00\x00\x00\x18ftypmp42" + bytes(2000)


@pytest.fixture()
def video_host():
    """A local host serving one MP4 file, recording the requests for it"""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_HEAD(self):
            self._answer(send_body=False)

        def do_GET(self):
            self._answer(send_body=True)

        def _answer(self, send_body: bool):
            requests.append((self.command, self.path))
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(VIDEO)))
            self.end_headers()
            if send_body:
                self.wfile.write(VIDEO)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/clip.mp4", requests
    httpd.shutdown()
    httpd.server_close()


def test_video_details_are_read_once(video_host):
    url, requests = video_host
    post = Mock(url=url)
    resources = Youtube(post, Configuration()).find_resources()
    assert resources[0].extension == "mp4"
    read_details = len(requests)
    resources[0].download()
    assert resources[0].content == VIDEO
    # Downloading reuses the details: only the file itself is requested again
    assert [r for r in requests[read_details:] if r[0] == "GET"] == [("GET", "/clip.mp4")]


def test_video_is_copied_next_to_its_destination(video_host, tmp_path):
    url, _requests = video_host
    resource = Youtube(Mock(url=url), Configuration()).find_resources()[0]
    resource.download({"temp_dir": tmp_path})
    assert resource.content is None and resource.file.parent == tmp_path
    assert resource.hash.hexdigest() == hashlib.md5(VIDEO).hexdigest()
    resource.save(tmp_path / "clip.mp4")
    assert [p.name for p in tmp_path.iterdir()] == ["clip.mp4"]
