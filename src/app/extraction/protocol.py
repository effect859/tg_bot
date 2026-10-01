"""Обмен с дочерним процессом: результат извлечения ⇄ одна JSON-строка в stdout.

Вывод дочернего процесса — недоверенные данные: он разбирал недоверенный файл.
decode_extraction проверяет типы, значения и лимиты и не доверяет ничему, что
не прошло проверку.
"""

import json
from typing import Any

from app.domain.enums import MaterialFailureReason, TextTruncationReason
from app.extraction.base import (
    Extraction,
    ExtractionFailure,
    ExtractionLimits,
    ExtractionResult,
    ExtractionWarning,
    WarningCode,
)

MAX_DETAIL_LENGTH = 500
MAX_EXTRACTOR_LENGTH = 255

R = MaterialFailureReason

# Причины, которые может сообщить дочерний процесс. Остальные (хранилище, тайм-аут,
# аварийное завершение, прерванная обработка) определяет только родитель: иначе
# дочерний процесс мог бы, например, вызвать повторы, назвав повторяемую причину.
WORKER_FAILURE_REASONS: frozenset[MaterialFailureReason] = frozenset(
    {
        R.CORRUPTED,
        R.ENCRYPTED,
        R.EMPTY_DOCUMENT,
        R.UNSUPPORTED,
        R.LIMIT_EXCEEDED,
        R.EXTRACTOR_ERROR,
    }
)


class ProtocolError(Exception):
    """Ответ дочернего процесса не соответствует протоколу."""


def encode_extraction(extraction: Extraction) -> bytes:
    if isinstance(extraction, ExtractionFailure):
        payload: dict[str, Any] = {
            "ok": False,
            "reason": extraction.reason.value,
            "detail": extraction.detail[:MAX_DETAIL_LENGTH],
        }
    else:
        payload = {
            "ok": True,
            "text": extraction.text,
            "extractor": extraction.extractor,
            "truncation": extraction.truncation.value if extraction.truncation else None,
            "page_count": extraction.page_count,
            "pages_processed": extraction.pages_processed,
            "warnings": [
                {"code": w.code.value, "pages": list(w.pages)} for w in extraction.warnings
            ],
        }
    # ensure_ascii: вывод — чистый ASCII, кодировка stdout и одиночные суррогаты
    # в строках исключений не могут его сломать.
    return json.dumps(payload, ensure_ascii=True).encode("ascii") + b"\n"


def decode_extraction(raw: bytes, limits: ExtractionLimits) -> Extraction:
    try:
        payload = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f"Invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("Payload is not an object")
    try:
        if payload.get("ok") is False:
            reason = MaterialFailureReason(_typed(payload, "reason", str))
            if reason not in WORKER_FAILURE_REASONS:
                raise ProtocolError(f"Reason is not reported by the worker: {reason.value}")
            return ExtractionFailure(reason, _typed(payload, "detail", str)[:MAX_DETAIL_LENGTH])
        if payload.get("ok") is not True:
            raise ProtocolError("Missing ok flag")
        return _decode_result(payload, limits)
    except (ValueError, TypeError) as exc:
        raise ProtocolError(str(exc)) from exc


def _decode_result(payload: dict[str, Any], limits: ExtractionLimits) -> ExtractionResult:
    text = _typed(payload, "text", str)
    if len(text) > limits.max_chars:
        raise ProtocolError(f"Text is longer than {limits.max_chars} characters")
    if "\x00" in text:
        raise ProtocolError("Text contains NUL")
    # Одиночные суррогаты не кодируются в UTF-8 и не сохранятся в PostgreSQL.
    text.encode("utf-8")

    extractor = _typed(payload, "extractor", str)
    if not extractor or len(extractor) > MAX_EXTRACTOR_LENGTH:
        raise ProtocolError("Invalid extractor")

    truncation = payload.get("truncation")
    page_count = _optional_int(payload, "page_count")
    pages_processed = _optional_int(payload, "pages_processed")
    if (page_count is None) != (pages_processed is None):
        raise ProtocolError("page_count and pages_processed must be set together")
    if page_count is not None and not (0 <= pages_processed <= page_count):  # type: ignore[operator]
        raise ProtocolError("Invalid page counters")

    raw_warnings = payload.get("warnings")
    if not isinstance(raw_warnings, list):
        raise ProtocolError("warnings is not a list")
    warnings = []
    for item in raw_warnings:
        if not isinstance(item, dict):
            raise ProtocolError("Warning is not an object")
        pages = item.get("pages")
        if not isinstance(pages, list) or not all(
            type(p) is int and 1 <= p <= (page_count or 0) for p in pages
        ):
            raise ProtocolError("Invalid warning pages")
        warnings.append(ExtractionWarning(WarningCode(_typed(item, "code", str)), tuple(pages)))

    return ExtractionResult(
        text=text,
        extractor=extractor,
        truncation=None if truncation is None else TextTruncationReason(truncation),
        page_count=page_count,
        pages_processed=pages_processed,
        warnings=tuple(warnings),
    )


def _typed(payload: dict[str, Any], key: str, kind: type) -> Any:
    value = payload.get(key)
    if type(value) is not kind:
        raise ProtocolError(f"{key} must be {kind.__name__}")
    return value


def _optional_int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ProtocolError(f"{key} must be a non-negative int")
    return value
