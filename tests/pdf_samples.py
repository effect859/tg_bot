"""Синтетические PDF для тестов: генерируются через PDFium, только ASCII (шрифт Helvetica).

Русский текст — в готовых образцах tests/fixtures/pdf (см. README там).
"""

import ctypes
import io
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

FIXTURES = Path(__file__).parent / "fixtures"

RU_LINES = [
    "Отчёт по производственной практике.",
    "Студент ознакомился со структурой предприятия «Ёлка» — договор № 42.",
    "Задачи: анализ бизнес-процессов, подготовка предложений по оптимизации.",
    "Съешь же ещё этих мягких французских булок, да выпей чаю.",
    "Срок практики: с 01.07.2026 по 28.07.2026, руководитель — Иванов И. И.",
]


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def make_pdf(
    pages: list[list[str]], *, size: tuple[float, float] = (595, 842), font_size: float = 12
) -> bytes:
    """PDF, в котором каждая страница — строки текста; пустой список — пустая страница."""
    pdf = pdfium.PdfDocument.new()
    try:
        for lines in pages:
            page = pdf.new_page(*size)
            y = size[1] - 2 * font_size
            for line in lines:
                obj = pdfium_c.FPDFPageObj_NewTextObj(
                    pdf.raw, b"Helvetica", ctypes.c_float(font_size)
                )
                buffer = ctypes.create_string_buffer((line + "\x00").encode("utf-16-le"))
                pdfium_c.FPDFText_SetText(
                    obj, ctypes.cast(buffer, ctypes.POINTER(pdfium_c.FPDF_WCHAR))
                )
                pdfium_c.FPDFPageObj_Transform(obj, 1, 0, 0, 1, 10, y)
                pdfium_c.FPDFPage_InsertObject(page.raw, obj)
                y -= font_size * 1.2
            pdfium_c.FPDFPage_GenerateContent(page.raw)
            page.close()
        output = io.BytesIO()
        pdf.save(output)
        return output.getvalue()
    finally:
        pdf.close()


def zero_pages_pdf() -> bytes:
    pdf = pdfium.PdfDocument.new()
    try:
        output = io.BytesIO()
        pdf.save(output)
        return output.getvalue()
    finally:
        pdf.close()
