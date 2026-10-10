"""Структура содержимого версий ТЗ и плана.

В БД содержимое хранится в JSONB, но записывается только после валидации этими
схемами. Схема ТЗ зависит от типа заказа: новый тип документа добавляет свою
схему в REQUIREMENTS_SCHEMAS.
"""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import DocType


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PracticeReportRequirements(_Strict):
    """ТЗ отчёта по практике. Поля необязательны: черновик заполняется постепенно."""

    topic: str | None = None
    practice_type: str | None = None
    practice_place: str | None = None
    practice_start: date | None = None
    practice_end: date | None = None
    university: str | None = None
    specialty: str | None = None
    student_name: str | None = None
    student_group: str | None = None
    supervisor: str | None = None
    target_pages: int | None = Field(default=None, ge=1)
    # Объём из материалов, под который план не составить: план идёт без объёма, если
    # пользователь согласился. strict — bool, строки и float не принимаются за число.
    # Нет в версиях, сохранённых до этого поля: для них оно None.
    unsupported_target_pages: int | None = Field(default=None, ge=1, strict=True)
    extra_requirements: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_period(self) -> "PracticeReportRequirements":
        if self.practice_start and self.practice_end and self.practice_start > self.practice_end:
            raise ValueError("practice_start must not be after practice_end")
        return self

    @model_validator(mode="after")
    def _check_volume(self) -> "PracticeReportRequirements":
        if self.target_pages is not None and self.unsupported_target_pages is not None:
            raise ValueError("target_pages and unsupported_target_pages are exclusive")
        return self


REQUIREMENTS_SCHEMAS: dict[DocType, type[BaseModel]] = {
    DocType.PRACTICE_REPORT: PracticeReportRequirements,
}


class PlanSection(_Strict):
    title: str = Field(min_length=1)
    description: str = ""
    requirements: list[str] = Field(default_factory=list)
    target_pages: int | None = Field(default=None, ge=1)
    subsections: list["PlanSection"] = Field(default_factory=list)


class PlanContent(_Strict):
    """План документа. Порядок разделов — порядок элементов списка sections."""

    sections: list[PlanSection] = Field(min_length=1)
    total_pages: int | None = Field(default=None, ge=1)
