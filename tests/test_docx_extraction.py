"""Извлечение текста из DOCX (lxml) на синтетических образцах.

Модуль вызывается напрямую, без дочернего процесса. Коды ошибок libxml2 зависят
от версии, встроенной в колесо lxml (на Linux и Windows разные): после
обновления lxml эти тесты нужно повторить в Linux-контейнере python:3.12-slim.
"""

import contextlib
import io
import socket
import threading
import time
import zipfile
from dataclasses import replace

import pytest

from app.domain.enums import MaterialFailureReason, TextTruncationReason
from app.extraction.base import ExtractionFailure, ExtractionLimits, ExtractionResult, WarningCode
from app.extraction.docx_text import extract_docx_text
from docx_samples import (
    body,
    complex_docx,
    doctype_docx,
    lying_size_docx,
    make_docx_parts,
    many_empty_paragraphs,
    many_foreign_elements,
    p,
    zip_bomb_docx,
)
from material_samples import DOCX_BYTES
from pdf_samples import RU_LINES, fixture

R = MaterialFailureReason
LIMITS = ExtractionLimits()
MiB = 1024 * 1024


def _extract(data: bytes, limits: ExtractionLimits = LIMITS) -> ExtractionResult:
    result = extract_docx_text(data, limits, deadline=time.monotonic() + limits.soft_budget_seconds)
    assert isinstance(result, ExtractionResult), result
    return result


def _fail(data: bytes, limits: ExtractionLimits = LIMITS) -> ExtractionFailure:
    result = extract_docx_text(data, limits, deadline=time.monotonic() + limits.soft_budget_seconds)
    assert isinstance(result, ExtractionFailure), result
    return result


# --- Текст ------------------------------------------------------------------------------


def test_minimal_docx() -> None:
    result = _extract(DOCX_BYTES)

    assert result.text == "Задание на практику"
    assert result.extractor.startswith("lxml 6.1.3 / libxml2 ")
    assert result.page_count is None


def test_word_like_docx_with_table_and_header_footer() -> None:
    result = _extract(fixture("docx/ru.docx"))

    lines = result.text.split("\n")
    assert lines[0] == "Колонтитул: кафедра информатики"
    assert lines[1] == "Задание на практику"
    assert lines[2:7] == RU_LINES
    assert lines[7:11] == ["Этап", "Срок", "Анализ «Ёлки»", "№ 3 — июль"]
    assert lines[-1] == "Нижний колонтитул"


def test_structure_order_and_no_duplicates() -> None:
    """Таблицы, вложенные таблицы, надписи, Choice/Fallback, одинаковые колонтитулы."""
    result = _extract(complex_docx())

    assert result.text.split("\n") == [
        "Верхний колонтитул",
        "Абзац 1.",
        "Ячейка A",
        "Вложенная ячейка",
        "Ячейка B",
        "Альт: выбор",
        # Надпись выводится перед абзацем, в котором она стоит.
        "ТБ в абзаце",
        "Перед надписью. После надписи.",
        "Ёлка «кавычки» — тире № 5\tпосле табуляции",
        "новая строкакто-то",
        "Абзац последний.",
        "Нижний колонтитул",
    ]
    assert "запас" not in result.text
    assert "копия VML" not in result.text
    assert "УДАЛЁННЫЙ" not in result.text
    assert "PAGE" not in result.text


def test_empty_body() -> None:
    result = _extract(make_docx_parts(body()))

    assert result.text == ""
    assert result.has_warning(WarningCode.NO_TEXT)


def test_utf16_document() -> None:
    xml = body(p("Текст в UTF-16")).replace('encoding="UTF-8"', 'encoding="UTF-16"')
    result = _extract(make_docx_parts(xml.encode("utf-16")))

    assert result.text == "Текст в UTF-16"


def test_external_and_escaping_relationships_are_ignored() -> None:
    from docx_samples import document_rels, header_part

    data = make_docx_parts(
        body(p("Текст")),
        {
            "word/_rels/document.xml.rels": document_rels(
                ("rId1", "header", "../../outside.xml"),
                ("rId2", "header", "missing.xml"),
            ),
            "outside.xml": header_part("hdr", p("Чужая часть")),
        },
    )
    assert _extract(data).text == "Текст"


