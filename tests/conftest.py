#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import socket

import pytest

# The live Arctic Shift API is never used by tests, online ones included: it is a free, shared service that times out
# under load (see AGENTS.md). Tests use tests/mock_arctic_shift.py or a mocked session instead.
ARCTIC_SHIFT_HOST = "arctic-shift.photon-reddit.com"


@pytest.fixture(autouse=True)
def no_live_arctic_shift(monkeypatch: pytest.MonkeyPatch):
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        if isinstance(host, (str, bytes)) and ARCTIC_SHIFT_HOST in (host.decode() if isinstance(host, bytes) else host):
            raise AssertionError(f"Tests must not contact the live Arctic Shift API ({host}); use mock_arctic_shift")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
