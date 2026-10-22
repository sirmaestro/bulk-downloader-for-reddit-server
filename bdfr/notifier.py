#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Notifications from `bdfr serve` (--notify URL, or BDFR_NOTIFY): failed jobs, watched users found suspended or
deleted, and failed posts given up on. Each message is POSTed as plain text with a Title header, as ntfy
(https://ntfy.sh/<topic>, or a self-hosted server) takes them; any webhook that accepts a text body works too. Events
within BATCH_SECONDS of the first are sent together, and at most one notification goes out per GAP_SECONDS, so a
run of failures (e.g. every scheduled check while Arctic Shift is down) is one or two notifications."""

import logging
import threading
import time
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)


class Notifier:
    BATCH_SECONDS = 60
    GAP_SECONDS = 15 * 60
    # The longest message sent; the rest of a large batch is counted instead
    MAX_LINES = 20

    def __init__(
        self, url: str, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic
    ):
        self.url = url
        self.sleep = sleep
        self.clock = clock
        self._last_sent: Optional[float] = None
        self._events: list[str] = []
        self._lock = threading.Lock()
        self._sending: Optional[threading.Thread] = None

    def notify(self, message: str):
        """Queues a message; the first one starts a batch that is sent BATCH_SECONDS later"""
        with self._lock:
            self._events.append(message)
            if self._sending is None:
                self._sending = threading.Thread(target=self._send_batch, name="bdfr-serve-notify", daemon=True)
                self._sending.start()

    def _send_batch(self):
        wait = self.BATCH_SECONDS
        if self._last_sent is not None:
            wait = max(wait, self._last_sent + self.GAP_SECONDS - self.clock())
        self.sleep(wait)
        with self._lock:
            events, self._events = self._events, []
            self._sending = None
        lines = events[: self.MAX_LINES]
        if len(events) > self.MAX_LINES:
            lines.append(f"... and {len(events) - self.MAX_LINES} more (see serve_log.txt)")
        title = events[0] if len(events) == 1 else f"{len(events)} problems"
        self._last_sent = self.clock()
        try:
            response = requests.post(
                self.url,
                data="\n".join(lines).encode("utf-8"),
                headers={"Title": f"bdfr serve: {title}"[:200].encode("utf-8"), "Content-Type": "text/plain"},
                timeout=30,
            )
            if response.status_code >= 400:
                logger.warning(f"Sending a notification to {self.url} failed: HTTP {response.status_code}")
        except requests.exceptions.RequestException as e:
            logger.warning(f"Sending a notification to {self.url} failed: {e}")
