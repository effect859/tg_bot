"""Текст из TXT: та же расшифровка кодировок, что и при приёме файла."""

from app.domain.enums import MaterialFailureReason, TextTruncationReason
from app.domain.errors import UnsupportedMaterialError
from app.domain.material_content import decode_text
from app.extraction.base import (
    ExtractionFailure,
    ExtractionLimits,
    ExtractionResult,
    ExtractionWarning,
    WarningCode,
)
from app.extraction.normalize import has_text, looks_unreadable, normalize_text

EXTRACTOR = "txt"


def extract_txt_text(data: bytes, limits: ExtractionLimits) -> ExtractionResult | ExtractionFailure:
    try:
        raw = decode_text(data)
    except UnsupportedMaterialError as exc:
        # При приёме файл проверялся той же функцией: сюда попадает только испорченный
        # после приёма файл.
        return ExtractionFailure(MaterialFailureReason.CORRUPTED, str(exc))
    text = normalize_text(raw)
    truncation = None
    if len(text) > limits.max_chars:
        text = text[: limits.max_chars]
        truncation = TextTruncationReason.CHAR_LIMIT
    warnings: list[ExtractionWarning] = []
    if not has_text(text):
        warnings.append(ExtractionWarning(WarningCode.NO_TEXT))
    elif looks_unreadable(raw):
        warnings.append(ExtractionWarning(WarningCode.LOW_QUALITY_TEXT))
    return ExtractionResult(
        text=text, extractor=EXTRACTOR, truncation=truncation, warnings=tuple(warnings)
    )
