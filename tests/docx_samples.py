"""Синтетические DOCX для тестов извлечения: XML собирается вручную, без python-docx."""

import io
import zipfile

from material_samples import CONTENT_TYPES_XML, DOCX_MAIN, RELS_XML

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NAMESPACES = (
    f'xmlns:w="{W_NS}" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:v="urn:schemas-microsoft-com:vml" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)
XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def p(text: str) -> str:
    return f"<w:p><w:r><w:t xml:space='preserve'>{text}</w:t></w:r></w:p>"


def body(*blocks: str) -> str:
    return (
        f"{XML_DECLARATION}<w:document {NAMESPACES}><w:body>{''.join(blocks)}</w:body></w:document>"
    )


def header_part(tag: str, *blocks: str) -> str:
    return f"{XML_DECLARATION}<w:{tag} {NAMESPACES}>{''.join(blocks)}</w:{tag}>"


def document_rels(*relationships: tuple[str, str, str]) -> str:
    items = "".join(
        f'<Relationship Id="{rid}" Type="{OFFICE_REL}/{kind}" Target="{target}"/>'
        for rid, kind, target in relationships
    )
    return (
        f"{XML_DECLARATION}<Relationships "
        f'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{items}'
        "</Relationships>"
    )


def make_docx_parts(
    document: str | bytes,
    extra: dict[str, str | bytes] | None = None,
    *,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    parts: dict[str, str | bytes] = {
        "[Content_Types].xml": CONTENT_TYPES_XML.format(main=DOCX_MAIN),
        "_rels/.rels": RELS_XML,
        "word/document.xml": document,
    }
    parts.update(extra or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


TEXTBOX_IN_PARAGRAPH = (
    "<w:p><w:r><w:t xml:space='preserve'>Перед надписью. </w:t></w:r>"
    "<w:r><mc:AlternateContent>"
    "<mc:Choice Requires='wps'><w:drawing><wps:txbx><w:txbxContent>"
    + p("ТБ в абзаце")
    + "</w:txbxContent></wps:txbx></w:drawing></mc:Choice>"
    "<mc:Fallback><w:pict><v:shape><v:textbox><w:txbxContent>"
    + p("ТБ в абзаце (копия VML)")
    + "</w:txbxContent></v:textbox></v:shape></w:pict></mc:Fallback>"
    "</mc:AlternateContent></w:r>"
    "<w:r><w:t>После надписи.</w:t></w:r></w:p>"
)

TABLE = (
    "<w:tbl><w:tr>"
    "<w:tc>" + p("Ячейка A") + "</w:tc>"
    "<w:tc><w:tbl><w:tr><w:tc>"
    + p("Вложенная ячейка")
    + "</w:tc></w:tr></w:tbl>"
    + p("Ячейка B")
    + "</w:tc></w:tr></w:tbl>"
)

BODY_ALTERNATE = (
    "<mc:AlternateContent><mc:Choice Requires='wps'>"
    + p("Альт: выбор")
    + "</mc:Choice><mc:Fallback>"
    + p("Альт: запас")
    + "</mc:Fallback></mc:AlternateContent>"
)

SPECIAL_RUNS = (
    "<w:p>"
    "<w:r><w:t>Ёлка «кавычки» — тире № 5</w:t></w:r>"
    "<w:r><w:tab/><w:t>после табуляции</w:t><w:br/><w:t>новая строка</w:t></w:r>"
    "<w:r><w:t>кто</w:t><w:noBreakHyphen/><w:t>то</w:t></w:r>"
    "<w:r><w:delText>УДАЛЁННЫЙ ТЕКСТ</w:delText></w:r>"
    "<w:r><w:instrText> PAGE </w:instrText></w:r>"
    "</w:p>"
)


def complex_docx() -> bytes:
    """Колонтитулы (два одинаковых верхних), таблицы, надписи, Choice/Fallback, особые элементы."""
    filler = "<w:sectPr/>" * 50
    document = body(
        p("Абзац 1."),
        TABLE,
        filler,
        BODY_ALTERNATE,
        TEXTBOX_IN_PARAGRAPH,
        SPECIAL_RUNS,
        p("Абзац последний."),
    )
    return make_docx_parts(
        document,
        {
            "word/_rels/document.xml.rels": document_rels(
                ("rId1", "header", "header1.xml"),
                ("rId2", "header", "header2.xml"),
                ("rId3", "footer", "footer1.xml"),
                ("rId4", "hyperlink", "https://example.org"),
            ),
            "word/header1.xml": header_part("hdr", p("Верхний колонтитул")),
            "word/header2.xml": header_part("hdr", p("Верхний колонтитул")),
            "word/footer1.xml": header_part("ftr", p("Нижний колонтитул")),
        },
    )


def many_empty_paragraphs(count: int) -> bytes:
    return make_docx_parts(body("<w:p/>" * count, p("конец")))


def many_foreign_elements(count: int) -> bytes:
    return make_docx_parts(body("<w:b/>" * count, p("конец")))


def doctype_docx(declaration: str, content: str = "&ent;") -> bytes:
    document = (
        f"{XML_DECLARATION}{declaration}<w:document {NAMESPACES}><w:body>"
        f"<w:p><w:r><w:t>{content}</w:t></w:r></w:p></w:body></w:document>"
    )
    return make_docx_parts(document)


def zip_bomb_docx(parts: int = 3, size: int = 120 * 1024 * 1024) -> bytes:
    """Маленький архив с огромными заявленными частями (сжатые нули)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES_XML.format(main=DOCX_MAIN))
        archive.writestr("word/document.xml", body(p("x")))
        chunk = b"\0" * (1024 * 1024)
        for index in range(parts):
            with archive.open(f"word/media/zero{index}.bin", "w") as part:
                for _ in range(size // len(chunk)):
                    part.write(chunk)
    return buffer.getvalue()


def lying_size_docx(declared: int, actual_paragraphs: int) -> bytes:
    """Заявленный в центральном каталоге размер document.xml меньше фактического."""
    data = make_docx_parts(body(*(p("слово " * 20) for _ in range(actual_paragraphs))))
    archive = zipfile.ZipFile(io.BytesIO(data))
    info = archive.getinfo("word/document.xml")
    actual = info.file_size
    raw = bytearray(data)
    old, new = actual.to_bytes(4, "little"), declared.to_bytes(4, "little")
    # Размер записан в локальном заголовке и в центральном каталоге — меняем оба.
    position = raw.find(old)
    while position != -1:
        raw[position : position + 4] = new
        position = raw.find(old, position + 4)
    return bytes(raw)