# --- Повреждения и защита -----------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        b"PK\x03\x04 not really a zip",
        b"plain bytes",
        DOCX_BYTES[: len(DOCX_BYTES) // 2],
        make_docx_parts("<w:document><w:body><w:p>"),
        make_docx_parts(body(p("x")).replace("</w:body>", "</w:bodyX>")),
    ],
    ids=["zip-header-only", "not-zip", "truncated", "unclosed-xml", "mismatched-tag"],
)
def test_corrupted(data: bytes) -> None:
    assert _fail(data).reason is R.CORRUPTED


def test_missing_document_part() -> None:
    data = make_docx_parts(body(p("x")))
    archive = zipfile.ZipFile(io.BytesIO(data))
    rebuilt = io.BytesIO()
    with zipfile.ZipFile(rebuilt, "w") as out:
        for info in archive.infolist():
            if info.filename != "word/document.xml":
                out.writestr(info, archive.read(info))
    assert _fail(rebuilt.getvalue()).reason is R.CORRUPTED


def test_duplicate_part_names() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", body(p("один")))
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("word/document.xml", body(p("два")))
    failure = _fail(buffer.getvalue())

    assert failure.reason is R.CORRUPTED
    assert failure.detail == "duplicate_names"


def test_encrypted_zip_entry() -> None:
    data = bytearray(make_docx_parts(body(p("x"))))
    # Флаг шифрования (бит 0) в заголовках центрального каталога.
    position = data.find(b"PK\x01\x02")
    while position != -1:
        data[position + 8] |= 0x1
        position = data.find(b"PK\x01\x02", position + 4)
    assert _fail(bytes(data)).reason is R.ENCRYPTED


def test_unsupported_compression() -> None:
    data = make_docx_parts(body(p("x")), compression=zipfile.ZIP_BZIP2)
    assert _fail(data).reason is R.UNSUPPORTED


@pytest.mark.parametrize(
    ("declaration", "content"),
    [
        ('<!DOCTYPE d [<!ENTITY ent "внутренняя">]>', "&ent;"),
        ('<!DOCTYPE d [<!ENTITY ent SYSTEM "file:///etc/hostname">]>', "&ent;"),
        ('<!DOCTYPE d [<!ENTITY ent SYSTEM "http://127.0.0.1:9/x">]>', "&ent;"),
        ('<!DOCTYPE d SYSTEM "http://127.0.0.1:9/x.dtd">', "текст"),
        (
            '<!DOCTYPE d [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">'
            '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;">]>',
            "&c;",
        ),
        ("<!--" + "x" * 12_000 + '--><!DOCTYPE d [<!ENTITY ent "x">]>', "&ent;"),
    ],
    ids=["internal", "external-file", "external-http", "external-dtd", "laughs", "late"],
)
def test_doctype_is_rejected(declaration: str, content: str) -> None:
    failure = _fail(doctype_docx(declaration, content))

    assert failure.reason is R.UNSUPPORTED
    assert failure.detail.startswith("doctype:")


def test_doctype_in_utf16_is_rejected() -> None:
    xml = (
        '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE d [<!ENTITY e "x">]>'
        + body(p("&e;")).split("?>", 1)[1]
    )
    assert _fail(make_docx_parts(xml.encode("utf-16"))).reason is R.UNSUPPORTED


