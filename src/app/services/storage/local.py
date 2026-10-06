import asyncio
import uuid
from pathlib import Path


class LocalFileStorage:
    """FileStorage на локальном диске. Для продакшена заменяется S3-реализацией."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def _path(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root) or path == self._root:
            raise ValueError(f"Invalid storage key: {key!r}")
        return path

    async def save(self, key: str, data: bytes) -> None:
        path = self._path(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Пишем во временный файл и переименовываем: читатель не увидит недописанный файл.
            # Свой временный файл на каждую запись: одновременные save одного ключа не
            # усекают чужой tmp, и replace всегда ставит полный файл.
            tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with tmp.open("xb") as file:
                    file.write(data)
                tmp.replace(path)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise

        await asyncio.to_thread(_write)

    async def read(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path(key).read_bytes)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._path(key).unlink, missing_ok=True)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._path(key).is_file)
