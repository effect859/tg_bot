"""Нормализация извлечённого текста и оценка его качества.

Нормализованный текст безопасен для PostgreSQL (нет NUL и одиночных суррогатов,
которые не кодируются в UTF-8) и единообразен для всех форматов: переводы строк
``\\n``, NFC, без управляющих символов, кроме ``\\t``, ``\\n`` и ``\\f`` (разделитель
страниц PDF).
"""

import re
import unicodedata

PAGE_SEPARATOR = "\f"

# Cc без \t \n \f; \r обрабатывается отдельно (перевод строки).
_CONTROL = re.compile("[\x00-\x08\x0b\x0e-\x1f\x7f-\x9f]")
_SURROGATES = re.compile("[\ud800-\udfff]")
_TRAILING_SPACE = re.compile("[ \t]+\n")
_MANY_BLANK_LINES = re.compile("\n{3,}")

# Страница или документ, где управляющих символов, символов-заменителей и символов
# из области частного использования больше этой доли, считается «нечитаемой»:
# так выглядит PDF со шрифтом без таблицы ToUnicode. Это эвристика.
LOW_QUALITY_RATIO = 0.1
# На коротком тексте доля не показательна.
LOW_QUALITY_MIN_CHARS = 20
_SUSPICIOUS = re.compile("[\x00-\x08\x0b\x0e-\x1f\x7f-\x9f�-]")


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    text = _SURROGATES.sub("", text)
    text = unicodedata.normalize("NFC", text)
    text = _TRAILING_SPACE.sub("\n", text)
    text = _MANY_BLANK_LINES.sub("\n\n", text)
    return text.strip(" \t\n")


def looks_unreadable(raw: str) -> bool:
    """Эвристика по исходному (ненормализованному) тексту: похоже ли на «мусор»."""
    meaningful = sum(1 for ch in raw if not ch.isspace())
    if meaningful < LOW_QUALITY_MIN_CHARS:
        return False
    return len(_SUSPICIOUS.findall(raw)) > meaningful * LOW_QUALITY_RATIO


def has_text(text: str) -> bool:
    return any(not ch.isspace() for ch in text)
