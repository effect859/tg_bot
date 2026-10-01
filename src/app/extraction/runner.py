"""Запуск извлечения в дочернем процессе: тайм-аут, ограничение вывода, уборка процесса.

* Процесс запускается через asyncio (без блокировки цикла событий) с флагом ``-I``
  и минимальным окружением: секреты бота в него не попадают.
* Жёсткий тайм-аут считается от запуска процесса; по нему процесс снимается
  (SIGKILL на Linux, TerminateProcess на Windows) и обязательно дожидается
  ``wait()``, чтобы не оставалось зомби.
* stdout читается с потолком (превышение — отказ), от stderr хранится только
  хвост: память родителя не растёт от вывода дочернего процесса. После снятия
  процесса каналы дочитываются в никуда — иначе ``wait()`` ждал бы конца данных
  в остановленном канале бесконечно.
* Одновременно работает не больше одного процесса (семафор на процесс бота).
  Это рассчитано на один экземпляр бота (polling).
"""

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from app.domain.enums import MaterialFailureReason
from app.extraction.base import Extraction, ExtractionFailure, ExtractionLimits, ExtractorBusyError
from app.extraction.protocol import ProtocolError, decode_extraction

logger = logging.getLogger(__name__)

R = MaterialFailureReason

HARD_TIMEOUT_SECONDS = 30.0
# Сколько материал ждёт места в очереди, прежде чем остаться необработанным.
QUEUE_TIMEOUT_SECONDS = 60.0
# 500 000 символов в JSON с \\uXXXX — до 3 МБ; потолок с запасом.
STDOUT_LIMIT_BYTES = 8 * 1024 * 1024
STDERR_LIMIT_BYTES = 64 * 1024
# Сколько ждать конца данных в каналах уже снятого процесса.
DRAIN_TIMEOUT_SECONDS = 10.0
READ_CHUNK = 64 * 1024
WORKER_MODULE = "app.extraction.worker"


class _OutputTooLarge(Exception):
    pass


@dataclass(frozen=True)
class WorkerRun:
    """Исход запуска дочернего процесса — до разбора его ответа."""

    pid: int
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_too_large: bool = False


def child_env() -> dict[str, str]:
    """Минимальное окружение дочернего процесса: только то, без чего не запустится Python."""
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"}
    if sys.platform == "win32":
        # Без SYSTEMROOT на Windows не загружается часть системных библиотек.
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return env


async def run_worker(
    argv: Sequence[str],
    data: bytes,
    *,
    timeout_seconds: float,
    stdout_limit: int = STDOUT_LIMIT_BYTES,
    stderr_limit: int = STDERR_LIMIT_BYTES,
) -> WorkerRun:
    """Запускает процесс, передаёт ``data`` в stdin и собирает вывод с ограничениями.

    Raises OSError, если процесс не удалось запустить.
    """
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=child_env(),
    )
    assert process.stdin and process.stdout and process.stderr

    async def feed() -> None:
        try:
            process.stdin.write(data)
            await process.stdin.drain()
        except OSError:
            # BrokenPipeError, ConnectionResetError: процесс завершился, не дочитав вход.
            # Исход покажут код выхода и stdout.
            pass
        finally:
            process.stdin.close()

    tasks = [
        asyncio.ensure_future(feed()),
        asyncio.ensure_future(_read_limited(process.stdout, stdout_limit)),
        asyncio.ensure_future(_read_tail(process.stderr, stderr_limit)),
    ]
    stdout = stderr = b""
    timed_out = too_large = False
    try:
        _, stdout, stderr, _ = await asyncio.wait_for(
            asyncio.gather(*tasks, process.wait()), timeout_seconds
        )
    except TimeoutError:
        timed_out = True
    except _OutputTooLarge:
        too_large = True
    finally:
        # Выполняется и при отмене вызывающей задачи: процесс не должен пережить её.
        for task in tasks:
            task.cancel()
        await _kill_and_reap(process)
    assert process.returncode is not None
    return WorkerRun(
        pid=process.pid,
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        output_too_large=too_large,
    )


