"""Small bounded cache for completed derived API results."""

from __future__ import annotations

import copy
import time
from collections import OrderedDict


class ResultCache:
    def __init__(self, max_entries: int = 256):
        self.max_entries = max_entries
        self._items: OrderedDict[str, tuple[float | None, object]] = OrderedDict()

    def get(self, key: str):
        item = self._items.get(key)
        if item is None:
            return None
        expires, value = item
        if expires is not None and expires <= time.monotonic():
            self._items.pop(key, None)
            return None
        self._items.move_to_end(key)
        return copy.deepcopy(value)

    def set(self, key: str, value, ttl: int | None = None) -> None:
        expires = time.monotonic() + ttl if ttl is not None else None
        self._items[key] = (expires, copy.deepcopy(value))
        self._items.move_to_end(key)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)
