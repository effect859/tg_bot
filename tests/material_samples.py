"""Образцы содержимого материалов для тестов приёма файлов."""

import io
import zipfile

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="{main}"/>'
    "</Types>"
)
DOCX_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
XLSX_MAIN = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Target="word/document.xml" Type="http://schemas.openxmlformats.org'
    '/officeDocument/2006/relationships/officeDocument"/>'
    "</Relationships>"
)
DOCUMENT_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body><w:p><w:r><w:t>Задание на практику</w:t></w:r></w:p></w:body></w:document>"
)


def make_zip(parts: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def make_docx(main_content_type: str = DOCX_MAIN, **overrides: str | bytes | None) -> bytes:
    """Минимальный DOCX; ``overrides`` заменяют части (None — удалить часть)."""
    parts: dict[str, str | bytes | None] = {
        "[Content_Types].xml": CONTENT_TYPES_XML.format(main=main_content_type),
        "_rels/.rels": RELS_XML,
        "word/document.xml": DOCUMENT_XML,
    }
    for key, value in overrides.items():
        parts[_PART_ALIASES.get(key, key)] = value
    return make_zip({name: content for name, content in parts.items() if content is not None})


_PART_ALIASES = {
    "content_types": "[Content_Types].xml",
    "rels": "_rels/.rels",
    "document": "word/document.xml",
}

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj << /Type /Catalog >> endobj\ntrailer\n%%EOF\n"
DOCX_BYTES = make_docx()
TXT_BYTES = "Тема практики: анализ бизнес-процессов.\n".encode()
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256)) * 4
