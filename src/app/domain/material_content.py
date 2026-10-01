"""Минимальная проверка содержимого материала: соответствуют ли байты расширению.

Это фильтр от случайных и очевидно подменённых файлов (картинка под именем
``.pdf``, архив под именем ``.docx``, бинарный файл под именем ``.txt``), а не
гарантия безопасности или корректности документа: файл, прошедший проверку,
всё ещё может быть повреждён или содержать вредоносные конструкции. Документ
здесь не разбирается целиком — только сигнатура и обязательная структура.

Модуль чистый (без БД и Telegram).
"""

import codecs
import io
import re
import zipfile
from collections.abc import Callable

from app.domain.errors import MaterialRejectReason, UnsupportedMaterialError

# Спецификация PDF требует заголовок в начале файла, но читатели допускают мусор
# перед ним в пределах первого килобайта — так же поступаем и мы.
PDF_HEADER = b"%PDF-"
PDF_HEADER_SEARCH_BYTES = 1024

DOCX_REQUIRED_PARTS = frozenset({"[Content_Types].xml", "_rels/.rels", "word/document.xml"})
DOCX_MAIN_CONTENT_TYPE = (
    b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)
# [Content_Types].xml у настоящих DOCX — единицы килобайт. Предел защищает от
# распаковки гигантской части (zip-бомба): больше этого не читаем.
MAX_CONTENT_TYPES_BYTES = 256 * 1024

# Доля управляющих символов (кроме пробельных), при которой текст считается бинарным.
MAX_CONTROL_CHAR_RATIO = 0.01
# Категория Unicode Cc без \t \n \v \f \r.
_CONTROL_CHARS = re.compile("[\x00-\x08\x0e-\x1f\x7f-\x9f]")
# UTF-16 распознаём только по BOM (так сохраняет «Блокнот» в режиме «Юникод»):
# без BOM нулевые байты — признак бинарного файла.
UTF16_BOMS = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
# Кодировки TXT без BOM, которые пробуем по порядку. cp1251 — типичная для русских
# файлов из Windows; она декодирует почти любые байты, поэтому решающая проверка
# для неё — доля управляющих символов.
TEXT_ENCODINGS = ("utf-8-sig", "cp1251")


def check_material_content(extension: str, data: bytes) -> None:
    """Проверяет, что содержимое похоже на файл указанного типа.

    Raises UnsupportedMaterialError(reason=CONTENT).
    """
    _CHECKS[extension](data)


def _reject(message: str) -> UnsupportedMaterialError:
    return UnsupportedMaterialError(message, reason=MaterialRejectReason.CONTENT)


def _check_pdf(data: bytes) -> None:
    if PDF_HEADER not in data[:PDF_HEADER_SEARCH_BYTES]:
        raise _reject("PDF header not found")


def _check_docx(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = {info.filename: info for info in archive.infolist()}
            missing = DOCX_REQUIRED_PARTS - infos.keys()
            if missing:
                raise _reject(f"DOCX parts missing: {sorted(missing)}")
            content_types = infos["[Content_Types].xml"]
            if content_types.file_size > MAX_CONTENT_TYPES_BYTES:
                raise _reject("DOCX [Content_Types].xml is too large")
            with archive.open(content_types) as part:
                # file_size из архива может лгать: ограничиваем фактическое чтение.
                declared = part.read(MAX_CONTENT_TYPES_BYTES + 1)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, RuntimeError) as exc:
        # BadZipFile — не ZIP или битый архив; NotImplementedError — неизвестное сжатие;
        # RuntimeError — зашифрованная часть.
        raise _reject(f"Not a readable ZIP container: {exc}") from exc
    except (OSError, EOFError, ValueError) as exc:
        raise _reject(f"Corrupted ZIP container: {exc}") from exc

    if len(declared) > MAX_CONTENT_TYPES_BYTES:
        raise _reject("DOCX [Content_Types].xml is too large")
    # Отличает DOCX от других OOXML-пакетов (XLSX, PPTX, DOCM) без разбора XML.
    if DOCX_MAIN_CONTENT_TYPE not in declared:
        raise _reject("ZIP container is not a Word document")


def _check_txt(data: bytes) -> None:
    text = decode_text(data)
    if "\x00" in text:
        raise _reject("Text file contains NUL characters")
    _, control = _CONTROL_CHARS.subn("", text)
    if control > len(text) * MAX_CONTROL_CHAR_RATIO:
        raise _reject("Text file looks binary: too many control characters")


def decode_text(data: bytes) -> str:
    """Декодирует TXT: UTF-16 с BOM, иначе UTF-8 (с BOM или без), иначе cp1251.

    Общая для проверки при приёме и для извлечения текста (app.extraction).

    Raises UnsupportedMaterialError(reason=CONTENT).
    """
    if data.startswith(UTF16_BOMS):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise _reject("Text file is not valid UTF-16") from exc
    if b"\x00" in data:
        raise _reject("Text file contains NUL bytes")
    for encoding in TEXT_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise _reject("Text file is not valid UTF-8 or cp1251")


_CHECKS: dict[str, Callable[[bytes], None]] = {
    "pdf": _check_pdf,
    "docx": _check_docx,
    "txt": _check_txt,
}
