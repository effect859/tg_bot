"""DOCX-рендерер отчёта: структура пакета, оформление ГОСТ, поля, текст, детерминизм.

Без БД и сети. Текст проверяется обратным извлечением существующим экстрактором
материалов (extract_docx_text, Stage 2.4) — независимым от python-docx.
"""

import inspect
import io
import posixpath
import time
import tracemalloc
import zipfile
from typing import Any

import pytest
from lxml import etree

from app.domain.enums import DocType
from app.domain.report_document import DocumentErrorCode, InvalidDocumentError
from app.domain.title_page import build_title_page
from app.extraction.base import ExtractionLimits, ExtractionResult
from app.extraction.docx_text import extract_docx_text
from app.generation.schemas import DocumentContent, Section
from app.rendering import docx as docx_module
from app.rendering.base import DocumentRenderer
from app.rendering.docx import (
    BLANK_LINE,
    DOCX_MIME_TYPE,
    GOST_7_32,
    REFERENCES_TITLE,
    PracticeReportDocxRenderer,
)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
LIMITS = ExtractionLimits()


def _w(name: str) -> str:
    return f"{{{W}}}{name}"


def _twips_from_mm(mm: float) -> int:
    return round(mm / 25.4 * 1440)


def _document(*sections: Section, title: str = "Анализ бизнес-процессов") -> DocumentContent:
    return DocumentContent(doc_type=DocType.PRACTICE_REPORT, title=title, sections=list(sections))


CONTENT = _document(
    Section(title="Теоретические основы", paragraphs=["Первый абзац.", "Второй абзац."]),
    Section(
        title="Анализ деятельности",
        paragraphs=["Вводный абзац раздела."],
        subsections=[
            Section(title="Характеристика предприятия", paragraphs=["Сведения."]),
            Section(title="Структура", paragraphs=["Структура.", "Ещё абзац."]),
        ],
    ),
    Section(
        title="Выводы",
        subsections=[Section(title="Итоги", paragraphs=["Итоговый абзац."])],
    ),
)


def expected_lines(content: DocumentContent) -> list[str]:
    """Абзацы DOCX по порядку: заголовок оглавления, затем заголовки с номерами и текст."""
    lines = [GOST_7_32.toc_title]

    def add(section: Section, number: str) -> None:
        lines.append(f"{number}. {section.title}")
        lines.extend(section.paragraphs)
        for index, subsection in enumerate(section.subsections, start=1):
            add(subsection, f"{number}.{index}")

    for index, section in enumerate(content.sections, start=1):
        add(section, str(index))
    return lines


def extract_lines(data: bytes) -> list[str]:
    result = extract_docx_text(data, LIMITS, deadline=time.monotonic() + 60)
    assert isinstance(result, ExtractionResult), result
    assert result.truncation is None
    return result.text.split("\n")


def _render(content: DocumentContent = CONTENT) -> bytes:
    return PracticeReportDocxRenderer().render(content)


class Package:
    """Части DOCX как деревья lxml."""

    def __init__(self, data: bytes) -> None:
        self.zip = zipfile.ZipFile(io.BytesIO(data))

    def xml(self, name: str) -> Any:
        return etree.fromstring(self.zip.read(name))

    def style(self, style_id: str) -> Any:
        [style] = self.xml("word/styles.xml").xpath(
            f'w:style[@w:styleId="{style_id}"]', namespaces=NS
        )
        return style

    def body_paragraphs(self) -> list[Any]:
        return self.xml("word/document.xml").xpath("w:body/w:p", namespaces=NS)


def _text(paragraph: Any) -> str:
    return "".join(paragraph.xpath(".//w:t/text()", namespaces=NS))


def _style_of(paragraph: Any) -> str | None:
    values = paragraph.xpath("w:pPr/w:pStyle/@w:val", namespaces=NS)
    return values[0] if values else None


def _on(toggle: Any) -> bool:
    """Значение переключателя OOXML (``<w:b/>``, ``<w:b w:val="0"/>``)."""
    return toggle is not None and toggle.get(_w("val")) not in ("0", "false", "off")


def _attr(element: Any, path: str, name: str) -> str | None:
    found = element.find(path, NS)
    return None if found is None else found.get(_w(name))


# --- Пакет -------------------------------------------------------------------------------


