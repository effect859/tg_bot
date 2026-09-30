"""Доменные правила материалов заказа: какие файлы принимаются и как меняется их статус.

Модуль чистый (без БД и Telegram) и работает только с метаданными файла.
check_material проверяет согласованность переданных метаданных (имя, заявленный
MIME, размер), но не содержимое: клиент может прислать что угодно под именем
``x.pdf``. Минимальная проверка фактического содержимого — в
app.domain.material_content, она выполняется при приёме файла, до создания материала.
"""

from app.domain.enums import MaterialProcessingStatus, OrderStatus
from app.domain.errors import (
    InvalidMaterialTransitionError,
    MaterialRejectReason,
    UnsupportedMaterialError,
)
from app.domain.order_flow import PRE_PAYMENT_STATUSES

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

# Материалы принимаются после согласия и до оплаты. Дополнительно add_material
# запрещает добавление после подтверждения ТЗ — это проверяется по данным в БД.
MATERIAL_ACCEPTING_STATUSES: frozenset[OrderStatus] = PRE_PAYMENT_STATUSES - {
    OrderStatus.NEW,
    OrderStatus.WAITING_FOR_CONSENT,
}

MATERIAL_TRANSITIONS: dict[MaterialProcessingStatus, frozenset[MaterialProcessingStatus]] = {
    M.RECEIVED: frozenset({M.PROCESSING}),
    M.PROCESSING: frozenset({M.PROCESSED, M.FAILED}),
    # Повторная обработка после сбоя.
    M.FAILED: frozenset({M.PROCESSING}),
    M.PROCESSED: frozenset(),
}


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


def check_material_transition(
    current: MaterialProcessingStatus, target: MaterialProcessingStatus
) -> None:
    if target not in MATERIAL_TRANSITIONS[current]:
        raise InvalidMaterialTransitionError(current, target)
