from typing import Protocol

from app.db.models import Order
from app.domain.enums import DocType
from app.generation.schemas import DocumentContent


class DocumentGenerator(Protocol):
    """Пайплайн генерации содержимого для одного типа документа."""

    doc_type: DocType

    async def generate(self, order: Order) -> DocumentContent: ...