def test_entities_never_touch_network() -> None:
    """Разбор с внешними сущностями и DTD не открывает ни одного соединения."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    server.settimeout(0.2)
    port = server.getsockname()[1]
    connections: list[object] = []

    def accept() -> None:
        with contextlib.suppress(OSError):
            connections.append(server.accept())

    thread = threading.Thread(target=accept)
    thread.start()
    try:
        for declaration in (
            f'<!DOCTYPE d [<!ENTITY ent SYSTEM "http://127.0.0.1:{port}/e">]>',
            f'<!DOCTYPE d SYSTEM "http://127.0.0.1:{port}/d.dtd">',
        ):
            assert _fail(doctype_docx(declaration)).reason is R.UNSUPPORTED
        # Без побайтной проверки: парсер сам по себе не раскрывает сущности и не ходит в сеть.
        from lxml import etree

        xml = (
            f'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY ent SYSTEM "http://127.0.0.1:{port}/e">]>'
            "<d>&ent;</d>"
        ).encode()
        for _, elem in etree.iterparse(
            io.BytesIO(xml),
            resolve_entities=False,
            no_network=True,
            load_dtd=False,
            huge_tree=False,
        ):
            assert "127.0.0.1" not in (elem.text or "")
    finally:
        thread.join()
        server.close()
    assert connections == []


# --- Лимиты -----------------------------------------------------------------------------


def test_zip_bomb_is_rejected_without_decompression() -> None:
    data = zip_bomb_docx()
    assert len(data) < 1 * MiB
    started = time.monotonic()
    failure = _fail(data)

    assert failure.reason is R.LIMIT_EXCEEDED
    assert failure.detail.startswith("declared_total:")
    assert time.monotonic() - started < 1


def test_too_many_entries() -> None:
    extra = {f"word/media/{i}.bin": b"" for i in range(1001)}
    failure = _fail(make_docx_parts(body(p("x")), extra))

    assert failure.reason is R.LIMIT_EXCEEDED
    assert failure.detail.startswith("entries:")


def test_declared_part_size_limit() -> None:
    data = make_docx_parts(body(p("x" * (2 * MiB))))
    failure = _fail(data, replace(LIMITS, max_xml_part=1 * MiB))

    assert failure.reason is R.LIMIT_EXCEEDED
    assert failure.detail.startswith("part_declared_size:word/document.xml")


def test_default_part_limit_is_16_mib() -> None:
    data = make_docx_parts(body(*(p("x" * 1000) for _ in range(17 * 1024))))
    failure = _fail(data)

    assert failure.reason is R.LIMIT_EXCEEDED
    assert failure.detail.startswith("part_declared_size:")


def test_lying_declared_size_is_corrupted() -> None:
    assert _fail(lying_size_docx(declared=1000, actual_paragraphs=500)).reason is R.CORRUPTED


def test_read_total_limit_covers_all_parsed_parts() -> None:
    from docx_samples import document_rels, header_part

    big = header_part(
        "hdr", *(f"<w:p><w:r><w:instrText>{'i' * 1000}</w:instrText></w:r></w:p>",) * 900
    )
    data = make_docx_parts(
        body(p("x")),
        {
            "word/_rels/document.xml.rels": document_rels(
                ("rId1", "header", "header1.xml"),
                ("rId2", "header", "header2.xml"),
                ("rId3", "footer", "footer1.xml"),
            ),
            "word/header1.xml": big,
            "word/header2.xml": big.replace("hdr", "hdr "),
            "word/footer1.xml": big.replace("hdr", "ftr"),
        },
    )
    failure = _fail(data, replace(LIMITS, max_xml_read_total=2 * MiB))

    assert failure.reason is R.LIMIT_EXCEEDED
    assert failure.detail == "read_total"


@pytest.mark.parametrize(
    "xml",
    [
        body(p("x" * (11 * 1000 * 1000))),
        body("<w:sdt>" * 300 + p("x") + "</w:sdt>" * 300),
    ],
    ids=["text-node-over-10mb", "nesting-300"],
)
def test_xml_parser_limits(xml: str) -> None:
    failure = _fail(make_docx_parts(xml))

    assert failure.reason is R.LIMIT_EXCEEDED
    assert failure.detail.startswith("xml_parser_limit:")


def test_char_limit() -> None:
    data = make_docx_parts(body(*(p("слово " * 100) for _ in range(2000))))
    result = _extract(data)

    assert len(result.text) == 500_000
    assert result.truncation is TextTruncationReason.CHAR_LIMIT


def test_soft_budget_stops_during_initial_parse() -> None:
    """Бюджет проверяется и во время первичного разбора, а не только после него."""
    result = _extract(many_empty_paragraphs(500_000), replace(LIMITS, soft_budget_seconds=0.05))

    assert result.truncation is TextTruncationReason.TIME_BUDGET


def test_million_empty_paragraphs_do_not_hang() -> None:
    started = time.monotonic()
    result = _extract(many_empty_paragraphs(1_000_000))

    assert result.text == "конец"
    assert result.truncation is None
    assert time.monotonic() - started < 15


def test_million_foreign_elements() -> None:
    result = _extract(many_foreign_elements(1_000_000))

    assert result.text == "конец"
