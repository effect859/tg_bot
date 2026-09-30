import codecs

import pytest

from app.domain.errors import MaterialRejectReason, UnsupportedMaterialError
from app.domain.material_content import MAX_CONTENT_TYPES_BYTES, check_material_content
from material_samples import (
    DOCX_BYTES,
    PDF_BYTES,
    PNG_BYTES,
    TXT_BYTES,
    XLSX_MAIN,
    make_docx,
    make_zip,
)


@pytest.mark.parametrize(
    ("extension", "data"),
    [
        ("pdf", PDF_BYTES),
        # Мусор перед заголовком в пределах первого килобайта допустим.
        ("pdf", b"\r\n" * 100 + PDF_BYTES),
        ("docx", DOCX_BYTES),
        ("txt", TXT_BYTES),
        ("txt", "Отчёт\r\n\tпо практике\f".encode("utf-8-sig")),
        ("txt", "Отчёт по практике".encode("cp1251")),
        ("txt", codecs.BOM_UTF16_LE + "Отчёт по практике".encode("utf-16-le")),
        ("txt", codecs.BOM_UTF16_BE + "Отчёт по практике".encode("utf-16-be")),
        # Одиночный управляющий символ в длинном тексте — не повод отказать.
        ("txt", ("строка текста\n" * 100 + "\x1a").encode()),
    ],
)
def test_matching_content_is_accepted(extension: str, data: bytes) -> None:
    check_material_content(extension, data)


@pytest.mark.parametrize(
    ("extension", "data"),
    [
        # Содержимое не соответствует расширению.
        ("pdf", DOCX_BYTES),
        ("pdf", PNG_BYTES),
        ("pdf", TXT_BYTES),
        ("pdf", b"\x00" * 1024 + PDF_BYTES),
        ("docx", PDF_BYTES),
        ("docx", TXT_BYTES),
        ("docx", PNG_BYTES),
        ("txt", PNG_BYTES),
        ("txt", DOCX_BYTES),
        # ZIP-сигнатура без ZIP-структуры.
        ("docx", b"PK\x03\x04" + b"\x00" * 100),
        # Настоящий ZIP, но не DOCX.
        ("docx", make_zip({"readme.txt": "hello"})),
        # DOCX без обязательных частей.
        ("docx", make_docx(document=None)),
        ("docx", make_docx(content_types=None)),
        ("docx", make_docx(rels=None)),
        # Другой OOXML-пакет с тем же набором имён частей.
        ("docx", make_docx(main_content_type=XLSX_MAIN)),
        # Обрезанный архив.
        ("docx", DOCX_BYTES[: len(DOCX_BYTES) // 2]),
        # Бинарные данные под видом текста.
        ("txt", b"text\x00with nul"),
        ("txt", bytes(range(32)) * 10),
        ("txt", codecs.BOM_UTF16_LE + b"\x00\xd8"),  # непарный суррогат
    ],
)
def test_mismatching_content_is_rejected(extension: str, data: bytes) -> None:
    with pytest.raises(UnsupportedMaterialError) as exc_info:
        check_material_content(extension, data)
    assert exc_info.value.reason is MaterialRejectReason.CONTENT


def test_oversized_content_types_part_is_not_read() -> None:
    padding = " " * (MAX_CONTENT_TYPES_BYTES + 1)
    data = make_docx(content_types=padding)

    with pytest.raises(UnsupportedMaterialError, match="too large"):
        check_material_content("docx", data)
