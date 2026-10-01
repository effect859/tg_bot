"""Дочерний процесс извлечения текста.

Запуск: ``python -I -m app.extraction.worker <расширение> <лимиты JSON> <RLIMIT_AS, байт>``.
stdin — байты файла, stdout — одна JSON-строка (app.extraction.protocol).

Процесс разбирает недоверенный файл, поэтому:
* ограничения ресурсов ставятся до импорта парсеров и до чтения входа
  (только Linux: RLIMIT_AS — экспериментальное значение, RLIMIT_CPU — страховка);
* окружение задаёт родитель: секретов бота (BOT_TOKEN, DATABASE_URL) здесь нет;
* сбой парсера завершает только этот процесс. Аварийное завершение и зависание
  обрабатывает родитель (app.extraction.runner).
"""

import json
import sys
import time

from app.domain.enums import MaterialFailureReason
from app.domain.materials import MAX_MATERIAL_SIZE_BYTES
from app.extraction.base import Extraction, ExtractionFailure, ExtractionLimits
from app.extraction.protocol import MAX_DETAIL_LENGTH, encode_extraction

# Страховка на случай, если родитель не снимет процесс: чуть больше жёсткого тайм-аута.
CPU_LIMIT_SECONDS = 35


def main(argv: list[str]) -> None:
    started = time.monotonic()
    extension, limits_json, memory_limit = argv[0], argv[1], int(argv[2])
    _limit_resources(memory_limit)
    limits = ExtractionLimits(**json.loads(limits_json))
    try:
        # +1 байт: файл больше лимита не разбираем вовсе.
        data = sys.stdin.buffer.read(MAX_MATERIAL_SIZE_BYTES + 1)
    except MemoryError:
        # Буфер чтения выделяется сразу на весь размер: при малом RLIMIT_AS память
        # кончается ещё до разбора.
        result: Extraction = ExtractionFailure(
            MaterialFailureReason.LIMIT_EXCEEDED, "memory: input"
        )
    else:
        if len(data) > MAX_MATERIAL_SIZE_BYTES:
            result = ExtractionFailure(MaterialFailureReason.LIMIT_EXCEEDED, "input_size")
        else:
            deadline = started + limits.soft_budget_seconds
            result = _extract(extension, data, limits, deadline=deadline)
    sys.stdout.buffer.write(encode_extraction(result))
    sys.stdout.buffer.flush()


def _limit_resources(memory_limit: int) -> None:
    if not sys.platform.startswith("linux"):
        return  # На Windows модуля resource нет: ограничения памяти нет.
    import resource

    if memory_limit > 0:
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_LIMIT_SECONDS, CPU_LIMIT_SECONDS))


def _extract(
    extension: str, data: bytes, limits: ExtractionLimits, *, deadline: float
) -> Extraction:
    try:
        if extension == "pdf":
            from app.extraction.pdf_text import extract_pdf_text

            return extract_pdf_text(data, limits, deadline=deadline)
        if extension == "docx":
            from app.extraction.docx_text import extract_docx_text

            return extract_docx_text(data, limits, deadline=deadline)
        if extension == "txt":
            from app.extraction.txt_text import extract_txt_text

            return extract_txt_text(data, limits)
        return ExtractionFailure(MaterialFailureReason.UNSUPPORTED, f"extension:{extension[:16]}")
    except MemoryError:
        # PDFium при нехватке памяти обычно завершается аварийно (SIGABRT) и сюда не
        # доходит; MemoryError бывает в Python-коде и lxml.
        return ExtractionFailure(MaterialFailureReason.LIMIT_EXCEEDED, "memory")
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"[:MAX_DETAIL_LENGTH]
        return ExtractionFailure(MaterialFailureReason.EXTRACTOR_ERROR, detail)


if __name__ == "__main__":
    main(sys.argv[1:])
