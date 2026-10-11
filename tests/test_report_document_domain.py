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
    heading_numbers,
    is_structural,
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
    ],
)
def test_invalid_structure(content: DocumentContent, code: E, path: str) -> None:
    error = _error(content)

    assert (error.code, error.path) == (code, path)


# --- Титульный лист и список литературы: данные, а не возможности рендерера ----------


def test_non_empty_title_page_and_references_pass_domain() -> None:
    """Поддерживает ли их рендерер, решает рендерер; домен проверяет только данные."""
    content = _document(
        title_page={"student_name": "Иванов Иван", "university": "НИУ ВШЭ"},
        references=["Источник 1.", "Источник 2."],
    )

    validate_document_content(content)


@pytest.mark.parametrize(
    ("title_page", "code", "path"),
    [
        ({"student_name": "до\x00после"}, E.INVALID_CHARACTER, "title_page['student_name']"),
        ({"student_name": "до\nпосле"}, E.INVALID_CHARACTER, "title_page['student_name']"),
        (
            {"student_name": "до" + chr(0xD800) + "после"},
            E.SURROGATE_CHARACTER,
            "title_page['student_name']",
        ),
        ({"ключ\x07": "значение"}, E.INVALID_CHARACTER, "title_page.keys[0]"),
        ({"ок": "т", "ключ" + chr(0xDFFF): "т"}, E.SURROGATE_CHARACTER, "title_page.keys[1]"),
    ],
    ids=["value-control", "value-newline", "value-surrogate", "key-control", "key-surrogate"],
)
def test_title_page_invalid_characters(title_page: dict[str, str], code: E, path: str) -> None:
    error = _error(_document(title_page=title_page))

    assert (error.code, error.path) == (code, path)


@pytest.mark.parametrize(
    ("char", "code"),
    [
        ("\x00", E.INVALID_CHARACTER),
        ("\t", E.INVALID_CHARACTER),
        (chr(0xFFFE), E.INVALID_CHARACTER),
        (chr(0xD800), E.SURROGATE_CHARACTER),
    ],
    ids=["U+0000", "U+0009", "U+FFFE", "U+D800"],
)
def test_references_invalid_characters(char: str, code: E) -> None:
    error = _error(_document(references=["Источник.", f"до{char}после"]))

    assert (error.code, error.path) == (code, "references[1]")


def test_title_page_and_references_count_toward_size() -> None:
    """Общий лимит не обходится через титульный лист и список литературы."""
    title, section_title, paragraph = "Т", "Р", "т"
    room = MAX_DOCUMENT_CHARS - len(title) - len(section_title) - len(paragraph)
    section = Section(title=section_title, paragraphs=[paragraph])

    key = "k"
    fits = _document(section, title=title, title_page={key: "а" * (room - len(key))})
    validate_document_content(fits)
    validate_document_content(_document(section, title=title, references=["б" * room]))

    # На один символ больше: бюджет общий и накопительный, поэтому лимит пересекает
    # последний учтённый текст — абзац раздела (титульный лист и литература — раньше).
    by_one = [
        _document(section, title=title, title_page={key: "а" * (room - len(key) + 1)}),
        _document(section, title=title, references=["б" * (room + 1)]),
    ]
    for content in by_one:
        error = _error(content)
        assert (error.code, error.path) == (E.TOO_LARGE, "sections[0].paragraphs[0]")

    # Само поле больше лимита — путь указывает на него.
    huge = "в" * MAX_DOCUMENT_CHARS
    error = _error(_document(section, title=title, title_page={key: huge}))
    assert (error.code, error.path) == (E.TOO_LARGE, "title_page['k']")
    error = _error(_document(section, title=title, references=[huge]))
    assert (error.code, error.path) == (E.TOO_LARGE, "references[0]")


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


# --- Нумерация заголовков по ГОСТ 7.32 (Stage 3.11) ---------------------------------------


@pytest.mark.parametrize(
    ("title", "structural"),
    [
        ("Введение", True),
        ("ВВЕДЕНИЕ", True),
        ("  заключение  ", True),
        ("Заключение и выводы", False),
        ("Вступление", False),
        ("Список литературы", False),
        ("Введение.", False),
    ],
)
def test_is_structural(title: str, structural: bool) -> None:
    assert is_structural(title) is structural


def test_heading_numbers_skip_structural_elements() -> None:
    titles = ["Введение", "Теория", "Практика", "Заключение"]

    assert heading_numbers(titles) == [None, "1", "2", None]
    assert heading_numbers(["А", "Б"]) == ["1", "2"]
    assert heading_numbers([]) == []