def test_render_returns_valid_docx_package() -> None:
    data = _render()

    assert isinstance(data, bytes) and data[:2] == b"PK"
    package = Package(data)
    assert package.zip.testzip() is None
    names = set(package.zip.namelist())
    for part in (
        "[Content_Types].xml",
        "_rels/.rels",
        "word/document.xml",
        "word/_rels/document.xml.rels",
        "word/styles.xml",
        "word/settings.xml",
        "word/footer1.xml",
        "docProps/core.xml",
    ):
        assert part in names
    for part in ("word/document.xml", "word/styles.xml", "word/settings.xml"):
        package.xml(part)  # разбирается без ошибок
    content_types = package.zip.read("[Content_Types].xml").decode()
    assert "wordprocessingml.document.main+xml" in content_types


def test_renderer_metadata_and_protocol() -> None:
    renderer: DocumentRenderer = PracticeReportDocxRenderer()

    assert renderer.file_extension == "docx"
    assert renderer.mime_type == DOCX_MIME_TYPE
    assert DOCX_MIME_TYPE == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    # Синхронный: из async-кода — через asyncio.to_thread.
    assert not inspect.iscoroutinefunction(PracticeReportDocxRenderer.render)
    assert set(inspect.signature(PracticeReportDocxRenderer.render).parameters) == {
        "self",
        "content",
    }


def test_layout_constants() -> None:
    layout = GOST_7_32
    assert (layout.page_width_mm, layout.page_height_mm) == (210, 297)
    assert (
        layout.margin_left_mm,
        layout.margin_right_mm,
        layout.margin_top_mm,
        layout.margin_bottom_mm,
    ) == (30, 15, 20, 20)
    assert (layout.font_name, layout.font_size_pt) == ("Times New Roman", 14)
    assert (layout.line_spacing, layout.first_line_indent_cm) == (1.5, 1.25)
    assert (layout.toc_title, layout.toc_levels) == ("СОДЕРЖАНИЕ", "1-2")


# --- Оформление ------------------------------------------------------------------------


def test_page_is_a4_with_gost_margins() -> None:
    [section] = Package(_render()).xml("word/document.xml").xpath("w:body/w:sectPr", namespaces=NS)

    assert _attr(section, "w:pgSz", "w") == str(_twips_from_mm(210)) == "11906"
    assert _attr(section, "w:pgSz", "h") == str(_twips_from_mm(297)) == "16838"
    assert _attr(section, "w:pgSz", "orient") in (None, "portrait")
    assert _attr(section, "w:pgMar", "left") == str(_twips_from_mm(30)) == "1701"
    assert _attr(section, "w:pgMar", "right") == str(_twips_from_mm(15)) == "850"
    assert _attr(section, "w:pgMar", "top") == str(_twips_from_mm(20)) == "1134"
    assert _attr(section, "w:pgMar", "bottom") == str(_twips_from_mm(20)) == "1134"


def _assert_times_new_roman(run_properties: Any) -> None:
    fonts = run_properties.find("w:rFonts", NS)
    for name in ("ascii", "hAnsi", "eastAsia", "cs"):
        assert fonts.get(_w(name)) == "Times New Roman"
    # Темы шрифтов шаблона (Calibri, Cambria) не перекрывают явный шрифт.
    for name in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
        assert fonts.get(_w(name)) is None


def test_normal_style_has_gost_text_format() -> None:
    normal = Package(_render()).style("Normal")

    _assert_times_new_roman(normal.find("w:rPr", NS))
    assert _attr(normal, "w:rPr/w:sz", "val") == "28"  # 14 pt в полупунктах
    assert _attr(normal, "w:pPr/w:spacing", "line") == "360"  # 1,5 × 240
    assert _attr(normal, "w:pPr/w:spacing", "lineRule") == "auto"
    assert _attr(normal, "w:pPr/w:spacing", "before") == "0"
    assert _attr(normal, "w:pPr/w:spacing", "after") == "0"
    assert _attr(normal, "w:pPr/w:ind", "firstLine") == str(round(1.25 / 2.54 * 1440)) == "709"
    assert _attr(normal, "w:pPr/w:jc", "val") == "both"  # по ширине


def test_document_defaults_use_font_and_russian_language() -> None:
    styles = Package(_render()).xml("word/styles.xml")
    [run_defaults] = styles.xpath("w:docDefaults/w:rPrDefault/w:rPr", namespaces=NS)

    _assert_times_new_roman(run_defaults)
    assert _attr(run_defaults, "w:lang", "val") == "ru-RU"


