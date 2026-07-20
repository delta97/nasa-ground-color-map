"""Disk cache for GIBS tiles.

Layout: {cache_dir}/{layer}/{date}/{z}/{row}/{col}.{ext}

Tiles for a concrete past date are immutable, so entries have no TTL. The
cache is size-capped: periodically evict oldest-mtime files down to 80% of
the cap; hits touch mtime to approximate LRU.
"""

import os
import tempfile
import time
from pathlib import Path


class TileCache:
    def __init__(self, cache_dir: str, max_bytes: int, eviction_check_interval: int = 100):
        self.root = Path(cache_dir)
        self.max_bytes = max_bytes
        self.eviction_check_interval = eviction_check_interval
        self._writes_since_check = 0
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, layer: str, date: str, zoom: int, row: int, col: int, ext: str) -> Path:
        return self.root / layer / date / str(zoom) / str(row) / f"{col}.{ext}"

    def get(self, layer: str, date: str, zoom: int, row: int, col: int, ext: str) -> bytes | None:
        path = self._path(layer, date, zoom, row, col, ext)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        try:
            os.utime(path, None)  # LRU touch
        except OSError:
            pass
        return data

    def put(self, layer: str, date: str, zoom: int, row: int, col: int, ext: str, data: bytes) -> None:
        path = self._path(layer, date, zoom, row, col, ext)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)  # atomic within the same directory
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._writes_since_check += 1
        if self._writes_since_check >= self.eviction_check_interval:
            self._writes_since_check = 0
            self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        entries: list[tuple[float, int, Path]] = []
        total = 0
        for path in self.root.rglob("*"):
            if not path.is_file() or path.suffix == ".tmp":
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
            total += stat.st_size
        if total <= self.max_bytes:
            return
        target = int(self.max_bytes * 0.8)
        entries.sort()  # oldest mtime first
        for _, size, path in entries:
            if total <= target:
                break
            try:
                path.unlink()
                total -= size
            except OSError:
                pass

    def force_eviction_check(self) -> None:
        """For tests and maintenance."""
        self._evict_if_needed()

    @staticmethod
    def now() -> float:
        return time.time()
