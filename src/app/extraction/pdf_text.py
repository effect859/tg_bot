"""Текст из PDF через pypdfium2 (PDFium).

Выполняется только в дочернем процессе (app.extraction.worker): PDFium написан на
C++, не потокобезопасен, и сбой в нём завершает процесс, а не бросает исключение.

Страницы обрабатываются по одной: лимиты страниц, символов и мягкий бюджет времени
проверяются между страницами, а лимит символов — ещё и внутри страницы. Одну
страницу посередине прервать нельзя — от этого защищает жёсткий тайм-аут родителя.

Скан не определяется по одному только пустому тексту: «страница без текста» —
факт, «только изображения» — эвристика по объектам страницы.
"""

import time

from app.domain.enums import MaterialFailureReason, TextTruncationReason
from app.extraction.base import (
    ExtractionFailure,
    ExtractionLimits,
    ExtractionResult,
    ExtractionWarning,
    WarningCode,
)
from app.extraction.normalize import PAGE_SEPARATOR, has_text, looks_unreadable, normalize_text

R = MaterialFailureReason


def extract_pdf_text(
    data: bytes, limits: ExtractionLimits, *, deadline: float
) -> ExtractionResult | ExtractionFailure:
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    extractor = f"pypdfium2 {pdfium.version.PYPDFIUM_INFO} / pdfium {pdfium.version.PDFIUM_INFO}"
    try:
        # Пустой пароль: PDF только с паролем владельца открывается без него.
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        if exc.err_code in (pdfium_c.FPDF_ERR_PASSWORD, pdfium_c.FPDF_ERR_SECURITY):
            return ExtractionFailure(R.ENCRYPTED, f"pdfium err_code={exc.err_code}")
        # Причину по остальным кодам (в том числе FPDF_ERR_SUCCESS у PDF без страниц)
        # PDFium надёжно не сообщает — не угадываем.
        return ExtractionFailure(R.CORRUPTED, f"pdfium err_code={exc.err_code}")

    try:
        page_count = len(pdf)
        if page_count == 0:
            return ExtractionFailure(R.EMPTY_DOCUMENT, "pdf has 0 pages")

        pages: list[str] = []
        chars = 0
        truncation: TextTruncationReason | None = None
        without_text: list[int] = []
        image_only: list[int] = []
        unreadable: list[int] = []

        for index in range(min(page_count, limits.max_pdf_pages)):
            if time.monotonic() > deadline:
                truncation = TextTruncationReason.TIME_BUDGET
                break
            room = limits.max_chars - chars
            if room <= 0:
                truncation = TextTruncationReason.CHAR_LIMIT
                break
            raw, images, text_objects, cut = _page_text(pdf, index, room, pdfium_c)
            text = normalize_text(raw)
            number = index + 1
            if not has_text(text):
                without_text.append(number)
                if images and not text_objects:
                    image_only.append(number)
            # Без ToUnicode PDFium отдаёт управляющие символы: после нормализации
            # страница может оказаться пустой, но причина — шрифт, а не скан.
            if looks_unreadable(raw):
                unreadable.append(number)
            cut = cut or len(text) > room
            text = text[:room]
            pages.append(text)
            chars += len(text) + len(PAGE_SEPARATOR)
            if cut:
                truncation = TextTruncationReason.CHAR_LIMIT
                break
        else:
            if page_count > limits.max_pdf_pages:
                truncation = TextTruncationReason.PAGE_LIMIT
    finally:
        pdf.close()

    full = PAGE_SEPARATOR.join(pages)[: limits.max_chars]
    warnings: list[ExtractionWarning] = []
    if not has_text(full):
        warnings.append(ExtractionWarning(WarningCode.NO_TEXT))
    elif without_text:
        warnings.append(ExtractionWarning(WarningCode.PAGES_WITHOUT_TEXT, tuple(without_text)))
    if image_only:
        warnings.append(ExtractionWarning(WarningCode.IMAGE_ONLY_PAGES, tuple(image_only)))
    if unreadable:
        warnings.append(ExtractionWarning(WarningCode.LOW_QUALITY_TEXT, tuple(unreadable)))
    return ExtractionResult(
        text=full,
        extractor=extractor,
        truncation=truncation,
        page_count=page_count,
        pages_processed=len(pages),
        warnings=tuple(warnings),
    )


def _page_text(pdf, index: int, room: int, pdfium_c) -> tuple[str, int, int, bool]:
    """Текст страницы (не больше ``room`` символов), число изображений и текстовых объектов."""
    page = pdf[index]
    try:
        images = sum(1 for _ in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE]))
        text_objects = sum(1 for _ in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_TEXT]))
        textpage = page.get_textpage()
        try:
            count = textpage.count_chars()
            take = min(count, room)
            raw = textpage.get_text_range(0, take) if take > 0 else ""
        finally:
            textpage.close()
    finally:
        page.close()
    return raw, images, text_objects, take < count
