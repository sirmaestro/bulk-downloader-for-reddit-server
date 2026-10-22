#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""`bdfr similar`: finds images that look alike although their files differ, offline, from perceptual hashes (pHash):
re-uploads of the same picture inside a folder, and folders sharing many pictures (likely one person's old and new
accounts, see `bdfr merge`). Needs Pillow (`pip install "bdfr[similar]"`), which serve doesn't.

Each folder's hashes are kept in its phashes.sqlite, by file name, size and modified time, so later runs only hash new
files. Nothing else is written, and nothing is requested from anywhere."""

import logging
import math
import os
import sqlite3
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations
from pathlib import Path
from typing import Optional

from bdfr.exceptions import BulkDownloaderException
from bdfr.hash_store import HashStore

logger = logging.getLogger(__name__)

CACHE_NAME = "phashes.sqlite"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
HASH_BITS = 64
SIZE = 32  # the image is shrunk to SIZE x SIZE before its DCT; the hash is the lowest 8 x 8 frequencies
_COSINES = [[math.cos(math.pi * (2 * x + 1) * u / (2 * SIZE)) for x in range(SIZE)] for u in range(8)]


def _image_module():
    try:
        from PIL import Image
    except ImportError:
        raise BulkDownloaderException('bdfr similar needs Pillow: pip install "bdfr[similar]"') from None
    return Image


def phash(path: Path) -> Optional[int]:
    """The image's 64-bit perceptual hash (as imagehash.phash computes it), or None if it can't be read"""
    image_module = _image_module()
    try:
        with image_module.open(path) as image:
            image.draft("L", (SIZE * 4, SIZE * 4))  # JPEGs are decoded at a fraction of their size
            pixels = list(image.convert("L").resize((SIZE, SIZE), image_module.Resampling.LANCZOS).tobytes())
    except (OSError, ValueError, image_module.DecompressionBombError) as e:
        logger.debug(f"Not hashing {path.name}: {e}")
        return None
    rows = [pixels[y * SIZE : (y + 1) * SIZE] for y in range(SIZE)]
    # The 2D DCT's lowest 8 x 8 frequencies: along each row, then down each column
    along = [[sum(c * p for c, p in zip(cosines, row)) for cosines in _COSINES] for row in rows]
    low = [sum(_COSINES[v][y] * along[y][u] for y in range(SIZE)) for v in range(8) for u in range(8)]
    median = sorted(low)[len(low) // 2 - 1 : len(low) // 2 + 1]
    middle = sum(median) / 2
    return sum(1 << i for i, value in enumerate(low) if value > middle)


def distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def folder_hashes(folder: Path) -> dict[str, int]:
    """File name -> pHash of every image in the folder (not its subfolders), from phashes.sqlite where the file is
    unchanged, hashing and storing the rest"""
    images = {}
    for entry in os.scandir(folder):
        if entry.is_file() and Path(entry.name).suffix.lower() in IMAGE_EXTENSIONS:
            if not HashStore._is_own_file(entry.name):
                stat = entry.stat()
                images[entry.name] = (stat.st_size, int(stat.st_mtime))
    connection = sqlite3.connect(folder / CACHE_NAME)
    try:
        with connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS phashes (name TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, hash TEXT)"
            )
        cached = {
            name: (size, mtime, value)
            for name, size, mtime, value in connection.execute("SELECT name, size, mtime, hash FROM phashes")
        }
        stale = [name for name, key in images.items() if cached.get(name, (None, None, None))[:2] != key]
        if stale:
            logger.info(f"{folder.name}: hashing {len(stale)} images")
            with ThreadPoolExecutor(min(32, (os.cpu_count() or 1) + 4), thread_name_prefix="bdfr-phash") as pool:
                hashes = list(pool.map(lambda name: phash(folder / name), stale))
            with connection:
                # Stored as text: SQLite's integers are signed 64-bit
                rows = [(name, *images[name], None if h is None else str(h)) for name, h in zip(stale, hashes)]
                connection.executemany("INSERT OR REPLACE INTO phashes VALUES (?, ?, ?, ?)", rows)
                gone = [(name,) for name in cached if name not in images]
                connection.executemany("DELETE FROM phashes WHERE name = ?", gone)
            for name, h in zip(stale, hashes):
                cached[name] = (*images[name], None if h is None else str(h))
    finally:
        connection.close()
    return {name: int(cached[name][2]) for name in images if cached[name][2] is not None}


def similar_pairs(hashes: list[int], threshold: int) -> set[tuple[int, int]]:
    """Index pairs of hashes at most threshold bits apart. Split into threshold + 1 pieces, two such hashes share at
    least one piece, so only hashes sharing a piece are compared."""
    pieces = threshold + 1
    bounds = [round(i * HASH_BITS / pieces) for i in range(pieces + 1)]
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, value in enumerate(hashes):
        for piece in range(pieces):
            low, high = bounds[piece], bounds[piece + 1]
            buckets[(piece, (value >> low) & ((1 << (high - low)) - 1))].append(index)
    pairs = set()
    for members in buckets.values():
        for a, b in combinations(members, 2):
            if (a, b) not in pairs and distance(hashes[a], hashes[b]) <= threshold:
                pairs.add((a, b))
    return pairs


def find_similar(folders: list[Path], threshold: int = 4, min_shared: int = 3) -> dict:
    """Images that look alike within each folder, and pairs of folders with at least min_shared such images"""
    if not 0 <= threshold <= 16:
        raise BulkDownloaderException("The threshold must be between 0 and 16 bits")
    items: list[tuple[str, str]] = []  # (folder, file name)
    hashes: list[int] = []
    for folder in folders:
        for name, value in sorted(folder_hashes(folder).items()):
            items.append((folder.name, name))
            hashes.append(value)
    within: dict[str, list[tuple[str, str]]] = defaultdict(list)
    shared: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for a, b in sorted(similar_pairs(hashes, threshold)):
        (folder_a, name_a), (folder_b, name_b) = items[a], items[b]
        if folder_a == folder_b:
            within[folder_a].append((name_a, name_b))
        else:
            key, names = ((folder_a, folder_b), (name_a, name_b))
            if folder_b < folder_a:
                key, names = (folder_b, folder_a), (name_b, name_a)
            shared[key].append(names)
    across = {key: pairs for key, pairs in shared.items() if len(pairs) >= min_shared}
    return {"images": len(hashes), "within": dict(within), "across": across}


def report_similar(root: Path, names: Optional[list[str]], threshold: int, min_shared: int) -> dict:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise BulkDownloaderException(f"{root} is not a folder")
    if names:
        folders = [root / name for name in names]
        for folder in folders:
            if not folder.is_dir():
                raise BulkDownloaderException(f"{folder} is not a folder")
    else:
        folders = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    result = find_similar(folders, threshold, min_shared)
    for folder, pairs in sorted(result["within"].items()):
        logger.info(f"{folder}: {len(pairs)} pairs of images that look alike")
        for name_a, name_b in pairs:
            logger.info(f"  {name_a}  ~  {name_b}")
    for (folder_a, folder_b), pairs in sorted(result["across"].items(), key=lambda item: -len(item[1])):
        logger.info(
            f"{folder_a} and {folder_b} share {len(pairs)} images (the same person? see bdfr merge), e.g. "
            + ", ".join(f"{a} ~ {b}" for a, b in pairs[:3])
        )
    shown = sum(len(p) for p in result["within"].values())
    logger.info(
        f"Compared {result['images']} images in {len(folders)} folders: {shown} look-alike pairs within folders, "
        f"{len(result['across'])} pairs of folders sharing at least {min_shared}"
    )
    return result