@pytest.mark.parametrize(("level", "page_break"), [(1, True), (2, False)])
def test_heading_styles(level: int, page_break: bool) -> None:
    style = Package(_render()).style(f"Heading{level}")

    run = style.find("w:rPr", NS)
    _assert_times_new_roman(run)
    assert _attr(style, "w:rPr/w:sz", "val") == "28"
    assert _on(run.find("w:b", NS))
    assert _attr(style, "w:rPr/w:color", "val") == "000000"
    assert _attr(style, "w:rPr/w:color", "themeColor") is None
    assert _attr(style, "w:pPr/w:outlineLvl", "val") == str(level - 1)  # для TOC \o
    assert _attr(style, "w:pPr/w:spacing", "line") == "360"
    assert _on(style.find("w:pPr/w:keepNext", NS))
    assert _on(style.find("w:pPr/w:pageBreakBefore", NS)) is page_break
    assert style.find("w:pPr/w:numPr", NS) is None  # номер — в тексте заголовка


def test_body_text_formatted_by_styles_not_directly() -> None:
    paragraphs = Package(_render()).body_paragraphs()
    text = [p for p in paragraphs if _text(p) == "Первый абзац."]

    [paragraph] = text
    assert paragraph.find("w:pPr", NS) is None
    assert paragraph.find("w:r/w:rPr", NS) is None


def test_sections_and_subsections_use_heading_styles() -> None:
    paragraphs = Package(_render()).body_paragraphs()
    headings = [(_style_of(p), _text(p)) for p in paragraphs if _style_of(p)]

    assert headings == [
        ("Heading1", "1. Теоретические основы"),
        ("Heading1", "2. Анализ деятельности"),
        ("Heading2", "2.1. Характеристика предприятия"),
        ("Heading2", "2.2. Структура"),
        ("Heading1", "3. Выводы"),
        ("Heading2", "3.1. Итоги"),
    ]


# --- Нумерация ---------------------------------------------------------------------------


def test_numbering_follows_structure() -> None:
    content = _document(
        Section(
            title="Первый",
            subsections=[
                Section(title="А", paragraphs=["т"]),
                Section(title="Б", paragraphs=["т"]),
            ],
        ),
        Section(title="Второй", subsections=[Section(title="В", paragraphs=["т"])]),
    )

    headings = [_text(p) for p in Package(_render(content)).body_paragraphs() if _style_of(p)]

    assert headings == ["1. Первый", "1.1. А", "1.2. Б", "2. Второй", "2.1. В"]


def test_structural_elements_are_unnumbered() -> None:
    """ГОСТ 7.32 (Stage 3.11): «Введение» и «Заключение» — без номера, заглавными;
    основные разделы нумеруются с 1. Остальные названия — как обычные разделы."""
    content = _document(
        Section(title="Введение", paragraphs=["т"]),
        Section(title="Основная часть", paragraphs=["т"]),
        Section(title="  заключение ", paragraphs=["т"]),
        Section(title="Список литературы", paragraphs=["т"]),
    )

    headings = [_text(p) for p in Package(_render(content)).body_paragraphs() if _style_of(p)]

    assert headings == [
        "ВВЕДЕНИЕ",
        "1. Основная часть",
        "ЗАКЛЮЧЕНИЕ",
        "2. Список литературы",
    ]


# --- Поля: номер страницы и оглавление ------------------------------------------------


def _field(paragraph: Any) -> tuple[list[str], str]:
    kinds = paragraph.xpath("w:r/w:fldChar/@w:fldCharType", namespaces=NS)
    instruction = "".join(paragraph.xpath("w:r/w:instrText/text()", namespaces=NS))
    return kinds, instruction


def test_page_number_field_centered_in_footer() -> None:
    package = Package(_render())
    [section] = package.xml("word/document.xml").xpath("w:body/w:sectPr", namespaces=NS)
    assert _attr(section, "w:footerReference", "type") == "default"

    [paragraph] = package.xml("word/footer1.xml").xpath("w:p", namespaces=NS)
    assert _field(paragraph) == (["begin", "separate", "end"], " PAGE ")
    assert _attr(paragraph, "w:pPr/w:jc", "val") == "center"
    assert _attr(paragraph, "w:pPr/w:ind", "firstLine") == "0"
    assert _text(paragraph) == ""  # номер вычисляет редактор


def test_table_of_contents_field() -> None:
    package = Package(_render())
    title, field, first_heading = package.body_paragraphs()[:3]

    assert _text(title) == "СОДЕРЖАНИЕ"
    assert _style_of(title) is None  # сам заголовок оглавления в оглавление не попадает
    assert _attr(title, "w:pPr/w:jc", "val") == "center"
    kinds, instruction = _field(field)
    assert kinds == ["begin", "separate", "end"]
    assert instruction == ' TOC \\o "1-2" \\h \\z \\u '
    assert field.xpath("w:r/w:fldChar[@w:fldCharType='begin']/@w:dirty", namespaces=NS) == ["true"]
    assert _text(field) == ""  # страницы рендерер не вычисляет
    assert _style_of(first_heading) == "Heading1"


