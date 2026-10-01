"""Текст из DOCX: проверка ZIP до разбора XML и потоковый обход через lxml.

Выполняется только в дочернем процессе (app.extraction.worker). python-docx не
используется: на раздутых документах его обход непропорционально медленный.

* ZIP проверяется по центральному каталогу, без распаковки: число частей,
  повторяющиеся имена, шифрование, метод сжатия, сумма заявленных размеров.
* Распаковываются только нужные XML-части, потоком в память, с ограничением
  фактически прочитанных байт. Читатель вызывается самим парсером, поэтому лимиты
  объёма и времени действуют во время первичного разбора, а не после него.
* XML с DOCTYPE отклоняется; сущности не раскрываются, сеть и DTD выключены.
* Извлекаются абзацы основного текста, таблиц, надписей и колонтитулов. Сноски,
  концевые сноски, комментарии, SmartArt и диаграммы не извлекаются.

Коды ошибок libxml2 зависят от его версии, встроенной в колесо lxml (на Linux и
Windows они разные). После обновления lxml тесты классификации нужно повторить
в Linux-контейнере.
"""

import io
import posixpath
import time
import zipfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass

from app.domain.enums import MaterialFailureReason, TextTruncationReason
from app.extraction.base import (
    ExtractionFailure,
    ExtractionLimits,
    ExtractionResult,
    ExtractionWarning,
    WarningCode,
)
from app.extraction.normalize import has_text, looks_unreadable, normalize_text

R = MaterialFailureReason

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P, T, TAB, BR, CR, NO_BREAK_HYPHEN = (
    f"{{{W}}}{name}" for name in ("p", "t", "tab", "br", "cr", "noBreakHyphen")
)
CHOICE, FALLBACK = f"{{{MC}}}Choice", f"{{{MC}}}Fallback"
RELATIONSHIP = f"{{{PKG_REL}}}Relationship"
DOCUMENT = "word/document.xml"
DOCUMENT_RELS = "word/_rels/document.xml.rels"
CONTENT_TYPES = "[Content_Types].xml"

READ_CHUNK = 64 * 1024
# DOCTYPE и ENTITY ищем в начале части побайтно, до разбора; дальше — docinfo парсера.
DOCTYPE_SCAN_BYTES = 8 * 1024
TIME_CHECK_EVERY_EVENTS = 2048

# Коды libxml2: 2.14 (колесо lxml для Linux) сообщает о лимитах парсера кодом
# ERR_RESOURCE_LIMIT; 2.11 (колесо для Windows) — ERR_NO_MEMORY для длинного текста
# и ERR_INTERNAL_ERROR с «Excessive depth» для глубины.
XML_ERR_INTERNAL_ERROR, XML_ERR_NO_MEMORY, XML_ERR_RESOURCE_LIMIT = 1, 2, 114


