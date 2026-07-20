import os
import time

from nasa_ground_color_map.gibs.cache import TileCache


def test_miss_then_hit(tmp_path):
    cache = TileCache(str(tmp_path), max_bytes=10_000)
    assert cache.get("L", "2026-01-01", 3, 1, 2, "jpg") is None
    cache.put("L", "2026-01-01", 3, 1, 2, "jpg", b"data")
    assert cache.get("L", "2026-01-01", 3, 1, 2, "jpg") == b"data"


def test_layout(tmp_path):
    cache = TileCache(str(tmp_path), max_bytes=10_000)
    cache.put("MyLayer", "2026-01-01", 5, 10, 20, "png", b"x")
    assert (tmp_path / "MyLayer" / "2026-01-01" / "5" / "10" / "20.png").exists()


def test_no_tmp_files_left(tmp_path):
    cache = TileCache(str(tmp_path), max_bytes=10_000)
    for i in range(5):
        cache.put("L", "2026-01-01", 1, 0, i, "jpg", b"abc")
    leftovers = [p for p in tmp_path.rglob("*.tmp")]
    assert leftovers == []


def test_eviction_removes_oldest(tmp_path):
    cache = TileCache(str(tmp_path), max_bytes=300, eviction_check_interval=1000)
    for i in range(10):
        cache.put("L", "2026-01-01", 1, 0, i, "jpg", b"x" * 100)  # 1000 bytes total
        # Distinct mtimes so ordering is deterministic
        path = tmp_path / "L" / "2026-01-01" / "1" / "0" / f"{i}.jpg"
        os.utime(path, (time.time() - (100 - i), time.time() - (100 - i)))
    cache.force_eviction_check()
    remaining = sorted(int(p.stem) for p in tmp_path.rglob("*.jpg"))
    # target = 80% of 300 = 240 bytes -> 2 newest files remain
    assert remaining == [8, 9]


def test_hit_touches_mtime(tmp_path):
    cache = TileCache(str(tmp_path), max_bytes=10_000)
    cache.put("L", "2026-01-01", 1, 0, 0, "jpg", b"x")
    path = tmp_path / "L" / "2026-01-01" / "1" / "0" / "0.jpg"
    old = time.time() - 1000
    os.utime(path, (old, old))
    cache.get("L", "2026-01-01", 1, 0, 0, "jpg")
    assert path.stat().st_mtime > old + 500
