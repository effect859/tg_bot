from typing import Protocol


class FileStorage(Protocol):
    """Хранилище файлов по ключу (например, ``orders/42/report.docx``)."""

    async def save(self, key: str, data: bytes) -> None: ...

    async def read(self, key: str) -> bytes:
        """Raises FileNotFoundError, если ключа нет."""
        ...

    async def delete(self, key: str) -> None:
        """Идемпотентно: удаление отсутствующего ключа не ошибка."""
        ...

    async def exists(self, key: str) -> bool: ...
