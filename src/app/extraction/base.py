"""Контракт извлечения текста: лимиты, результат, отказ, интерфейс извлекателя."""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from app.domain.enums import MaterialFailureReason, TextTruncationReason

MiB = 1024 * 1024


@dataclass(frozen=True)
class ExtractionLimits:
    """Лимиты одного извлечения. Передаются в дочерний процесс как JSON."""

    # Символов текста на материал (после нормализации).
    max_chars: int = 500_000
    # PDF: обрабатываются только первые страницы, остальное — с пометкой page_limit.
    max_pdf_pages: int = 300
    # Мягкий бюджет: проверяется между страницами и порциями XML, результат частичный.
    soft_budget_seconds: float = 20.0
    # DOCX (ZIP): проверяются по центральному каталогу до распаковки.
    max_zip_entries: int = 1000
    max_zip_declared_total: int = 256 * MiB
    # DOCX: одна разбираемая XML-часть — заявленный и фактически распакованный размер.
    max_xml_part: int = 16 * MiB
    # DOCX: сумма фактически распакованных байт всех разбираемых частей.
    max_xml_read_total: int = 64 * MiB
    max_rels_part: int = 1 * MiB


class WarningCode(StrEnum):
    # Факты.
    NO_TEXT = "no_text"
    PAGES_WITHOUT_TEXT = "pages_without_text"
    # Эвристики: подсказка пользователю, а не причина отказа.
    IMAGE_ONLY_PAGES = "image_only_pages"
    LOW_QUALITY_TEXT = "low_quality_text"


class WarningKind(StrEnum):
    FACT = "fact"
    HEURISTIC = "heuristic"


WARNING_KINDS: dict[WarningCode, WarningKind] = {
    WarningCode.NO_TEXT: WarningKind.FACT,
    WarningCode.PAGES_WITHOUT_TEXT: WarningKind.FACT,
    WarningCode.IMAGE_ONLY_PAGES: WarningKind.HEURISTIC,
    WarningCode.LOW_QUALITY_TEXT: WarningKind.HEURISTIC,
}


@dataclass(frozen=True)
class ExtractionWarning:
    code: WarningCode
    # Номера страниц PDF с единицы; пусто — относится ко всему документу.
    pages: tuple[int, ...] = ()

    @property
    def kind(self) -> WarningKind:
        return WARNING_KINDS[self.code]

    def to_json(self) -> dict[str, object]:
        return {"code": self.code.value, "kind": self.kind.value, "pages": list(self.pages)}


@dataclass(frozen=True)
class ExtractionResult:
    """Текст извлечён (возможно, пустой или частичный — это не отказ)."""

    text: str
    extractor: str
    truncation: TextTruncationReason | None = None
    # Только для PDF.
    page_count: int | None = None
    pages_processed: int | None = None
    warnings: tuple[ExtractionWarning, ...] = field(default_factory=tuple)

    def has_warning(self, code: WarningCode) -> bool:
        return any(w.code is code for w in self.warnings)


@dataclass(frozen=True)
class ExtractionFailure:
    """Текст не получен. ``detail`` — для логов, пользователю не показывается."""

    reason: MaterialFailureReason
    detail: str


Extraction = ExtractionResult | ExtractionFailure


class ExtractorBusyError(Exception):
    """Очередь извлечения занята дольше допустимого: материал не взят в работу."""


class TextExtractor(Protocol):
    def slot(self) -> AbstractAsyncContextManager[None]:
        """Место в очереди извлечения; держится на всё время обработки материала.

        Raises ExtractorBusyError, если место не освободилось за отведённое время.
        """
        ...

    async def extract(self, extension: str, data: bytes) -> Extraction: ...
