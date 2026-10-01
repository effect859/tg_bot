"""Извлечение текста из PDF (pypdfium2) на синтетических образцах.

Модуль вызывается напрямую, без дочернего процесса: здесь проверяется разбор,
изоляция — в test_extraction_runner.py.
"""

import time
from dataclasses import replace

import pytest

from app.domain.enums import MaterialFailureReason, TextTruncationReason
from app.extraction import pdf_text
from app.extraction.base import ExtractionFailure, ExtractionLimits, ExtractionResult, WarningCode
from app.extraction.pdf_text import extract_pdf_text
from pdf_samples import RU_LINES, fixture, make_pdf, zero_pages_pdf

R = MaterialFailureReason
LIMITS = ExtractionLimits()


def _extract(data: bytes, limits: ExtractionLimits = LIMITS) -> ExtractionResult:
    result = extract_pdf_text(data, limits, deadline=time.monotonic() + limits.soft_budget_seconds)
    assert isinstance(result, ExtractionResult), result
    return result


def _fail(data: bytes) -> ExtractionFailure:
    result = extract_pdf_text(data, LIMITS, deadline=time.monotonic() + 20)
    assert isinstance(result, ExtractionFailure), result
    return result


def test_russian_text() -> None:
    result = _extract(fixture("pdf/ru.pdf"))

    assert result.text == "\n".join(RU_LINES)
    assert result.page_count == result.pages_processed == 1
    assert result.truncation is None
    assert result.warnings == ()
    assert result.extractor.startswith("pypdfium2 5.13.0 / pdfium ")


def test_pages_are_separated_and_line_breaks_normalized() -> None:
    result = _extract(make_pdf([["Page one", "line two"], ["Page two"]]))

    assert result.text == "Page one\nline two\fPage two"
    assert "\r" not in result.text


def test_mixed_document_reports_pages_without_text() -> None:
    result = _extract(fixture("pdf/mixed_scan.pdf"))

    assert result.page_count == 4
    pages = result.text.split("\f")
    assert pages[0].startswith("Отчёт по производственной практике.")
    assert pages[1] == pages[2] == ""
    assert pages[3].startswith("Раздел 1.")
    by_code = {w.code: w.pages for w in result.warnings}
    # Факт: на страницах 2 и 3 текста нет. Эвристика: на 2-й только изображение.
    assert by_code == {
        WarningCode.PAGES_WITHOUT_TEXT: (2, 3),
        WarningCode.IMAGE_ONLY_PAGES: (2,),
    }


def test_image_only_document_is_not_an_error() -> None:
    result = _extract(fixture("pdf/image_only.pdf"))

    assert result.text == ""
    assert result.has_warning(WarningCode.NO_TEXT)
    assert result.has_warning(WarningCode.IMAGE_ONLY_PAGES)


def test_blank_pages_are_not_called_scans() -> None:
    """Пустая страница без изображений — «текста нет», но не «похоже на скан»."""
    result = _extract(make_pdf([[], []]))

    assert result.text == "\f"
    assert [w.code for w in result.warnings] == [WarningCode.NO_TEXT]


def test_font_without_tounicode_is_flagged() -> None:
    result = _extract(fixture("pdf/no_tounicode.pdf"))

    assert result.has_warning(WarningCode.LOW_QUALITY_TEXT)
    assert not result.has_warning(WarningCode.IMAGE_ONLY_PAGES)


def test_encrypted_with_user_password() -> None:
    assert _fail(fixture("pdf/enc_user_aes256.pdf")).reason is R.ENCRYPTED


def test_owner_password_only_opens() -> None:
    result = _extract(fixture("pdf/enc_owner_only_aes256.pdf"))
    assert result.text == "\n".join(RU_LINES)


@pytest.mark.parametrize(
    "data",
    [
        b"%PDF-1.7\n" + bytes(range(256)) * 50,
        b"",
        fixture("pdf/ru.pdf")[:5000],
    ],
    ids=["garbage", "empty-bytes", "truncated"],
)
def test_corrupted(data: bytes) -> None:
    assert _fail(data).reason is R.CORRUPTED


def test_zero_pages_is_not_guessed() -> None:
    """PDFium не открывает PDF без страниц и не сообщает причину: это «повреждён»."""
    assert _fail(zero_pages_pdf()).reason is R.CORRUPTED


def test_empty_document_when_library_reports_zero_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    import pypdfium2

    class _Empty:
        def __len__(self) -> int:
            return 0

        def close(self) -> None:
            pass

    monkeypatch.setattr(pypdfium2, "PdfDocument", lambda data: _Empty())

    assert _fail(b"%PDF-").reason is R.EMPTY_DOCUMENT


def test_page_limit() -> None:
    data = make_pdf([[f"Page {i + 1}"] for i in range(12)])
    result = _extract(data, replace(LIMITS, max_pdf_pages=10))

    assert result.page_count == 12
    assert result.pages_processed == 10
    assert result.truncation is TextTruncationReason.PAGE_LIMIT
    assert result.text.split("\f")[-1] == "Page 10"


def test_default_page_limit_is_300() -> None:
    data = make_pdf([[f"Page {i + 1}"] for i in range(305)])
    result = _extract(data)

    assert (result.page_count, result.pages_processed) == (305, 300)
    assert result.truncation is TextTruncationReason.PAGE_LIMIT


def test_char_limit_inside_page() -> None:
    line = "x" * 100
    data = make_pdf([[line] * 60], size=(2000, 3000), font_size=4)
    result = _extract(data, replace(LIMITS, max_chars=1000))

    assert len(result.text) <= 1000
    assert result.truncation is TextTruncationReason.CHAR_LIMIT


def test_char_limit_across_pages() -> None:
    data = make_pdf([["y" * 80] for _ in range(20)])
    result = _extract(data, replace(LIMITS, max_chars=500))

    assert len(result.text) <= 500
    assert result.pages_processed < 20
    assert result.truncation is TextTruncationReason.CHAR_LIMIT


def test_soft_time_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    data = make_pdf([[f"Page {i + 1}"] for i in range(5)])
    calls = iter(range(100))
    # Каждая проверка времени — на «секунду» позже; бюджет — 2 «секунды».
    monkeypatch.setattr(pdf_text.time, "monotonic", lambda: float(next(calls)))
    result = extract_pdf_text(data, LIMITS, deadline=2.0)

    assert isinstance(result, ExtractionResult)
    assert result.truncation is TextTruncationReason.TIME_BUDGET
    assert 0 < result.pages_processed < 5
