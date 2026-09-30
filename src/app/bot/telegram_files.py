"""Скачивание документов из Telegram с жёстким ограничением размера.

Размер из Telegram (Document.file_size, File.file_size) необязателен и не
проверяется сервером за нас, поэтому окончательно лимит соблюдается по фактически
полученным байтам: поток обрывается, как только их становится больше лимита.
"""

import io

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiohttp import ClientError

# Общий таймаут скачивания одного файла (у aiogram по умолчанию 30 с — мало для 20 МиБ).
DOWNLOAD_TIMEOUT_SECONDS = 120


class FileTooLargeError(Exception):
    """Файл больше лимита: по данным Telegram или по фактически полученным байтам."""


class FileDownloadError(Exception):
    """Не удалось получить файл из Telegram (сеть, ошибка Bot API, таймаут)."""


class CappedBuffer(io.BytesIO):
    """BytesIO, который отказывается принимать больше ``limit`` байт."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self._limit = limit
        self._received = 0

    def write(self, data: bytes) -> int:  # type: ignore[override]
        self._received += len(data)
        if self._received > self._limit:
            raise FileTooLargeError(f"Received more than {self._limit} bytes")
        return super().write(data)


async def download_document(bot: Bot, file_id: str, *, max_size: int) -> bytes:
    """Скачивает файл целиком в память, но не больше ``max_size`` байт.

    Raises FileTooLargeError, FileDownloadError.
    """
    try:
        file = await bot.get_file(file_id)
    except TelegramBadRequest as exc:
        # Облачный Bot API не отдаёт ботам файлы больше 20 МБ.
        if "too big" in exc.message.lower():
            raise FileTooLargeError("Telegram refused to serve the file") from exc
        raise FileDownloadError("getFile failed") from exc
    except (TelegramAPIError, ClientError, TimeoutError) as exc:
        raise FileDownloadError("getFile failed") from exc

    if file.file_size is not None and file.file_size > max_size:
        raise FileTooLargeError(f"File size {file.file_size} exceeds {max_size}")
    if not file.file_path:
        raise FileDownloadError("Telegram returned no file_path")

    buffer = CappedBuffer(max_size)
    try:
        await bot.download_file(file.file_path, buffer, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    except (TelegramAPIError, ClientError, TimeoutError) as exc:
        raise FileDownloadError("File download failed") from exc
    return buffer.getvalue()
