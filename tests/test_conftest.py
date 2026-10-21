#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import pytest
import requests

from bdfr.arctic_shift import ArcticShift


def test_live_arctic_shift_is_blocked():
    with pytest.raises(AssertionError, match="live Arctic Shift"):
        requests.get(f"{ArcticShift.API_URL}/posts/ids?ids=abc", timeout=5)
