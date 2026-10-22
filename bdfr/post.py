#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from typing import Any, Optional


class Post:
    """A Reddit post as Arctic Shift, the userscript and post JSON files give it: the post's JSON fields as attributes.

    Nothing is ever fetched, so a field missing from the data raises AttributeError (use getattr with a default for
    fields not every post has). `author` and `subreddit` are names; a deleted author is None."""

    def __init__(self, data: dict):
        data = dict(data)
        for key in ("link_flair_text", "score", "upvote_ratio"):
            data.setdefault(key, None)
        data.setdefault("selftext", "")
        self._data = data

    def __getattr__(self, name: str) -> Any:
        # Only called for names that aren't normal attributes; _data itself may be missing while copying or unpickling
        if name.startswith("__") or name == "_data":
            raise AttributeError(name)
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(f"Post {self._data.get('id')} has no field '{name}'") from None

    def to_dict(self) -> dict:
        """The post's data, e.g. to give it to a later job"""
        return dict(self._data)

    @property
    def author(self) -> Optional[str]:
        author = self._data.get("author")
        return None if author in (None, "", "[deleted]") else str(author)

    @property
    def subreddit(self) -> str:
        return str(self._data.get("subreddit") or "")

    @property
    def fullname(self) -> str:
        return f"t3_{self.id}"

    @property
    def data(self) -> dict:
        return self._data

    def __eq__(self, other) -> bool:
        return isinstance(other, Post) and other.id == self.id

    def __hash__(self) -> int:
        return hash(self.id)

    def __repr__(self) -> str:
        return f"Post(id={self._data.get('id')!r})"