class _Rejected(Exception):
    def __init__(self, reason: MaterialFailureReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class _Stop(Exception):
    """Мягкая остановка: вернуть частичный результат."""

    def __init__(self, reason: TextTruncationReason) -> None:
        self.reason = reason


@dataclass
class _State:
    limits: ExtractionLimits
    deadline: float
    read_total: int = 0
    events: int = 0

    def check_time(self) -> None:
        if time.monotonic() > self.deadline:
            raise _Stop(TextTruncationReason.TIME_BUDGET)


class _CappedReader:
    """Распакованные байты одной части с лимитами объёма и времени; читает lxml."""

    def __init__(self, raw: io.BufferedIOBase, name: str, cap: int, state: _State) -> None:
        self._raw = raw
        self._name = name
        self._cap = cap
        self._state = state
        self._consumed = 0
        self._head = b""

    def read(self, size: int = READ_CHUNK) -> bytes:
        self._state.check_time()
        chunk = self._raw.read(min(size, READ_CHUNK) if size > 0 else READ_CHUNK)
        self._consumed += len(chunk)
        self._state.read_total += len(chunk)
        if self._consumed > self._cap:
            raise _Rejected(R.LIMIT_EXCEEDED, f"part_actual_size:{self._name}")
        if self._state.read_total > self._state.limits.max_xml_read_total:
            raise _Rejected(R.LIMIT_EXCEEDED, "read_total")
        if len(self._head) < DOCTYPE_SCAN_BYTES:
            self._head += chunk[: DOCTYPE_SCAN_BYTES - len(self._head)]
            _reject_doctype_bytes(self._head, self._name)
        return chunk


def _reject_doctype_bytes(head: bytes, name: str) -> None:
    for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
        if "<!DOCTYPE".encode(encoding) in head or "<!ENTITY".encode(encoding) in head:
            raise _Rejected(R.UNSUPPORTED, f"doctype:{name}")


def extract_docx_text(
    data: bytes, limits: ExtractionLimits, *, deadline: float
) -> ExtractionResult | ExtractionFailure:
    from lxml import etree

    extractor = f"lxml {etree.__version__} / libxml2 {'.'.join(map(str, etree.LIBXML_VERSION))}"
    state = _State(limits=limits, deadline=deadline)
    paragraphs: list[str] = []
    chars = 0
    truncation: TextTruncationReason | None = None
    unreadable = False

    def add(raw: str) -> None:
        nonlocal chars, unreadable
        text = normalize_text(raw)
        if not has_text(text):
            return
        unreadable = unreadable or looks_unreadable(raw)
        room = limits.max_chars - chars
        if room <= 0:
            raise _Stop(TextTruncationReason.CHAR_LIMIT)
        paragraphs.append(text[:room])
        chars += min(len(text), room) + 1  # + перевод строки между абзацами
        if len(text) > room:
            raise _Stop(TextTruncationReason.CHAR_LIMIT)

    try:
        archive = _check_archive(data, limits)
        headers, footers = _header_footer_parts(archive, state)
        # Колонтитулы разных разделов часто одинаковы — без повторов.
        seen: set[tuple[str, ...]] = set()
        for part in headers:
            _add_part_once(archive, part, state, seen, add)
        for text in _paragraphs(archive, DOCUMENT, state):
            add(text)
        for part in footers:
            _add_part_once(archive, part, state, seen, add)
    except _Rejected as exc:
        return ExtractionFailure(exc.reason, exc.detail)
    except _Stop as stop:
        truncation = stop.reason

    text = "\n".join(paragraphs)[: limits.max_chars]
    warnings: list[ExtractionWarning] = []
    if not has_text(text):
        warnings.append(ExtractionWarning(WarningCode.NO_TEXT))
    if unreadable:
        warnings.append(ExtractionWarning(WarningCode.LOW_QUALITY_TEXT))
    return ExtractionResult(
        text=text, extractor=extractor, truncation=truncation, warnings=tuple(warnings)
    )


def _add_part_once(archive, part, state, seen, add) -> None:
    texts = tuple(_paragraphs(archive, part, state))
    if texts and texts not in seen:
        seen.add(texts)
        for text in texts:
            add(text)


def _check_archive(data: bytes, limits: ExtractionLimits) -> zipfile.ZipFile:
    """Проверка по центральному каталогу. Ничего не распаковывает."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        infos = archive.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, EOFError, OSError) as exc:
        raise _Rejected(R.CORRUPTED, f"zip:{type(exc).__name__}") from exc
    if len(infos) > limits.max_zip_entries:
        raise _Rejected(R.LIMIT_EXCEEDED, f"entries:{len(infos)}")
    duplicates = [name for name, count in Counter(i.filename for i in infos).items() if count > 1]
    if duplicates:
        raise _Rejected(R.CORRUPTED, "duplicate_names")
    if any(info.flag_bits & 0x1 for info in infos):
        raise _Rejected(R.ENCRYPTED, "zip_entry_encrypted")
    if any(info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) for info in infos):
        raise _Rejected(R.UNSUPPORTED, "compression_method")
    declared = sum(info.file_size for info in infos)
    if declared > limits.max_zip_declared_total:
        raise _Rejected(R.LIMIT_EXCEEDED, f"declared_total:{declared}")
    names = {info.filename for info in infos}
    if DOCUMENT not in names or CONTENT_TYPES not in names:
        raise _Rejected(R.CORRUPTED, "missing_parts")
    return archive


def _open_part(archive: zipfile.ZipFile, name: str, cap: int, state: _State) -> _CappedReader:
    info = archive.getinfo(name)
    if info.file_size > cap:
        raise _Rejected(R.LIMIT_EXCEEDED, f"part_declared_size:{name}:{info.file_size}")
    try:
        raw = archive.open(info)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError, EOFError) as exc:
        raise _Rejected(R.CORRUPTED, f"zip_open:{name}:{type(exc).__name__}") from exc
    return _CappedReader(raw, name, cap, state)


def _iterparse(source: _CappedReader, name: str, state: _State, tag: str | None = None):
    from lxml import etree

    events = etree.iterparse(
        source,
        events=("end",),
        tag=tag,
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
        remove_comments=True,
        remove_pis=True,
    )
    first = True
    try:
        for _, elem in events:
            if first:
                first = False
                if elem.getroottree().docinfo.doctype:
                    raise _Rejected(R.UNSUPPORTED, f"doctype:{name}")
            state.events += 1
            # Время проверяется и в _CappedReader.read() — на каждые 64 КиБ входа,
            # то есть даже если событий нет совсем.
            if state.events % TIME_CHECK_EVERY_EVENTS == 0:
                state.check_time()
            yield elem
        # Часть без единого события: DOCTYPE проверяем по завершении разбора.
        root = events.root
        if first and root is not None and root.getroottree().docinfo.doctype:
            raise _Rejected(R.UNSUPPORTED, f"doctype:{name}")
    except etree.XMLSyntaxError as exc:
        raise _xml_error(exc, name) from exc
    except zipfile.BadZipFile as exc:
        # Ошибка CRC: заявленный размер меньше фактического, повреждённый поток.
        raise _Rejected(R.CORRUPTED, f"zip_stream:{name}") from exc
    except (EOFError, OSError) as exc:
        raise _Rejected(R.CORRUPTED, f"zip_stream:{name}:{type(exc).__name__}") from exc


def _xml_error(exc, name: str) -> _Rejected:
    code = exc.code
    if code in (XML_ERR_RESOURCE_LIMIT, XML_ERR_NO_MEMORY) or (
        code == XML_ERR_INTERNAL_ERROR and "Excessive depth" in (exc.msg or "")
    ):
        return _Rejected(R.LIMIT_EXCEEDED, f"xml_parser_limit:{name}:{code}")
    return _Rejected(R.CORRUPTED, f"xml:{name}:{code}")


def _paragraphs(archive: zipfile.ZipFile, name: str, state: _State) -> Iterator[str]:
    """Текст абзацев части в порядке их завершения.

    * берутся w:t, w:tab, w:br, w:cr, w:noBreakHyphen; удалённый при рецензировании
      текст (w:delText) и коды полей (w:instrText) — нет;
    * абзац очищается сразу после обработки, поэтому абзац надписи не попадает в
      текст внешнего абзаца повторно; надпись выводится перед этим абзацем;
    * mc:Fallback пропускается, если у mc:AlternateContent есть mc:Choice
      (Word пишет надпись дважды: DrawingML и VML).
    """
    source = _open_part(archive, name, state.limits.max_xml_part, state)
    # Только события конца w:p (фильтр на стороне C): элементы вне абзацев
    # освобождаются при завершении следующего абзаца.
    for elem in _iterparse(source, name, state, tag=P):
        if len(elem) and not _in_skipped_fallback(elem):
            parts: list[str] = []
            for node in elem.iter(T, TAB, BR, CR, NO_BREAK_HYPHEN):
                tag = node.tag
                if tag == T:
                    parts.append(node.text or "")
                elif tag == TAB:
                    parts.append("\t")
                elif tag == NO_BREAK_HYPHEN:
                    parts.append("-")
                else:
                    parts.append("\n")
            # Перед clear() не держим ссылок на потомков: живой прокси lxml заставляет
            # clear() переносить поддерево в отдельный документ — на миллионах
            # дочерних элементов это десятки секунд.
            node = None  # noqa: F841
            text = "".join(parts)
            if text.strip():
                yield text
        # Абзац и всё, что было перед ним на том же уровне, уже обработано.
        elem.clear(keep_tail=False)
        parent = elem.getparent()
        if parent is not None:
            while elem.getprevious() is not None:
                del parent[0]


def _in_skipped_fallback(paragraph) -> bool:
    for fallback in paragraph.iterancestors(FALLBACK):
        alternate = fallback.getparent()
        if alternate is not None and alternate.find(CHOICE) is not None:
            return True
    return False


def _header_footer_parts(archive: zipfile.ZipFile, state: _State) -> tuple[list[str], list[str]]:
    names = set(archive.namelist())
    if DOCUMENT_RELS not in names:
        return [], []
    source = _open_part(archive, DOCUMENT_RELS, state.limits.max_rels_part, state)
    headers: list[str] = []
    footers: list[str] = []
    for elem in _iterparse(source, DOCUMENT_RELS, state, tag=RELATIONSHIP):
        rel_type, target = elem.get("Type", ""), elem.get("Target", "")
        if elem.get("TargetMode") == "External" or not target:
            continue
        path = posixpath.normpath(posixpath.join("word", target)).lstrip("/")
        if path.startswith(".."):
            continue
        if rel_type == f"{OFFICE_REL}/header":
            headers.append(path)
        elif rel_type == f"{OFFICE_REL}/footer":
            footers.append(path)
    return [h for h in headers if h in names], [f for f in footers if f in names]
