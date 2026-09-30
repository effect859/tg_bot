"""Контракт между генерацией и рендерингом.

Генератор (LLM) заполняет DocumentContent, рендерер превращает его в файл.
Оформление (шрифты, поля, ГОСТ) — только забота рендерера.
"""

from pydantic import BaseModel, Field

from app.domain.enums import DocType


class Section(BaseModel):
    title: str
    paragraphs: list[str] = Field(default_factory=list)
    subsections: list["Section"] = Field(default_factory=list)


class DocumentContent(BaseModel):
    doc_type: DocType
    title: str
    # Поля титульного листа (вуз, кафедра, студент, руководитель, ...);
    # набор ключей определяется шаблоном рендерера для конкретного doc_type.
    title_page: dict[str, str] = Field(default_factory=dict)
    sections: list[Section] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