def test_fields_update_on_open_in_schema_position() -> None:
    settings = Package(_render()).xml("word/settings.xml")
    tags = [etree.QName(child).localname for child in settings]

    [update] = settings.findall("w:updateFields", NS)
    assert update.get(_w("val")) == "true"
    # CT_Settings — последовательность: updateFields идёт до compat и прочих.
    assert tags.index("updateFields") < tags.index("compat")
    assert tags.index("updateFields") < tags.index("listSeparator")


# --- Текст ------------------------------------------------------------------------------


def test_round_trip_text() -> None:
    assert extract_lines(_render()) == expected_lines(CONTENT)


def test_unicode_and_xml_special_characters_round_trip() -> None:
    texts = [
        "Кириллица: съешь же ещё этих мягких французских булок.",
        "English text with digits 2026 and punctuation!",
        "Спецсимволы XML: <tag> & </tag> \"двойные\" 'одинарные' — апостроф d'Artagnan",
        "Неразрывный\u00a0пробел, «ёлочки», „лапки“, № 5, 10 %, ∑ x² ≤ 10",
    ]
    content = _document(
        Section(title="Знаки < > & \" '", paragraphs=texts),
        Section(title="Ещё", paragraphs=["Последний абзац."]),
    )

    data = _render(content)

    assert extract_lines(data) == expected_lines(content)
    document_xml = Package(data).zip.read("word/document.xml").decode()
    assert "&lt;tag&gt; &amp; &lt;/tag&gt;" in document_xml


def test_long_paragraph_round_trip() -> None:
    long_text = " ".join(f"Слово{i}" for i in range(4000))  # ~35 000 символов
    content = _document(Section(title="Длинный", paragraphs=[long_text, "Короткий."]))

    assert extract_lines(_render(content)) == expected_lines(content)


# --- Детерминизм и метаданные ------------------------------------------------------------


def test_render_is_deterministic() -> None:
    first = _render()
    time.sleep(2.1)  # время записей ZIP — с точностью до 2 секунд
    second = PracticeReportDocxRenderer().render(CONTENT.model_copy(deep=True))

    assert first == second


def test_no_dynamic_metadata() -> None:
    package = Package(_render())

    assert {info.date_time for info in package.zip.infolist()} == {(1980, 1, 1, 0, 0, 0)}
    core = package.zip.read("docProps/core.xml").decode()
    assert "dcterms:created" not in core and "dcterms:modified" not in core
    assert "python-docx" not in core
    assert "<dc:title>Анализ бизнес-процессов</dc:title>" in core


def test_render_does_not_mutate_input() -> None:
    before = CONTENT.model_dump()

    _render()

    assert CONTENT.model_dump() == before


# --- Невалидный вход ---------------------------------------------------------------------


def test_invalid_document_rejected_before_rendering(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_document() -> None:
        raise AssertionError("rendering started before validation")

    monkeypatch.setattr(docx_module, "Document", no_document)
    content = _document(Section(title="Р", paragraphs=["до\x00после"]))

    with pytest.raises(InvalidDocumentError) as caught:
        PracticeReportDocxRenderer().render(content)

    assert caught.value.code is DocumentErrorCode.INVALID_CHARACTER


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (_document(), DocumentErrorCode.NO_SECTIONS),
        (_document(Section(title="Р", paragraphs=[""])), DocumentErrorCode.EMPTY_PARAGRAPH),
        (
            DocumentContent(
                doc_type=DocType.PRACTICE_REPORT,
                title="t",
                title_page={"rector": "Иванов"},
                sections=[Section(title="Р", paragraphs=["т"])],
            ),
            DocumentErrorCode.TITLE_PAGE_UNSUPPORTED,
        ),
    ],
    ids=["no-sections", "empty-paragraph", "unknown-title-page-field"],
)
def test_invalid_document_raises(content: DocumentContent, code: DocumentErrorCode) -> None:
    with pytest.raises(InvalidDocumentError) as caught:
        _render(content)

    assert caught.value.code is code


def test_other_doc_type_rejected() -> None:
    content = DocumentContent.model_construct(
        doc_type="other",
        title="t",
        title_page={},
        sections=[Section(title="Р", paragraphs=["т"])],
        references=[],
    )

    with pytest.raises(InvalidDocumentError) as caught:
        _render(content)

    assert caught.value.code is DocumentErrorCode.UNSUPPORTED_DOC_TYPE


# --- Большой документ ---------------------------------------------------------------------


