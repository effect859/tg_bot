from typing import Protocol

from app.generation.schemas import DocumentContent


class DocumentRenderer(Protocol):
    """Превращает DocumentContent в файл (DOCX, позже PPTX/XLSX)."""

    file_extension: str
    mime_type: str

    def render(self, content: DocumentContent) -> bytes:
        """CPU-bound: из async-кода вызывать через asyncio.to_thread."""
        ...
