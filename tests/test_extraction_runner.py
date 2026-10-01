"""Дочерний процесс извлечения: настоящие процессы, заглушки вместо парсеров для сбоев.

Проверяются тайм-аут и снятие процесса, ограничение вывода, классификация
аварийных завершений, отсутствие секретов в окружении, очередь и уборка процессов.
"""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.domain.enums import MaterialFailureReason
from app.extraction.base import ExtractionFailure, ExtractionResult, ExtractorBusyError
from app.extraction.runner import SubprocessTextExtractor, run_worker
from material_samples import DOCX_BYTES, TXT_BYTES
from pdf_samples import RU_LINES, fixture

R = MaterialFailureReason
LINUX = sys.platform.startswith("linux")


def _stub(code: str, **kwargs: object) -> SubprocessTextExtractor:
    return SubprocessTextExtractor(worker_command=[sys.executable, "-I", "-c", code], **kwargs)


def _process_exists(pid: int) -> bool:
    """Есть ли процесс (в том числе зомби) с таким pid."""
    if LINUX:
        return Path(f"/proc/{pid}").exists()
    if sys.platform == "win32":
        output = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return f'"{pid}"' in output
    pytest.skip("process check is implemented for Linux and Windows")


# --- Настоящий обработчик -----------------------------------------------------------------


async def test_real_worker_extracts_all_formats() -> None:
    extractor = SubprocessTextExtractor()

    txt = await extractor.extract("txt", TXT_BYTES)
    docx = await extractor.extract("docx", DOCX_BYTES)
    pdf = await extractor.extract("pdf", fixture("pdf/ru.pdf"))

    assert isinstance(txt, ExtractionResult)
    assert txt.text == "Тема практики: анализ бизнес-процессов."
    assert isinstance(docx, ExtractionResult)
    assert docx.text == "Задание на практику"
    assert isinstance(pdf, ExtractionResult)
    assert pdf.text == "\n".join(RU_LINES)


async def test_real_worker_reports_document_failures() -> None:
    extractor = SubprocessTextExtractor()

    encrypted = await extractor.extract("pdf", fixture("pdf/enc_user_aes256.pdf"))
    corrupted = await extractor.extract("docx", b"not a zip")
    unknown = await extractor.extract("odt", b"x")

    assert isinstance(encrypted, ExtractionFailure) and encrypted.reason is R.ENCRYPTED
    assert isinstance(corrupted, ExtractionFailure) and corrupted.reason is R.CORRUPTED
    assert isinstance(unknown, ExtractionFailure) and unknown.reason is R.UNSUPPORTED


async def test_real_worker_rejects_oversized_input() -> None:
    result = await SubprocessTextExtractor().extract("txt", b"a" * (20 * 1024 * 1024 + 1))

    assert isinstance(result, ExtractionFailure)
    assert result.reason is R.LIMIT_EXCEEDED


async def test_memory_error_while_reading_input_is_limit_exceeded() -> None:
    """Настоящий worker.main, у которого чтение stdin кончается MemoryError (как при RLIMIT_AS)."""
    code = (
        "import sys\n"
        "from app.extraction import worker\n"
        "class _NoMemory:\n"
        "    def read(self, size=-1):\n"
        "        raise MemoryError\n"
        "class _Stdin:\n"
        "    buffer = _NoMemory()\n"
        "sys.stdin = _Stdin()\n"
        "worker.main(sys.argv[1:])\n"
    )
    result = await _stub(code).extract("pdf", b"%PDF-1.7")

    assert isinstance(result, ExtractionFailure)
    assert result.reason is R.LIMIT_EXCEEDED


async def test_child_gets_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    code = (
        "import json, os, sys; sys.stdout.write(json.dumps("
        "{'ok': False, 'reason': 'corrupted', 'detail': json.dumps(sorted(os.environ))}))"
    )
    result = await _stub(code).extract("txt", b"")

    assert isinstance(result, ExtractionFailure)
    keys = set(json.loads(result.detail))
    assert "BOT_TOKEN" not in keys
    assert "DATABASE_URL" not in keys
    assert keys <= {"PATH", "PYTHONIOENCODING", "SYSTEMROOT", "LC_CTYPE"}


# --- Тайм-аут и уборка процессов -----------------------------------------------------------


async def test_hard_timeout_kills_and_reaps_process() -> None:
    started = time.monotonic()
    run = await run_worker(
        [sys.executable, "-I", "-c", "import time; time.sleep(60)"], b"", timeout_seconds=1.5
    )

    assert run.timed_out
    assert time.monotonic() - started < 10
    if LINUX:
        assert run.returncode == -9  # SIGKILL
    assert not _process_exists(run.pid)


async def test_timeout_is_classified_without_retry() -> None:
    result = await _stub("import time; time.sleep(60)", hard_timeout=1.0).extract("pdf", b"")

    assert isinstance(result, ExtractionFailure)
    assert result.reason is R.TIMEOUT


