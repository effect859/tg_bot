"""Проверка документа перед рендерингом (чистая функция, без БД)."""

from typing import Any

import pytest

from app.domain.enums import DocType
from app.domain.report_document import (
    MAX_DEPTH,
    MAX_DOCUMENT_CHARS,
    MAX_HEADINGS,
    MAX_TITLE_CHARS,
    DocumentErrorCode,
    InvalidDocumentError,
    validate_document_content,
)
from app.generation.schemas import DocumentContent, Section

E = DocumentErrorCode


def _document(*sections: Section, **kwargs: Any) -> DocumentContent:
    return DocumentContent(
        doc_type=DocType.PRACTICE_REPORT,
        title=kwargs.pop("title", "Анализ бизнес-процессов"),
        sections=list(sections) or [Section(title="Введение", paragraphs=["Текст."])],
        **kwargs,
    )


def _error(content: DocumentContent) -> InvalidDocumentError:
    with pytest.raises(InvalidDocumentError) as caught:
        validate_document_content(content)
    return caught.value


VALID = _document(
    Section(title="Введение", paragraphs=["Первый абзац.", "Второй абзац."]),
    Section(
        title="Характеристика организации",
        subsections=[
            Section(title="Общие сведения", paragraphs=["Сведения."]),
            Section(title="Структура", paragraphs=["Структура."]),
        ],
    ),
)


def test_constants() -> None:
    assert MAX_DEPTH == 2
    assert MAX_HEADINGS == 300
    assert MAX_TITLE_CHARS == 500
    assert MAX_DOCUMENT_CHARS == 2_000_000


def test_valid_document_passes() -> None:
    validate_document_content(VALID)


def test_empty_title_page_and_references_pass() -> None:
    content = _document(title_page={}, references=[])

    validate_document_content(content)


def test_section_with_only_subsections_passes() -> None:
    """Как в Stage 3.1: текст раздела до подразделов может отсутствовать."""
    validate_document_content(
        _document(Section(title="Раздел", subsections=[Section(title="П", paragraphs=["т"])]))
    )


def test_empty_document_title_passes() -> None:
    validate_document_content(_document(title=""))


def test_input_is_not_mutated() -> None:
    before = VALID.model_dump()

    validate_document_content(VALID)

    assert VALID.model_dump() == before


@pytest.mark.parametrize(
    ("content", "code", "path"),
    [
        (
            DocumentContent(doc_type=DocType.PRACTICE_REPORT, title="t", sections=[]),
            E.NO_SECTIONS,
            "sections",
        ),
        (_document(Section(title="", paragraphs=["т"])), E.EMPTY_TITLE, "sections[0].title"),
        (_document(Section(title="  ", paragraphs=["т"])), E.EMPTY_TITLE, "sections[0].title"),
        (
            _document(Section(title="Р", paragraphs=["т", " "])),
            E.EMPTY_PARAGRAPH,
            "sections[0].paragraphs[1]",
        ),
        (
            _document(Section(title="Р", paragraphs=[""])),
            E.EMPTY_PARAGRAPH,
            "sections[0].paragraphs[0]",
        ),
        (_document(Section(title="Р")), E.EMPTY_SECTION, "sections[0]"),
        (
            _document(Section(title="Р", subsections=[Section(title="П")])),
            E.EMPTY_SECTION,
            "sections[0].subsections[0]",
        ),
        (
            _document(
                Section(
                    title="Р",
                    subsections=[
                        Section(
                            title="П",
                            paragraphs=["т"],
                            subsections=[Section(title="Глубже", paragraphs=["т"])],
                        )
                    ],
                )
            ),
            E.TOO_DEEP,
            "sections[0].subsections[0].subsections",
        ),
        (
            _document(Section(title="Р" * (MAX_TITLE_CHARS + 1), paragraphs=["т"])),
            E.TITLE_TOO_LONG,
            "sections[0].title",
        ),
        (_document(title="Т" * (MAX_TITLE_CHARS + 1)), E.TITLE_TOO_LONG, "title"),
        (_document(title_page={"student": "Иванов"}), E.TITLE_PAGE_UNSUPPORTED, "title_page"),
        (_document(references=["Источник"]), E.REFERENCES_UNSUPPORTED, "references"),
    ],
    ids=[
        "no-sections",
        "empty-title",
        "blank-title",
        "blank-paragraph",
        "empty-paragraph",
        "empty-section",
        "empty-subsection",
        "depth-3",
        "section-title-too-long",
        "document-title-too-long",
        "title-page",
        "references",
    ],
)
def test_invalid_structure(content: DocumentContent, code: E, path: str) -> None:
    error = _error(content)

    assert (error.code, error.path) == (code, path)