def test_forty_page_document() -> None:
    """8 разделов × 5 страниц (по 1800 символов): рендерится быстро, без утечки памяти."""
    page = "Предложение отчёта о практике с обычной длиной слов. " * 34  # ~1800 символов
    sections = [
        Section(
            title=f"Раздел {i}",
            paragraphs=[page.strip()],
            subsections=[
                Section(title=f"Подраздел {i}.{j}", paragraphs=[page.strip(), page.strip()])
                for j in range(1, 3)
            ],
        )
        for i in range(1, 9)
    ]
    content = _document(*sections)
    assert sum(len(p) for s in sections for p in _all_paragraphs(s)) >= 8 * 5 * 1800

    tracemalloc.start()
    started = time.monotonic()
    try:
        data = _render(content)
        elapsed = time.monotonic() - started
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # Пороги с большим запасом: ловят зависание и взрыв памяти, а не замеряют скорость.
    assert elapsed < 30
    assert peak < 200 * 1024 * 1024
    assert Package(data).zip.testzip() is None
    assert extract_lines(data) == expected_lines(content)


def _all_paragraphs(section: Section) -> list[str]:
    paragraphs = list(section.paragraphs)
    for subsection in section.subsections:
        paragraphs += _all_paragraphs(subsection)
    return paragraphs


# --- Возможности рендерера: поля титульного листа (Stage 3.3, 3.11) ----------------------


@pytest.mark.parametrize(
    ("capability", "code"),
    [
        (
            {"title_page": {"student_name": "Иванов Иван", "rector": "Петров"}},
            DocumentErrorCode.TITLE_PAGE_UNSUPPORTED,
        ),
    ],
    ids=["unknown-title-page-field"],
)
def test_unsupported_capabilities_rejected_before_rendering(
    monkeypatch: pytest.MonkeyPatch, capability: dict[str, Any], code: DocumentErrorCode
) -> None:
    """Данные валидны для домена, но рендерер такого поля не знает: отказ до Document()."""

    def no_document() -> None:
        raise AssertionError("rendering started before capability check")

    monkeypatch.setattr(docx_module, "Document", no_document)
    content = DocumentContent(
        doc_type=DocType.PRACTICE_REPORT,
        title="t",
        sections=[Section(title="Р", paragraphs=["т"])],
        **capability,
    )
    docx_module.validate_document_content(content)  # домен такие данные принимает

    with pytest.raises(InvalidDocumentError) as caught:
        PracticeReportDocxRenderer().render(content)

    assert caught.value.code is code


def test_capability_checked_before_document_validation() -> None:
    """Порядок: doc_type → возможности рендерера → проверка документа."""
    content = DocumentContent(
        doc_type=DocType.PRACTICE_REPORT,
        title="t",
        title_page={"rector": "Иванов"},
        sections=[Section(title="Р", paragraphs=["до\x00после"])],
    )

    with pytest.raises(InvalidDocumentError) as caught:
        _render(content)

    assert caught.value.code is DocumentErrorCode.TITLE_PAGE_UNSUPPORTED


# --- Стили оглавления toc 1 / toc 2 (Stage 3.3) ------------------------------------------


def _style_by_name(package: Package, name: str) -> Any:
    [style] = package.xml("word/styles.xml").xpath(
        f'w:style[w:name/@w:val="{name}"]', namespaces=NS
    )
    return style


def _twips_from_cm(cm: float) -> int:
    return round(cm / 2.54 * 1440)


@pytest.mark.parametrize(
    ("level", "left_indent"),
    [(1, "0"), (2, str(_twips_from_cm(GOST_7_32.toc_level_indent_cm)))],
)
def test_toc_styles(level: int, left_indent: str) -> None:
    style = _style_by_name(Package(_render()), f"toc {level}")

    assert style.get(_w("type")) == "paragraph"
    assert style.get(_w("customStyle")) is None  # встроенный стиль Word
    assert _attr(style, "w:basedOn", "val") == "Normal"
    assert _attr(style, "w:pPr/w:ind", "firstLine") == "0"
    assert _attr(style, "w:pPr/w:ind", "left") == left_indent
    assert _attr(style, "w:pPr/w:jc", "val") == "left"
    [tab] = style.findall("w:pPr/w:tabs/w:tab", NS)
    assert (tab.get(_w("val")), tab.get(_w("leader"))) == ("right", "dot")
    assert tab.get(_w("pos")) == str(_twips_from_mm(GOST_7_32.text_width_mm)) == "9354"
    # Шрифт, размер и интервал — от Normal: своих rFonts, тем и размеров нет.
    assert style.find("w:rPr", NS) is None


