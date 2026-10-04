"""Документ отчёта перед рендерингом: что можно превратить в файл.

Модуль чистый (без БД, сети, LLM и Telegram). Проверяет DocumentContent
(app.generation.schemas) — общий контракт генерации и рендеринга — и ничего в нём
не меняет. Рендерер вызывает проверку первой: невалидный документ не рендерится.

Структура — как в плане: раздел (глубина 1) и подраздел (глубина 2), глубже нельзя.
В каждой строке — только символы, допустимые в XML и в одном абзаце: без
управляющих (в том числе переводов строк и табуляции) и без суррогатов.

Здесь проверяются данные, а не возможности конкретного рендерера: у title_page и
references — символы и общий размер документа. Поддерживает ли рендерер титульный
лист и список литературы, решает сам рендерер (сейчас DOCX отклоняет непустые).

Ошибка несёт код и путь до места (``sections[1].subsections[0].paragraphs[2]``), но
не сам текст: текст отчёта не попадает в логи.
"""

import re
from enum import StrEnum

from app.generation.schemas import DocumentContent, Section

# Раздел и подраздел, как в плане.
MAX_DEPTH = 2
# Разделов и подразделов вместе: в плане не больше 15 × (1 + 10).
MAX_HEADINGS = 300
MAX_TITLE_CHARS = 500
# Символов во всём документе: объём ТЗ до 500 страниц × 1800 символов × допуск 1,25.
MAX_DOCUMENT_CHARS = 2_000_000

# Недопустимые в XML 1.0 (C0, кроме \t \n \r; U+FFFE, U+FFFF) и управляющие символы
# вообще (\t \n \r, DEL, C1): абзац — одна строка текста.
_INVALID_CHARACTER = re.compile("[\x00-\x1f\x7f-\x9f\ufffe\uffff]")
_SURROGATE = re.compile("[\ud800-\udfff]")


class DocumentErrorCode(StrEnum):
    NO_SECTIONS = "no_sections"
    EMPTY_TITLE = "empty_title"
    TITLE_TOO_LONG = "title_too_long"
    # Раздел без текста: ни абзацев, ни подразделов (у подраздела — без абзацев).
    EMPTY_SECTION = "empty_section"
    EMPTY_PARAGRAPH = "empty_paragraph"
    TOO_DEEP = "too_deep"
    INVALID_CHARACTER = "invalid_character"
    SURROGATE_CHARACTER = "surrogate_character"
    TOO_MANY_HEADINGS = "too_many_headings"
    TOO_LARGE = "too_large"
    TITLE_PAGE_UNSUPPORTED = "title_page_unsupported"
    REFERENCES_UNSUPPORTED = "references_unsupported"
    UNSUPPORTED_DOC_TYPE = "unsupported_doc_type"


class InvalidDocumentError(Exception):
    """Документ нельзя рендерить. ``path`` — где нарушение (без текста документа)."""

    def __init__(self, code: DocumentErrorCode, path: str = "") -> None:
        super().__init__(f"{code} at {path}" if path else str(code))
        self.code = code
        self.path = path


def validate_document_content(content: DocumentContent) -> None:
    """Проверяет документ целиком; ничего не меняет.

    Raises InvalidDocumentError.
    """
    # Название документа (тема) может быть пустым: на этом этапе оно не выводится в текст.
    _check_text(content.title, "title")
    if len(content.title) > MAX_TITLE_CHARS:
        raise InvalidDocumentError(DocumentErrorCode.TITLE_TOO_LONG, "title")
    if not content.sections:
        raise InvalidDocumentError(DocumentErrorCode.NO_SECTIONS, "sections")

    counter = _Counter(chars=len(content.title))
    _check_title_page(content.title_page, counter)
    for index, reference in enumerate(content.references):
        path = f"references[{index}]"
        counter.add_chars(len(reference), path)
        _check_text(reference, path)
    for index, section in enumerate(content.sections):
        _check_section(section, f"sections[{index}]", 1, counter)


def _check_title_page(title_page: dict[str, str], counter: "_Counter") -> None:
    """Ключи и значения титульного листа: символы и общий размер (путь — без значения)."""
    for index, (key, value) in enumerate(title_page.items()):
        key_path = f"title_page.keys[{index}]"
        counter.add_chars(len(key), key_path)
        _check_text(key, key_path)
        value_path = f"title_page[{key!r}]"
        counter.add_chars(len(value), value_path)
        _check_text(value, value_path)


class _Counter:
    def __init__(self, chars: int) -> None:
        self.chars = chars
        self.headings = 0

    def add_heading(self, path: str) -> None:
        self.headings += 1
        if self.headings > MAX_HEADINGS:
            raise InvalidDocumentError(DocumentErrorCode.TOO_MANY_HEADINGS, path)

    def add_chars(self, count: int, path: str) -> None:
        self.chars += count
        if self.chars > MAX_DOCUMENT_CHARS:
            raise InvalidDocumentError(DocumentErrorCode.TOO_LARGE, path)


def _check_section(section: Section, path: str, depth: int, counter: _Counter) -> None:
    counter.add_heading(path)
    title_path = f"{path}.title"
    if not section.title.strip():
        raise InvalidDocumentError(DocumentErrorCode.EMPTY_TITLE, title_path)
    if len(section.title) > MAX_TITLE_CHARS:
        raise InvalidDocumentError(DocumentErrorCode.TITLE_TOO_LONG, title_path)
    counter.add_chars(len(section.title), title_path)
    _check_text(section.title, title_path)

    for index, paragraph in enumerate(section.paragraphs):
        paragraph_path = f"{path}.paragraphs[{index}]"
        if not paragraph.strip():
            raise InvalidDocumentError(DocumentErrorCode.EMPTY_PARAGRAPH, paragraph_path)
        counter.add_chars(len(paragraph), paragraph_path)
        _check_text(paragraph, paragraph_path)

    if section.subsections and depth >= MAX_DEPTH:
        raise InvalidDocumentError(DocumentErrorCode.TOO_DEEP, f"{path}.subsections")
    if not section.paragraphs and not section.subsections:
        raise InvalidDocumentError(DocumentErrorCode.EMPTY_SECTION, path)
    for index, subsection in enumerate(section.subsections):
        _check_section(subsection, f"{path}.subsections[{index}]", depth + 1, counter)


def _check_text(value: str, path: str) -> None:
    if _SURROGATE.search(value):
        raise InvalidDocumentError(DocumentErrorCode.SURROGATE_CHARACTER, path)
    if _INVALID_CHARACTER.search(value):
        raise InvalidDocumentError(DocumentErrorCode.INVALID_CHARACTER, path)
