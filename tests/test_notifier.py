#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bdfr.notifier import Notifier


@pytest.fixture()
def receiver():
    """A local stand-in for an ntfy topic, recording what is POSTed to it"""
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8")
            received.append((self.headers["Title"], body))
            self.send_response(200)
            self.end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/bdfr", received
    httpd.shutdown()
    httpd.server_close()


class Gate:
    """Stands in for time.sleep: records the waits and holds the batch until released"""

    def __init__(self):
        self.waits = []
        self.release = threading.Event()

    def __call__(self, seconds: float):
        self.waits.append(seconds)
        assert self.release.wait(5)


def send(notifier: Notifier, gate: Gate, *messages: str):
    for message in messages:
        notifier.notify(message)
    thread = notifier._sending
    gate.release.set()
    thread.join(5)
    gate.release.clear()


def test_messages_close_together_are_one_notification(receiver):
    url, received = receiver
    gate = Gate()
    notifier = Notifier(url, sleep=gate)
    send(notifier, gate, "Job a failed", "u/someone is suspended on Reddit")
    assert received == [("bdfr serve: 2 problems", "Job a failed\nu/someone is suspended on Reddit")]
    assert gate.waits == [Notifier.BATCH_SECONDS]
    send(notifier, gate, "Job b failed")
    assert received[1] == ("bdfr serve: Job b failed", "Job b failed")


def test_notifications_are_spaced_out(receiver):
    url, _received = receiver
    gate, now = Gate(), [1000.0]
    notifier = Notifier(url, sleep=gate, clock=lambda: now[0])
    send(notifier, gate, "first")
    now[0] += 120
    send(notifier, gate, "second")
    assert gate.waits == [Notifier.BATCH_SECONDS, Notifier.GAP_SECONDS - 120]


def test_large_batches_are_cut_short(receiver):
    url, received = receiver
    gate = Gate()
    send(Notifier(url, sleep=gate), gate, *(f"Job {n} failed" for n in range(Notifier.MAX_LINES + 5)))
    lines = received[0][1].splitlines()
    assert len(lines) == Notifier.MAX_LINES + 1 and lines[-1].startswith("... and 5 more")


def test_unreachable_receiver_is_logged(caplog: pytest.LogCaptureFixture):
    gate = Gate()
    with caplog.at_level(logging.WARNING):
        send(Notifier("http://127.0.0.1:9/bdfr", sleep=gate), gate, "Job a failed")
    assert "Sending a notification to http://127.0.0.1:9/bdfr failed" in caplog.text
