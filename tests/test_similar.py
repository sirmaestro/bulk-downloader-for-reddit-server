#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import random
from pathlib import Path

import pytest
from click.testing import CliRunner

from bdfr import similar
from bdfr.__main__ import cli
from bdfr.hash_store import HashStore
from bdfr.similar import distance, find_similar, folder_hashes, phash, similar_pairs

Image = pytest.importorskip("PIL.Image")


def picture(seed: int) -> "Image.Image":
    """A 256 x 256 picture of random 8 x 8 blocks, so different seeds differ in their broad shapes"""
    rng = random.Random(seed)
    blocks = Image.new("RGB", (8, 8))
    blocks.putdata([tuple(rng.randrange(256) for _ in range(3)) for _ in range(64)])
    return blocks.resize((256, 256), Image.Resampling.BILINEAR)


def save(image: "Image.Image", path: Path, **options) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, **options)
    return path


def reupload(image: "Image.Image") -> "Image.Image":
    """The same picture as a host might serve it again: smaller and recompressed"""
    return image.resize((200, 200), Image.Resampling.BICUBIC)


def test_reuploads_hash_alike_and_other_pictures_do_not(tmp_path: Path):
    original = phash(save(picture(1), tmp_path / "a.png"))
    copy = phash(save(reupload(picture(1)), tmp_path / "b.jpg", quality=70))
    other = phash(save(picture(2), tmp_path / "c.png"))
    assert distance(original, copy) <= 4
    assert distance(original, other) > 10


def test_unreadable_files_have_no_hash(tmp_path: Path):
    (tmp_path / "broken.jpg").write_bytes(b"not an image")
    assert phash(tmp_path / "broken.jpg") is None


@pytest.mark.parametrize("threshold", (0, 3, 6))
def test_similar_pairs_finds_what_comparing_every_pair_finds(threshold: int):
    rng = random.Random(threshold)
    base = [rng.getrandbits(64) for _ in range(40)]
    # Near copies of some, a few bits flipped
    hashes = base + [h ^ (1 << rng.randrange(64)) ^ (1 << rng.randrange(64)) for h in base[:20]]
    expected = {
        (a, b)
        for a in range(len(hashes))
        for b in range(a + 1, len(hashes))
        if distance(hashes[a], hashes[b]) <= threshold
    }
    assert similar_pairs(hashes, threshold) == expected


def test_hashes_are_cached_per_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    save(picture(1), tmp_path / "a.png")
    save(picture(2), tmp_path / "b.png")
    (tmp_path / "notes.txt").write_text("not an image")
    hashed = []
    real = similar.phash
    monkeypatch.setattr(similar, "phash", lambda path: hashed.append(path.name) or real(path))
    first = folder_hashes(tmp_path)
    assert sorted(first) == ["a.png", "b.png"] and sorted(hashed) == ["a.png", "b.png"]
    hashed.clear()
    assert folder_hashes(tmp_path) == first and hashed == []
    (tmp_path / "b.png").unlink()
    save(picture(3), tmp_path / "c.png")
    assert sorted(folder_hashes(tmp_path)) == ["a.png", "c.png"] and hashed == ["c.png"]
    assert HashStore._is_own_file("phashes.sqlite")


def test_finds_reuploads_and_folders_sharing_pictures(tmp_path: Path):
    old, new, other = tmp_path / "old_account", tmp_path / "new_account", tmp_path / "someone_else"
    for seed in range(3):
        save(picture(seed), old / f"o{seed}.png")
        save(reupload(picture(seed)), new / f"n{seed}.jpg", quality=80)
    save(picture(10), other / "x.png")
    save(reupload(picture(10)), other / "x_again.jpg", quality=80)
    result = find_similar([new, old, other], min_shared=3)
    assert result["images"] == 8
    assert result["within"] == {"someone_else": [("x.png", "x_again.jpg")]}
    assert list(result["across"]) == [("new_account", "old_account")]
    assert sorted(result["across"][("new_account", "old_account")]) == [(f"n{s}.jpg", f"o{s}.png") for s in range(3)]
    assert find_similar([new, old], min_shared=4)["across"] == {}


def test_similar_command(tmp_path: Path):
    for seed in range(3):
        save(picture(seed), tmp_path / "old_account" / f"o{seed}.png")
        save(reupload(picture(seed)), tmp_path / "new_account" / f"n{seed}.jpg", quality=80)
    result = CliRunner().invoke(cli, ["similar", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "new_account and old_account share 3 images" in result.output