async def _read_limited(stream: asyncio.StreamReader, limit: int) -> bytes:
    buffer = bytearray()
    while chunk := await stream.read(READ_CHUNK):
        if len(buffer) + len(chunk) > limit:
            raise _OutputTooLarge(limit)
        buffer += chunk
    return bytes(buffer)


async def _read_tail(stream: asyncio.StreamReader, limit: int) -> bytes:
    """stderr — только для диагностики: храним последние ``limit`` байт, не отказываем."""
    buffer = bytearray()
    while chunk := await stream.read(READ_CHUNK):
        buffer += chunk
        del buffer[:-limit]
    return bytes(buffer)


async def _discard(stream: asyncio.StreamReader) -> None:
    while await stream.read(READ_CHUNK):
        pass


async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    assert process.stdout and process.stderr
    # wait() ждёт и выхода процесса, и конца данных во всех каналах; канал, чтение
    # которого остановлено, стоит на паузе — дочитываем в никуда.
    try:
        await asyncio.wait_for(
            asyncio.gather(_discard(process.stdout), _discard(process.stderr)),
            DRAIN_TIMEOUT_SECONDS,
        )
    except (TimeoutError, OSError):
        logger.warning("Extraction worker pipes did not close: pid=%s", process.pid)
    await process.wait()


class SubprocessTextExtractor:
    """TextExtractor: каждый материал разбирается в отдельном дочернем процессе."""

    def __init__(
        self,
        limits: ExtractionLimits | None = None,
        *,
        memory_limit_bytes: int = 0,
        hard_timeout: float = HARD_TIMEOUT_SECONDS,
        queue_timeout: float = QUEUE_TIMEOUT_SECONDS,
        max_concurrency: int = 1,
        worker_command: Sequence[str] | None = None,
    ) -> None:
        self.limits = limits or ExtractionLimits()
        self._memory_limit = memory_limit_bytes
        self._hard_timeout = hard_timeout
        self._queue_timeout = queue_timeout
        self._semaphore = asyncio.Semaphore(max_concurrency)
        # Для тестов: заглушка вместо настоящего обработчика.
        self._worker_command = list(worker_command or [sys.executable, "-I", "-m", WORKER_MODULE])

    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        try:
            await asyncio.wait_for(self._semaphore.acquire(), self._queue_timeout)
        except TimeoutError as exc:
            raise ExtractorBusyError from exc
        try:
            yield
        finally:
            self._semaphore.release()

    async def extract(self, extension: str, data: bytes) -> Extraction:
        argv = [
            *self._worker_command,
            extension,
            json.dumps(dataclasses.asdict(self.limits)),
            str(self._memory_limit),
        ]
        try:
            run = await run_worker(argv, data, timeout_seconds=self._hard_timeout)
        except OSError as exc:
            logger.exception("Extraction worker failed to start")
            return ExtractionFailure(R.EXTRACTOR_UNAVAILABLE, f"spawn: {type(exc).__name__}")
        return classify_run(run, self.limits)


def classify_run(run: WorkerRun, limits: ExtractionLimits) -> Extraction:
    """Переводит исход процесса в результат или причину отказа."""
    if run.timed_out:
        return ExtractionFailure(R.TIMEOUT, f"killed after timeout, rc={run.returncode}")
    if run.output_too_large:
        return ExtractionFailure(R.EXTRACTOR_ERROR, "stdout limit exceeded")
    stderr_tail = run.stderr[-500:].decode("utf-8", "replace")
    if run.returncode != 0:
        # Сигнал (rc < 0: SIGSEGV, SIGABRT при нехватке памяти в PDFium, SIGXCPU) или
        # код выхода без результата. Нехватку памяти отсюда надёжно не отличить.
        logger.warning(
            "Extraction worker crashed: rc=%s stderr=%r", run.returncode, stderr_tail[-200:]
        )
        return ExtractionFailure(R.EXTRACTOR_CRASH, f"rc={run.returncode}")
    try:
        return decode_extraction(run.stdout.strip(), limits)
    except ProtocolError as exc:
        logger.warning("Extraction worker protocol error: %s stderr=%r", exc, stderr_tail[-200:])
        return ExtractionFailure(R.EXTRACTOR_ERROR, f"protocol: {exc}")
