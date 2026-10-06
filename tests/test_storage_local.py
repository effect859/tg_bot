import asyncio
import re
import threading
from pathlib import Path
from typing import Any

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


# --- Одновременные save одного ключа -------------------------------------------------------
#
# Порядок операций в потоках записи задаётся событиями, без sleep: подмена Path.open
# останавливает n-е открытие временного файла, подмена Path.replace — замену.

KEY = "orders/1/report/plan-1.docx"
DATA = bytes(range(256)) * 1024
TIMEOUT = 10.0


def tmp_files(root: Path) -> list[str]:
    return sorted(path.name for path in root.rglob("*.tmp"))


class TmpWriteGate:
    """Останавливает ``open_number``-е открытие временного файла хранилища.

    ``pause_at="write"`` — записав половину данных, ``"open"`` — сразу после открытия,
    ``None`` — без остановки. После release() запись дописывается или (``fail``)
    падает OSError.
    """

    def __init__(
        self, *, open_number: int = 1, pause_at: str | None = "write", fail: bool = False
    ) -> None:
        self.open_number = open_number
        self.pause_at = pause_at
        self.fail = fail
        self.paused = threading.Event()
        self._release = threading.Event()
        self._lock = threading.Lock()
        self._opened = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "TmpWriteGate":
        real_open = Path.open
        gate = self

        def open_(path: Path, *args: Any, **kwargs: Any) -> Any:
            file = real_open(path, *args, **kwargs)
            if path.suffix != ".tmp":
                return file
            with gate._lock:
                gate._opened += 1
                mine = gate._opened == gate.open_number
            if not mine:
                return file
            if gate.pause_at == "open":
                gate.pause()
            return _GatedFile(file, gate)

        monkeypatch.setattr(Path, "open", open_)
        return self

    def pause(self) -> None:
        self.paused.set()
        assert self._release.wait(TIMEOUT)

    def release(self) -> None:
        self._release.set()

    async def wait_paused(self) -> None:
        assert await asyncio.to_thread(self.paused.wait, TIMEOUT)


class _GatedFile:
    def __init__(self, file: Any, gate: TmpWriteGate) -> None:
        self._file = file
        self._gate = gate

    def write(self, data: bytes) -> int:
        written = 0
        if self._gate.pause_at == "write":
            written = self._file.write(data[: len(data) // 2])
            self._file.flush()
            self._gate.pause()
        if self._gate.fail:
            raise OSError("simulated write failure")
        return written + self._file.write(data[written:])

    def __enter__(self) -> "_GatedFile":
        return self

    def __exit__(self, *exc: object) -> None:
        self._file.close()


class ReplaceGate:
    """Останавливает первую замену временного файла: tmp уже закрыт, replace ещё нет."""

    def __init__(self) -> None:
        self.paused = threading.Event()
        self._release = threading.Event()
        self._lock = threading.Lock()
        self._seen = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "ReplaceGate":
        real_replace = Path.replace
        gate = self

        def replace(path: Path, target: Any) -> Any:
            with gate._lock:
                first = path.suffix == ".tmp" and not gate._seen
                gate._seen = gate._seen or first
            if first:
                gate.paused.set()
                assert gate._release.wait(TIMEOUT)
            return real_replace(path, target)

        monkeypatch.setattr(Path, "replace", replace)
        return self

    def release(self) -> None:
        self._release.set()

    async def wait_paused(self) -> None:
        assert await asyncio.to_thread(self.paused.wait, TIMEOUT)


@pytest.mark.parametrize("a_outcome", ["completes", "fails"])
@pytest.mark.parametrize(
    "order", ["a_writes_part_then_b_saves", "a_opens_between_b_close_and_replace"]
)
async def test_concurrent_save_same_key_never_exposes_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, order: str, a_outcome: str
) -> None:
    """Старая запись A и новая B одного ключа: файл по ключу всегда полный.

    a_opens_between_b_close_and_replace — сценарий R1: при общем ``<key>.tmp`` A
    усекает уже закрытый tmp B, и replace B ставит пустой файл.
    """
    storage = LocalFileStorage(tmp_path)
    fails = a_outcome == "fails"
    gates: list[TmpWriteGate | ReplaceGate] = []
    try:
        if order == "a_writes_part_then_b_saves":
            a = TmpWriteGate(open_number=1, pause_at="write", fail=fails).install(monkeypatch)
            gates.append(a)
            task_a = asyncio.create_task(storage.save(KEY, DATA))
            await a.wait_paused()  # A открыл свой tmp, записал половину и стоит
            await storage.save(KEY, DATA)  # B — полностью
        else:
            a = TmpWriteGate(open_number=2, pause_at="open", fail=fails).install(monkeypatch)
            b = ReplaceGate().install(monkeypatch)
            gates += [a, b]
            task_b = asyncio.create_task(storage.save(KEY, DATA))
            await b.wait_paused()  # B закрыл tmp, replace ещё не сделан
            task_a = asyncio.create_task(storage.save(KEY, DATA))
            await a.wait_paused()  # A открыл tmp, не записав ни байта
            b.release()
            await task_b

        assert await storage.read(KEY) == DATA

        a.release()
        if fails:
            with pytest.raises(OSError, match="simulated write failure"):
                await task_a
        else:
            await task_a

        assert await storage.read(KEY) == DATA
        assert tmp_files(tmp_path) == []
    finally:
        for gate in gates:
            gate.release()


@pytest.mark.parametrize("failing", ["write", "replace"])
async def test_save_failure_leaves_no_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    storage = LocalFileStorage(tmp_path)
    await storage.save(KEY, b"old")
    if failing == "write":
        TmpWriteGate(pause_at=None, fail=True).install(monkeypatch)
    else:

        def replace(path: Path, target: Any) -> Any:
            raise OSError("simulated replace failure")

        monkeypatch.setattr(Path, "replace", replace)

    with pytest.raises(OSError, match=f"simulated {failing} failure"):
        await storage.save(KEY, DATA)

    assert await storage.read(KEY) == b"old"
    assert tmp_files(tmp_path) == []


async def test_tmp_names_are_unique(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = LocalFileStorage(tmp_path)
    sources: list[str] = []
    real_replace = Path.replace

    def replace(path: Path, target: Any) -> Any:
        sources.append(path.name)
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", replace)

    await storage.save(KEY, b"first")
    await storage.save(KEY, b"second")

    assert len(sources) == len(set(sources)) == 2
    assert all(re.fullmatch(r"plan-1\.docx\.[0-9a-f]{32}\.tmp", name) for name in sources)
    assert await storage.read(KEY) == b"second"
    assert tmp_files(tmp_path) == []
