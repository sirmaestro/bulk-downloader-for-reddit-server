#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""A local stand-in for the Arctic Shift API, and for the media its posts link to, so tests run the real downloader
without reaching the real archive. Failures (HTTP errors, error messages, dropped connections, invalid JSON) can be
queued per endpoint."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from bdfr.arctic_shift import ArcticShift


class MockArcticShift:
    def __init__(self, page_size: int = 100):
        # What "limit=auto" returns per page
        self.page_size = page_size
        self.posts: dict[str, dict] = {}
        self.media: dict[str, bytes] = {}
        # Endpoint ("posts/search", "posts/ids", "media") -> failures still to give, oldest first
        self.failures: dict[str, list[dict]] = {}
        # (endpoint, query parameters) of every API request, in order
        self.requests: list[tuple[str, dict]] = []
        self._lock = threading.Lock()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self.base = ""

    def add_post(self, post_id: str, created_utc: int, author: str = "someone", **fields) -> dict:
        """A post linking to an image served by the mock (unless 'url' is given)"""
        url = fields.pop("url", None) or f"{self.base}/media/{post_id}.jpg"
        self.media.setdefault(f"{post_id}.jpg", f"image of {post_id}".encode())
        post = {
            "id": post_id,
            "title": f"Post {post_id}",
            "author": author,
            "subreddit": "pics",
            "created_utc": created_utc,
            "url": url,
            "domain": urlsplit(url).netloc,
            "permalink": f"/r/pics/comments/{post_id}/post/",
            "is_self": False,
            "over_18": False,
            "score": 1,
            "num_comments": 0,
            **fields,
        }
        self.posts[post_id] = post
        return post

    def fail(
        self,
        endpoint: str,
        status: int = 503,
        error: Optional[str] = None,
        drop: bool = False,
        invalid_json: bool = False,
        times: int = 1,
    ):
        self.failures.setdefault(endpoint, []).extend(
            [{"status": status, "error": error, "drop": drop, "invalid_json": invalid_json}] * times
        )

    def endpoint_requests(self, endpoint: str) -> list[dict]:
        return [params for name, params in self.requests if name == endpoint]

    # --- the API ---

    def search(self, params: dict) -> list[dict]:
        posts = list(self.posts.values())
        for key in ("author", "subreddit"):
            if key in params:
                posts = [post for post in posts if post[key].lower() == params[key].lower()]
        if "after" in params:
            posts = [post for post in posts if post["created_utc"] > int(params["after"])]
        if "before" in params:
            posts = [post for post in posts if post["created_utc"] < int(params["before"])]
        posts.sort(key=lambda post: post["created_utc"], reverse=params.get("sort") != "asc")
        limit = params.get("limit", "auto")
        return posts[: self.page_size if limit == "auto" else int(limit)]

    def by_ids(self, params: dict) -> list[dict]:
        return [self.posts[i] for i in params.get("ids", "").split(",") if i in self.posts]

    # --- the server ---

    def _handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parts = urlsplit(self.path)
                params = {key: values[-1] for key, values in parse_qs(parts.query).items()}
                if parts.path.startswith("/media/"):
                    endpoint = "media"
                elif parts.path.startswith("/api/"):
                    endpoint = parts.path[len("/api/") :]
                    with mock._lock:
                        mock.requests.append((endpoint, params))
                else:
                    endpoint = None
                with mock._lock:
                    failure = mock.failures.get(endpoint, [None]).pop(0) if mock.failures.get(endpoint) else None
                if failure:
                    if failure["drop"]:
                        # Closing without an answer: the client sees a connection error
                        self.close_connection = True
                        self.connection.shutdown(2)
                        return
                    if failure["invalid_json"]:
                        return self._send(200, b"<html>not json</html>", "text/html")
                    body = {"error": failure["error"]} if failure["error"] else {}
                    return self._send(failure["status"], json.dumps(body).encode(), "application/json")
                if endpoint == "media":
                    data = mock.media.get(parts.path[len("/media/") :])
                    if data is None:
                        return self._send(404, b"", "text/plain")
                    return self._send(200, data, "image/jpeg")
                if endpoint == "posts/search":
                    data = mock.search(params)
                elif endpoint == "posts/ids":
                    data = mock.by_ids(params)
                else:
                    return self._send(404, json.dumps({"error": "Not found"}).encode(), "application/json")
                self._send(200, json.dumps({"data": data}).encode(), "application/json")

            def _send(self, status: int, data: bytes, content_type: str):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler

    @contextmanager
    def running(self):
        """Serves the mock and points ArcticShift at it, without waits between requests or retries"""
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.base = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        try:
            with (
                patch.object(ArcticShift, "API_URL", f"{self.base}/api"),
                patch.object(ArcticShift, "MIN_REQUEST_INTERVAL", 0),
                patch.object(ArcticShift, "_retry_delay", staticmethod(lambda error, attempt: 0)),
            ):
                yield self
        finally:
            self._httpd.shutdown()
            self._httpd.server_close()
