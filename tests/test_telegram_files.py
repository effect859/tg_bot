"""Скачивание из Telegram. Bot — мок: реальный Bot API здесь не вызывается."""

from collections.abc import Callable
from typing import BinaryIO
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import GetFile
from aiogram.types import File
from aiohttp import ClientConnectionError

from app.bot.telegram_files import (
    CappedBuffer,
    FileDownloadError,
    FileTooLargeError,
    download_document,
)

LIMIT = 1000


def _bot(file_size: int | None = None, file_path: str | None = "documents/file_1.pdf") -> AsyncMock:
    bot = AsyncMock()
    bot.get_file.return_value = File(
        file_id="id", file_unique_id="uid", file_size=file_size, file_path=file_path
    )
    return bot


def _streams(*chunks: bytes) -> Callable[..., object]:
    async def download_file(file_path: str, destination: BinaryIO, **kwargs: object) -> BinaryIO:
        for chunk in chunks:
            destination.write(chunk)
        return destination

    return download_file


def test_capped_buffer_accepts_exactly_limit() -> None:
    buffer = CappedBuffer(LIMIT)
    buffer.write(b"x" * 600)
    buffer.write(b"x" * 400)
    assert buffer.getvalue() == b"x" * LIMIT


def test_capped_buffer_rejects_one_byte_over_limit() -> None:
    buffer = CappedBuffer(LIMIT)
    buffer.write(b"x" * LIMIT)
    with pytest.raises(FileTooLargeError):
        buffer.write(b"x")


async def test_download_returns_bytes() -> None:
    bot = _bot(file_size=6)
    bot.download_file.side_effect = _streams(b"abc", b"def")

    assert await download_document(bot, "id", max_size=LIMIT) == b"abcdef"
    bot.get_file.assert_awaited_once_with("id")


async def test_declared_size_over_limit_is_not_downloaded() -> None:
    bot = _bot(file_size=LIMIT + 1)

    with pytest.raises(FileTooLargeError):
        await download_document(bot, "id", max_size=LIMIT)
    bot.download_file.assert_not_awaited()


@pytest.mark.parametrize("declared", [None, 10])
async def test_actual_bytes_over_limit_abort_download(declared: int | None) -> None:
    # Telegram не сообщил размер или сообщил неверный: лимит держится по байтам.
    bot = _bot(file_size=declared)
    bot.download_file.side_effect = _streams(b"x" * 600, b"x" * 600)

    with pytest.raises(FileTooLargeError):
        await download_document(bot, "id", max_size=LIMIT)


async def test_telegram_refuses_big_file() -> None:
    bot = AsyncMock()
    bot.get_file.side_effect = TelegramBadRequest(
        method=GetFile(file_id="id"), message="Bad Request: file is too big"
    )

    with pytest.raises(FileTooLargeError):
        await download_document(bot, "id", max_size=LIMIT)


@pytest.mark.parametrize(
    "error",
    [
        TelegramBadRequest(method=GetFile(file_id="id"), message="Bad Request: invalid file_id"),
        TelegramNetworkError(method=GetFile(file_id="id"), message="timeout"),
        ClientConnectionError("connection reset"),
        TimeoutError(),
    ],
)
async def test_get_file_errors(error: Exception) -> None:
    bot = AsyncMock()
    bot.get_file.side_effect = error

    with pytest.raises(FileDownloadError):
        await download_document(bot, "id", max_size=LIMIT)


@pytest.mark.parametrize("error", [ClientConnectionError("reset"), TimeoutError()])
async def test_download_errors(error: Exception) -> None:
    bot = _bot(file_size=10)
    bot.download_file.side_effect = error

    with pytest.raises(FileDownloadError):
        await download_document(bot, "id", max_size=LIMIT)


async def test_missing_file_path() -> None:
    bot = _bot(file_size=10, file_path=None)

    with pytest.raises(FileDownloadError):
        await download_document(bot, "id", max_size=LIMIT)
    bot.download_file.assert_not_awaited()
