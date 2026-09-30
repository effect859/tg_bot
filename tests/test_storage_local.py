from pathlib import Path

import pytest

from app.services.storage.local import LocalFileStorage


async def test_save_read_delete(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)

    await storage.save("orders/1/report.docx", b"content")

    assert await storage.exists("orders/1/report.docx")
    assert await storage.read("orders/1/report.docx") == b"content"

    await storage.delete("orders/1/report.docx")
    assert not await storage.exists("orders/1/report.docx")


async def test_save_overwrites(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)

    await storage.save("a.txt", b"old")
    await storage.save("a.txt", b"new")

    assert await storage.read("a.txt") == b"new"


async def test_read_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await LocalFileStorage(tmp_path).read("missing.txt")


async def test_delete_missing_is_noop(tmp_path: Path) -> None:
    await LocalFileStorage(tmp_path).delete("missing.txt")


@pytest.mark.parametrize("key", ["../outside.txt", "a/../../outside.txt", ""])
async def test_rejects_keys_outside_root(tmp_path: Path, key: str) -> None:
    storage = LocalFileStorage(tmp_path / "root")

    with pytest.raises(ValueError):
        await storage.save(key, b"x")