async def test_timeout_with_unread_large_input() -> None:
    """Процесс не читает stdin, а вход — 20 МиБ: запись в канал не должна зависнуть."""
    started = time.monotonic()
    run = await run_worker(
        [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
        b"x" * (20 * 1024 * 1024),
        timeout_seconds=1.5,
    )

    assert run.timed_out
    assert time.monotonic() - started < 10
    assert not _process_exists(run.pid)


async def test_stdout_flood_is_cut_and_process_reaped() -> None:
    code = (
        "import sys\n"
        "chunk = b'x' * (1024 * 1024)\n"
        "for _ in range(300):\n"
        "    sys.stdout.buffer.write(chunk)\n"
    )
    started = time.monotonic()
    run = await run_worker([sys.executable, "-I", "-c", code], b"", timeout_seconds=30)

    assert run.output_too_large
    assert len(run.stdout) == 0
    assert time.monotonic() - started < 10
    assert not _process_exists(run.pid)


async def test_stdout_flood_is_extractor_error() -> None:
    code = "import sys\nwhile True: sys.stdout.buffer.write(b'x' * 65536)"
    result = await _stub(code).extract("pdf", b"")

    assert isinstance(result, ExtractionFailure)
    assert result.reason is R.EXTRACTOR_ERROR


async def test_stderr_flood_does_not_fail_extraction() -> None:
    code = (
        "import sys, json\n"
        "sys.stderr.write('w' * (5 * 1024 * 1024))\n"
        "print(json.dumps({'ok': True, 'text': 'ok', 'extractor': 'stub', 'truncation': None,"
        " 'page_count': None, 'pages_processed': None, 'warnings': []}))"
    )
    run_result = await _stub(code).extract("txt", b"")

    assert isinstance(run_result, ExtractionResult)
    assert run_result.text == "ok"


async def test_cancellation_kills_child() -> None:
    pids: list[int] = []
    original = asyncio.create_subprocess_exec

    async def spy(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        process = await original(*args, **kwargs)  # type: ignore[arg-type]
        pids.append(process.pid)
        return process

    extractor = _stub("import time; time.sleep(60)")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(asyncio, "create_subprocess_exec", spy)
        task = asyncio.ensure_future(extractor.extract("pdf", b""))
        await asyncio.sleep(1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(pids) == 1
    assert not _process_exists(pids[0])


# --- Аварийные завершения и протокол -------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "import os; os._exit(3)",
        "import ctypes; ctypes.string_at(0)",
        "raise SystemExit(1)",
    ],
    ids=["exit-code", "segfault", "python-error"],
)
async def test_crash_is_extractor_crash(code: str) -> None:
    result = await _stub(code).extract("pdf", b"")

    assert isinstance(result, ExtractionFailure)
    assert result.reason is R.EXTRACTOR_CRASH


@pytest.mark.parametrize(
    "code",
    [
        "print('not json')",
        "pass",
        'print(\'{"ok": true, "text": 5}\')',
        'print(\'{"ok": false, "reason": "storage_error", "detail": "x"}\')',
    ],
    ids=["garbage", "silent", "wrong-types", "parent-reason"],
)
async def test_bad_output_is_extractor_error(code: str) -> None:
    result = await _stub(code).extract("pdf", b"")

    assert isinstance(result, ExtractionFailure)
    assert result.reason is R.EXTRACTOR_ERROR


async def test_spawn_failure_is_extractor_unavailable(tmp_path: Path) -> None:
    extractor = SubprocessTextExtractor(worker_command=[str(tmp_path / "no-such-python")])
    result = await extractor.extract("pdf", b"")

    assert isinstance(result, ExtractionFailure)
    assert result.reason is R.EXTRACTOR_UNAVAILABLE


@pytest.mark.skipif(not LINUX, reason="RLIMIT_AS есть только на Linux")
async def test_memory_limit_in_child() -> None:
    code = (
        "import json, sys\n"
        "from app.extraction.worker import _limit_resources\n"
        "_limit_resources(int(sys.argv[3]))\n"
        "try:\n"
        "    block = bytearray(512 * 1024 * 1024)\n"
        "    reason = 'none'\n"
        "except MemoryError:\n"
        "    reason = 'limit_exceeded'\n"
        "print(json.dumps({'ok': False, 'reason': reason if reason != 'none' else 'corrupted',"
        " 'detail': reason}))"
    )
    result = await _stub(code, memory_limit_bytes=256 * 1024 * 1024).extract("pdf", b"")

    assert isinstance(result, ExtractionFailure)
    assert result.reason is R.LIMIT_EXCEEDED


# --- Очередь ------------------------------------------------------------------------------


async def test_queue_allows_one_extraction_at_a_time() -> None:
    extractor = SubprocessTextExtractor()
    active = peak = 0

    async def job() -> None:
        nonlocal active, peak
        async with extractor.slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1

    await asyncio.gather(*(job() for _ in range(5)))

    assert peak == 1


async def test_queue_timeout_raises_busy() -> None:
    extractor = SubprocessTextExtractor(queue_timeout=0.1)

    async with extractor.slot():
        with pytest.raises(ExtractorBusyError):
            async with extractor.slot():
                pass
    # Место освобождается и после отказа.
    async with extractor.slot():
        pass
