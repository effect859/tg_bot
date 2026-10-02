"""Доменные правила материалов заказа: какие файлы принимаются и как меняется их статус.

Модуль чистый (без БД и Telegram) и работает только с метаданными файла.
check_material проверяет согласованность переданных метаданных (имя, заявленный
MIME, размер), но не содержимое: клиент может прислать что угодно под именем
``x.pdf``. Минимальная проверка фактического содержимого — в
app.domain.material_content, она выполняется при приёме файла, до создания материала.
"""

from collections.abc import Iterable
from datetime import timedelta
from enum import StrEnum

from app.domain.enums import MaterialFailureReason, MaterialProcessingStatus, OrderStatus
from app.domain.errors import (
    InvalidMaterialTransitionError,
    MaterialRejectReason,
    MaterialsNotReadyError,
    MaterialsNotReadyReason,
    UnsupportedMaterialError,
)

M = MaterialProcessingStatus

MAX_MATERIAL_SIZE_BYTES = 20 * 1024 * 1024
MAX_MATERIALS_PER_ORDER = 20
MAX_FILENAME_LENGTH = 255

# Расширение -> MIME-типы, которые клиент может заявить для такого файла.
SUPPORTED_MATERIAL_TYPES: dict[str, frozenset[str]] = {
    "pdf": frozenset({"application/pdf"}),
    "docx": frozenset({"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}),
    "txt": frozenset({"text/plain"}),
}
# Клиент не знает тип файла — решает расширение.
UNKNOWN_MIME_TYPES: frozenset[str] = frozenset({"application/octet-stream"})

# Материалы принимаются только на шаге загрузки: после согласия и до того, как
# пользователь завершит приём (finish_materials). Набор задаёт и то, где материалы
# обрабатываются и восстанавливаются: завершить приём можно только когда обработка
# всех материалов закончена, поэтому дальше обрабатывать нечего. Дополнительно
# add_material запрещает добавление после подтверждения ТЗ — защита на случай, если
# ТЗ подтверждено раньше завершения приёма.
MATERIAL_ACCEPTING_STATUSES: frozenset[OrderStatus] = frozenset({OrderStatus.WAITING_FOR_FILES})

MATERIAL_TRANSITIONS: dict[MaterialProcessingStatus, frozenset[MaterialProcessingStatus]] = {
    M.RECEIVED: frozenset({M.PROCESSING}),
    M.PROCESSING: frozenset({M.PROCESSED, M.FAILED}),
    # Повторная обработка после сбоя.
    M.FAILED: frozenset({M.PROCESSING}),
    M.PROCESSED: frozenset(),
}

R = MaterialFailureReason

# Сколько раз материал можно взять в обработку (первая попытка и повторы).
MAX_PROCESSING_ATTEMPTS = 3
# Повтор имеет смысл только для сбоев, не связанных с содержимым файла. Остальные
# причины (повреждён, зашифрован, тайм-аут, аварийное завершение, отказ БД принять
# результат) повторяются с тем же исходом: пользователь заменяет файл новым.
RETRYABLE_FAILURE_REASONS: frozenset[MaterialFailureReason] = frozenset(
    {R.STORAGE_ERROR, R.EXTRACTOR_UNAVAILABLE, R.INTERRUPTED, R.DATABASE_ERROR}
)
# Материал считается прерванным, если он в PROCESSING дольше этого времени с начала
# текущей попытки (processing_started_at). Момент начала ставится при захвате, когда
# место в очереди извлечения уже получено, — ожидание очереди сюда не входит. Попытка
# укладывается в чтение файла, жёсткий тайм-аут дочернего процесса (30 с), дочитывание
# его каналов (до 10 с) и сохранение; остальное — запас. Прерванный материал находит
# следующая обработка материалов заказа или проход восстановления
# (app.services.material_recovery): переводит его в FAILED с причиной INTERRUPTED и,
# если заказ ещё принимает материалы (MATERIAL_ACCEPTING_STATUSES) и попытки не
# исчерпаны, берёт заново.
STALE_PROCESSING_AFTER = timedelta(minutes=3)
# Проход восстановления берёт RECEIVED только старше этого: свежий материал
# обрабатывает апдейт, в котором его прислали (он может ждать очередь извлечения до
# 60 с), и восстановление не должно его перехватывать.
RECEIVED_GRACE = timedelta(minutes=2)


def check_material(filename: str, mime_type: str | None, size_bytes: int | None) -> str:
    """Проверяет метаданные файла и возвращает нормализованное расширение (``"pdf"``).

    ``size_bytes=None`` — размер ещё неизвестен (Telegram не сообщил его до скачивания):
    проверка размера пропускается, её нужно повторить по фактическим байтам.

    Raises UnsupportedMaterialError.
    """
    name = filename.strip()
    if not name or len(name) > MAX_FILENAME_LENGTH:
        raise UnsupportedMaterialError(
            f"Invalid filename length: {filename!r}", reason=MaterialRejectReason.FILENAME
        )
    stem, dot, extension = name.rpartition(".")
    extension = extension.lower()
    if not dot or not stem or extension not in SUPPORTED_MATERIAL_TYPES:
        raise UnsupportedMaterialError(
            f"Unsupported file type: {filename!r}", reason=MaterialRejectReason.FORMAT
        )

    if mime_type is not None:
        # "text/plain; charset=utf-8" -> "text/plain"
        mime = mime_type.split(";", 1)[0].strip().lower()
        if mime not in UNKNOWN_MIME_TYPES and mime not in SUPPORTED_MATERIAL_TYPES[extension]:
            raise UnsupportedMaterialError(
                f"MIME type {mime_type!r} does not match extension {extension!r}",
                reason=MaterialRejectReason.MIME,
            )

    if size_bytes is not None and size_bytes <= 0:
        raise UnsupportedMaterialError(
            f"File size {size_bytes} is not positive", reason=MaterialRejectReason.EMPTY
        )
    if size_bytes is not None and size_bytes > MAX_MATERIAL_SIZE_BYTES:
        raise UnsupportedMaterialError(
            f"File size {size_bytes} exceeds the limit", reason=MaterialRejectReason.SIZE
        )
    return extension


def is_supported_material(filename: str, mime_type: str | None, size_bytes: int | None) -> bool:
    try:
        check_material(filename, mime_type, size_bytes)
    except UnsupportedMaterialError:
        return False
    return True


def is_retryable_failure(reason: MaterialFailureReason | None, attempts: int) -> bool:
    return reason in RETRYABLE_FAILURE_REASONS and attempts < MAX_PROCESSING_ATTEMPTS


class MaterialCompletionState(StrEnum):
    """Состояние материала с точки зрения завершения приёма."""

    # Обработка не закончена: новый, в работе или ждёт повтора.
    PENDING = "pending"
    WITH_TEXT = "with_text"
    # Обработан, но текста нет (скан, изображения) — для анализа непригоден.
    WITHOUT_TEXT = "without_text"
    # Окончательный сбой: причина без повтора или попытки исчерпаны.
    FAILED = "failed"


def material_completion_state(
    status: MaterialProcessingStatus,
    failure_reason: MaterialFailureReason | None,
    attempts: int,
    char_count: int | None,
) -> MaterialCompletionState:
    """``char_count`` — длина сохранённого текста; None — строки текста нет.

    PENDING — ровно то, что ещё может взять обработка или восстановление: RECEIVED,
    PROCESSING (в работе или прерванный) и FAILED, который будет повторён.
    """
    if status is M.PROCESSED:
        if char_count:
            return MaterialCompletionState.WITH_TEXT
        return MaterialCompletionState.WITHOUT_TEXT
    if status is M.FAILED and not is_retryable_failure(failure_reason, attempts):
        return MaterialCompletionState.FAILED
    return MaterialCompletionState.PENDING


def check_materials_ready(states: Iterable[MaterialCompletionState]) -> None:
    """Можно ли завершить приём материалов: есть материалы, все обработаны, есть текст.

    Raises MaterialsNotReadyError.
    """
    states = list(states)
    if not states:
        raise MaterialsNotReadyError(MaterialsNotReadyReason.NO_MATERIALS)
    pending = states.count(MaterialCompletionState.PENDING)
    if pending:
        raise MaterialsNotReadyError(MaterialsNotReadyReason.PENDING, pending=pending)
    if MaterialCompletionState.WITH_TEXT not in states:
        raise MaterialsNotReadyError(MaterialsNotReadyReason.NO_TEXT)


def check_material_transition(
    current: MaterialProcessingStatus, target: MaterialProcessingStatus
) -> None:
    if target not in MATERIAL_TRANSITIONS[current]:
        raise InvalidMaterialTransitionError(current, target)