def test_toc_level_two_indent_constant() -> None:
    assert GOST_7_32.toc_level_indent_cm == 1.25
    assert _twips_from_cm(1.25) == 709
    assert GOST_7_32.text_width_mm == 210 - 30 - 15


def test_toc_styles_defined_once() -> None:
    styles = Package(_render()).xml("word/styles.xml")
    names = styles.xpath("w:style/w:name/@w:val", namespaces=NS)

    assert names.count("toc 1") == 1 and names.count("toc 2") == 1
    assert "toc 3" not in names


# --- Пакет без артефактов шаблона (Stage 3.3) ---------------------------------------------

PACKAGE_PARTS = {
    "[Content_Types].xml",
    "_rels/.rels",
    "docProps/core.xml",
    "word/document.xml",
    "word/_rels/document.xml.rels",
    "word/styles.xml",
    "word/settings.xml",
    "word/webSettings.xml",
    "word/fontTable.xml",
    "word/theme/theme1.xml",
    "word/numbering.xml",
    "word/footer1.xml",
}
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"


def test_package_parts_allowlist() -> None:
    """Точный состав пакета: изменение шаблона python-docx не пройдёт незамеченным."""
    assert set(Package(_render()).zip.namelist()) == PACKAGE_PARTS


def _relationships(package: Package) -> list[tuple[str, str, str]]:
    """(часть-источник, тип, часть-цель) для всех внутренних связей пакета."""
    found = []
    for name in package.zip.namelist():
        if not name.endswith(".rels"):
            continue
        directory, rels_file = posixpath.split(name)
        source_directory = posixpath.dirname(directory)  # word/_rels → word
        source = posixpath.join(source_directory, rels_file.removesuffix(".rels"))
        for rel in package.xml(name).findall(f"{{{PKG_REL}}}Relationship"):
            if rel.get("TargetMode") == "External":
                continue
            target = posixpath.normpath(posixpath.join(source_directory, rel.get("Target")))
            found.append((source, rel.get("Type"), target))
    return found


def test_no_dangling_or_orphan_parts() -> None:
    package = Package(_render())
    names = set(package.zip.namelist())
    relationships = _relationships(package)

    targets = {target for _, _, target in relationships}
    assert targets <= names  # нет связей на отсутствующие части
    content_parts = {n for n in names if not n.endswith(".rels") and n != "[Content_Types].xml"}
    assert content_parts == targets  # и нет частей, на которые никто не ссылается


def test_content_types_match_parts() -> None:
    package = Package(_render())
    types = package.xml("[Content_Types].xml")
    defaults = {d.get("Extension") for d in types.findall(f"{{{CONTENT_TYPES}}}Default")}
    overrides = {
        o.get("PartName").lstrip("/") for o in types.findall(f"{{{CONTENT_TYPES}}}Override")
    }
    names = set(package.zip.namelist()) - {"[Content_Types].xml"}

    assert overrides <= names  # нет типов для удалённых частей
    for name in names:
        # Расширение — после последней точки (у «_rels/.rels» это «rels»).
        assert name in overrides or name.rsplit(".", 1)[-1] in defaults, name
    assert "jpeg" not in defaults


def test_template_artifacts_removed() -> None:
    package = Package(_render())
    names = package.zip.namelist()
    relationship_types = {kind for _, kind, _ in _relationships(package)}

    for artifact in ("thumbnail", "docProps/app.xml", "customXml", "stylesWithEffects"):
        assert not any(artifact in name for name in names), artifact
    assert not any(
        kind.endswith(("/thumbnail", "/extended-properties", "/customXml", "/stylesWithEffects"))
        for kind in relationship_types
    )
    settings = package.zip.read("word/settings.xml").decode()
    assert "w14:docId" not in settings
    # rsid сознательно оставлены (OD-3); updateFields и прочие настройки — на месте.
    assert "w:rsidRoot" in settings and 'w:updateFields w:val="true"' in settings


# --- Номер страницы до появления титульного листа (Stage 3.3, m8 отложен) -----------------


def test_page_number_without_title_page() -> None:
    """Титульного листа нет: номер на всех страницах, включая первую (оглавление)."""
    [section] = Package(_render()).xml("word/document.xml").xpath("w:body/w:sectPr", namespaces=NS)

    assert section.find("w:titlePg", NS) is None
    footers = [ref.get(_w("type")) for ref in section.findall("w:footerReference", NS)]
    assert footers == ["default"]
    assert section.findall("w:headerReference", NS) == []


# --- Титульный лист и список источников (Stage 3.11) -------------------------------------