@pytest.mark.parametrize(
    "char",
    [
        "\x00",
        "\x08",
        "\x0b",
        "\x1f",
        "\t",
        "\n",
        "\r",
        "\x7f",
        "\x85",
        "\x9f",
        "\ufffe",
        "\uffff",
    ],
    ids=lambda char: f"U+{ord(char):04X}",
)
@pytest.mark.parametrize("where", ["title", "section_title", "paragraph"])
def test_invalid_character_rejected(char: str, where: str) -> None:
    error = _error(_with_text(where, f"до{char}после"))

    assert error.code is E.INVALID_CHARACTER
    assert error.path == _PATHS[where]


@pytest.mark.parametrize("char", ["\ud800", "\udbff", "\udc00", "\udfff"])
@pytest.mark.parametrize("where", ["title", "section_title", "paragraph"])
def test_surrogate_rejected(char: str, where: str) -> None:
    error = _error(_with_text(where, f"до{char}после"))

    assert error.code is E.SURROGATE_CHARACTER
    assert error.path == _PATHS[where]


_PATHS = {
    "title": "title",
    "section_title": "sections[0].title",
    "paragraph": "sections[0].paragraphs[0]",
}


def _with_text(where: str, text: str) -> DocumentContent:
    if where == "title":
        return _document(title=text)
    if where == "section_title":
        return _document(Section(title=text, paragraphs=["т"]))
    return _document(Section(title="Р", paragraphs=[text]))


@pytest.mark.parametrize(
    "text",
    [
        "Кириллица и English",
        "Спецсимволы XML: < > & \" '",
        "Неразрывный\u00a0пробел, тире — и «кавычки»",
        "Мягкий пере\u00adнос и эмодзи 📄",
        "Математика: ∑ x² ≤ 10 %",
    ],
)
def test_xml_safe_unicode_passes(text: str) -> None:
    validate_document_content(_document(Section(title=text, paragraphs=[text]), title=text))


def test_too_many_headings() -> None:
    sections = [Section(title=f"Р{i}", paragraphs=["т"]) for i in range(MAX_HEADINGS + 1)]

    error = _error(_document(*sections))

    assert (error.code, error.path) == (E.TOO_MANY_HEADINGS, f"sections[{MAX_HEADINGS}]")


def test_max_headings_pass() -> None:
    sections = [Section(title=f"Р{i}", paragraphs=["т"]) for i in range(MAX_HEADINGS)]

    validate_document_content(_document(*sections))


def test_document_too_large() -> None:
    half = MAX_DOCUMENT_CHARS // 2
    content = _document(Section(title="Р", paragraphs=["а" * half, "б" * half]))

    error = _error(content)

    assert (error.code, error.path) == (E.TOO_LARGE, "sections[0].paragraphs[1]")


def test_document_at_size_limit_passes() -> None:
    title = "Т"
    body = MAX_DOCUMENT_CHARS - len(title) - len("Р")

    validate_document_content(_document(Section(title="Р", paragraphs=["а" * body]), title=title))


def test_error_has_code_and_path_but_no_text() -> None:
    secret = "СЕКРЕТНЫЙ ТЕКСТ\x00"

    error = _error(_document(Section(title="Р", paragraphs=[secret])))

    assert str(error) == "invalid_character at sections[0].paragraphs[0]"
    assert "СЕКРЕТНЫЙ" not in str(error)
