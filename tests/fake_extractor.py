"""Подмена TextExtractor для тестов сервиса и хендлеров."""

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable

from app.extraction.base import Extraction, ExtractionResult, ExtractorBusyError

OK = ExtractionResult(text="Текст задания", extractor="fake")


class FakeExtractor:
    """TextExtractor с заданными ответами; запоминает, что ему передали."""

    def __init__(self, *answers: Extraction | Callable[[], Extraction]) -> None:
        self.answers = list(answers) or [OK]
        self.calls: list[tuple[str, bytes]] = []
        self.busy = False
        self.on_extract: Callable[[], Awaitable[object]] | None = None

    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        if self.busy:
            raise ExtractorBusyError
        yield

    async def extract(self, extension: str, data: bytes) -> Extraction:
        self.calls.append((extension, data))
        if self.on_extract is not None:
            await self.on_extract()
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return answer() if callable(answer) else answer