R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
FULL_TITLE_PAGE = build_title_page(
    {
        "university": "НИУ ВШЭ",
        "practice_type": "производственная",
        "practice_place": "ООО «Ёлка» & партнёры",
        "topic": "Анализ <бизнес-процессов>",
        "practice_start": "2026-07-01",
        "practice_end": "2026-07-28",
        "student_name": "Иванов Иван Иванович",
        "student_group": "БИ-21-1",
        "specialty": "Бизнес-информатика",
        "supervisor": "Петров Пётр Петрович",
    }
)
# Только обязательные поля ТЗ: группы, направления и руководителя нет.
MINIMAL_TITLE_PAGE = build_title_page(
    {
        "university": "НИУ ВШЭ",
        "practice_type": "учебная",
        "practice_place": "ООО «Ёлка»",
        "topic": "Тема",
        "practice_start": "2026-07-01",
        "practice_end": "2026-07-28",
        "student_name": "Иванов Иван Иванович",
    }
)


def _with(
    title_page: dict[str, str] | None = None, references: list[str] | None = None
) -> DocumentContent:
    return CONTENT.model_copy(
        update={"title_page": title_page or {}, "references": references or []}
    )


def _footers(package: Package) -> dict[str, Any]:
    """Нижние колонтитулы по типу (default, first) — через связи document.xml."""
    [section] = package.xml("word/document.xml").xpath("w:body/w:sectPr", namespaces=NS)
    rels = package.xml("word/_rels/document.xml.rels")
    targets = {rel.get("Id"): rel.get("Target") for rel in rels}
    return {
        ref.get(_w("type")): package.xml(f"word/{targets[ref.get(f'{{{R}}}id')]}")
        for ref in section.findall("w:footerReference", NS)
    }


def _title_page_lines(data: bytes) -> list[str]:
    lines = extract_lines(data)
    return lines[: lines.index(GOST_7_32.toc_title)]


def test_title_page_is_first_page_with_requirements_data() -> None:
    lines = _title_page_lines(_render(_with(FULL_TITLE_PAGE)))

    assert lines == [
        "НИУ ВШЭ",
        f"Факультет (институт): {BLANK_LINE}",
        f"Кафедра: {BLANK_LINE}",
        "ОТЧЁТ",
        "о прохождении практики",
        "Вид практики: производственная",
        "Место прохождения практики: ООО «Ёлка» & партнёры",
        "Тема: Анализ <бизнес-процессов>",
        "Сроки практики: с 01.07.2026 по 28.07.2026",
        "Студент: Иванов Иван Иванович",
        "Группа: БИ-21-1",
        "Направление подготовки: Бизнес-информатика",
        "Руководитель практики от вуза: Петров Пётр Петрович",
        f"Руководитель практики от организации: {BLANK_LINE}",
        f"{BLANK_LINE} 2026",
    ]


def test_title_page_spacing_fits_one_page() -> None:
    # Отступы подобраны так, чтобы лист с предельными значениями ТЗ уместился на одной
    # странице (проверено в Word); остальное — одинарный интервал, шрифт 14 pt по стилю.
    paragraphs = Package(_render(_with(FULL_TITLE_PAGE))).body_paragraphs()
    title = paragraphs[: [_text(p) for p in paragraphs].index(GOST_7_32.toc_title)]

    before = {
        _text(p): int(spacing.get(_w("before")))
        for p in title
        if (spacing := p.find("w:pPr/w:spacing", NS)) is not None
        and spacing.get(_w("before")) != "0"
    }
    assert before == {
        "ОТЧЁТ": 36 * 20,
        "Вид практики: производственная": 12 * 20,
        "Студент: Иванов Иван Иванович": 24 * 20,
        f"{BLANK_LINE} 2026": 24 * 20,
    }
    for paragraph in title:
        assert paragraph.find("w:pPr/w:spacing", NS).get(_w("line")) == "240"
        assert paragraph.xpath(".//w:sz", namespaces=NS) == []


def test_long_title_page_values_use_smaller_font_without_truncation() -> None:
    limit = docx_module.TITLE_PAGE_LONG_VALUE_CHARS
    fields = {
        **FULL_TITLE_PAGE,
        "university": "У" * (limit + 1),
        "practice_place": "М" * limit,  # на пределе — обычный размер
        "topic": "Т" * 500,
        "specialty": "С" * 255,  # уменьшаются только вуз, место практики и тема
    }
    paragraphs = Package(_render(_with(fields))).body_paragraphs()
    title = paragraphs[: [_text(p) for p in paragraphs].index(GOST_7_32.toc_title)]

    sizes = {
        _text(p): [int(sz.get(_w("val"))) for sz in p.xpath(".//w:sz", namespaces=NS)]
        for p in title
    }
    small = [docx_module.TITLE_PAGE_LONG_VALUE_FONT_PT * 2]
    assert sizes.pop("У" * (limit + 1)) == small
    assert sizes.pop("Тема: " + "Т" * 500) == small
    assert sizes["Место прохождения практики: " + "М" * limit] == []
    assert sizes["Направление подготовки: " + "С" * 255] == []
    assert all(value == [] for value in sizes.values())


def test_missing_title_page_data_becomes_blank_lines() -> None:
    lines = _title_page_lines(_render(_with(MINIMAL_TITLE_PAGE)))

    assert f"Группа: {BLANK_LINE}" in lines
    assert f"Направление подготовки: {BLANK_LINE}" in lines
    assert f"Руководитель практики от вуза: {BLANK_LINE}" in lines
    assert "Студент: Иванов Иван Иванович" in lines


def test_title_page_text_is_escaped_in_xml() -> None:
    document = Package(_render(_with(FULL_TITLE_PAGE))).zip.read("word/document.xml").decode()

    assert "Анализ &lt;бизнес-процессов&gt;" in document
    assert "«Ёлка» &amp; партнёры" in document


def test_title_page_has_no_page_number_and_toc_starts_new_page() -> None:
    package = Package(_render(_with(FULL_TITLE_PAGE)))
    [section] = package.xml("word/document.xml").xpath("w:body/w:sectPr", namespaces=NS)

    assert _on(section.find("w:titlePg", NS))
    footers = _footers(package)
    assert set(footers) == {"default", "first"}
    first = footers["first"].xpath(".//w:fldChar | .//w:instrText | .//w:t", namespaces=NS)
    assert first == []  # первая страница — без номера
    [paragraph] = footers["default"].xpath("w:p", namespaces=NS)
    assert _field(paragraph) == (["begin", "separate", "end"], " PAGE ")
    [toc_title] = [p for p in package.body_paragraphs() if _text(p) == GOST_7_32.toc_title]
    assert _on(toc_title.find("w:pPr/w:pageBreakBefore", NS))


def test_without_title_page_layout_is_unchanged() -> None:
    package = Package(_render())
    [section] = package.xml("word/document.xml").xpath("w:body/w:sectPr", namespaces=NS)

    assert section.find("w:titlePg", NS) is None
    assert set(_footers(package)) == {"default"}
    assert extract_lines(_render()) == expected_lines(CONTENT)


def test_references_are_last_unnumbered_section_with_numbered_items() -> None:
    references = [
        "ГОСТ 7.32-2017. Отчёт о научно-исследовательской работе.",
        "Иванов И. И. Учебник.",
    ]
    data = _render(_with(FULL_TITLE_PAGE, references))
    package = Package(data)

    headings = [_text(p) for p in package.body_paragraphs() if _style_of(p) == "Heading1"]
    assert headings[-1] == REFERENCES_TITLE
    [heading] = [p for p in package.body_paragraphs() if _text(p) == REFERENCES_TITLE]
    assert _attr(heading, "w:pPr/w:jc", "val") == "center"
    assert extract_lines(data)[-3:] == [
        REFERENCES_TITLE,
        "1. ГОСТ 7.32-2017. Отчёт о научно-исследовательской работе.",
        "2. Иванов И. И. Учебник.",
    ]


def test_no_references_section_without_references() -> None:
    lines = extract_lines(_render(_with(FULL_TITLE_PAGE)))

    assert REFERENCES_TITLE not in lines


def test_structural_heading_is_centered_and_its_subsections_unnumbered() -> None:
    content = _document(
        Section(title="Введение", subsections=[Section(title="Цели", paragraphs=["т"])]),
        Section(title="Анализ", subsections=[Section(title="Сведения", paragraphs=["т"])]),
    )
    package = Package(_render(content))

    headings = [(p, _text(p)) for p in package.body_paragraphs() if _style_of(p)]
    assert [text for _, text in headings] == ["ВВЕДЕНИЕ", "Цели", "1. Анализ", "1.1. Сведения"]
    assert _attr(headings[0][0], "w:pPr/w:jc", "val") == "center"
    assert _attr(headings[2][0], "w:pPr/w:jc", "val") is None  # обычный раздел — по стилю


def test_render_with_title_page_and_references_is_deterministic() -> None:
    content = _with(FULL_TITLE_PAGE, ["Источник один.", "Источник два."])

    assert _render(content) == _render(content.model_copy(deep=True))
