"""Нормализация текста, TXT и протокол обмена с дочерним процессом (без процессов)."""

import json

import pytest

from app.bot.texts import format_pages
from app.domain.enums import MaterialFailureReason, TextTruncationReason
from app.extraction.base import (
    ExtractionFailure,
    ExtractionLimits,
    ExtractionResult,
    ExtractionWarning,
    WarningCode,
    WarningKind,
)
from app.extraction.normalize import looks_unreadable, normalize_text
from app.extraction.protocol import (
    WORKER_FAILURE_REASONS,
    ProtocolError,
    decode_extraction,
    encode_extraction,
)
from app.extraction.txt_text import extract_txt_text

R = MaterialFailureReason
LIMITS = ExtractionLimits()


# --- Нормализация ----------------------------------------------------------------------


def test_normalize_line_breaks_and_spaces() -> None:
    assert normalize_text("  a  \r\nb\rc\n\n\n\n d \t\n") == "a\nb\nc\n\n d"


def test_normalize_removes_control_chars_and_surrogates() -> None:
    raw = "a\x00b\x01c\x7f\x9fd\ud800e\tf\x0cg"
    assert normalize_text(raw) == "abcde\tf\x0cg"


def test_normalize_nfc() -> None:
    # «ё» из двух кодовых точек (е + U+0308) -> одна.
    assert normalize_text("ёлка") == "ёлка"


def test_normalized_text_is_storable() -> None:
    text = normalize_text("x\x00\ud83d" + "й" * 10)
    text.encode("utf-8")
    assert "\x00" not in text


def test_unreadable_heuristic() -> None:
    assert looks_unreadable("\x03\x04\x05\x06" * 20 + "abc")
    assert looks_unreadable("" * 20)
    assert not looks_unreadable("Обычный текст, вполне читаемый. " * 3)
    # Короткий текст не оценивается.
    assert not looks_unreadable("\x03\x04")


def test_warning_kinds() -> None:
    assert ExtractionWarning(WarningCode.NO_TEXT).kind is WarningKind.FACT
    assert ExtractionWarning(WarningCode.IMAGE_ONLY_PAGES).kind is WarningKind.HEURISTIC
    assert ExtractionWarning(WarningCode.PAGES_WITHOUT_TEXT, (2,)).to_json() == {
        "code": "pages_without_text",
        "kind": "fact",
        "pages": [2],
    }


def test_format_pages() -> None:
    assert format_pages((5, 1, 2, 3)) == "1–3, 5"
    assert format_pages((7,)) == "7"
    assert format_pages(tuple(range(1, 40, 2))).endswith(" и другие")


# --- TXT --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        "Ёлка — «№ 1»\r\nвторая строка".encode(),
        "Ёлка — «№ 1»\r\nвторая строка".encode("utf-8-sig"),
        "Ёлка — «№ 1»\r\nвторая строка".encode("utf-16"),
        "Ёлка — «№ 1»\r\nвторая строка".encode("cp1251"),
    ],
    ids=["utf-8", "utf-8-sig", "utf-16", "cp1251"],
)
def test_txt_encodings(data: bytes) -> None:
    result = extract_txt_text(data, LIMITS)
    assert isinstance(result, ExtractionResult)
    assert result.text == "Ёлка — «№ 1»\nвторая строка"
    assert result.warnings == ()
    assert result.truncation is None


def test_txt_whitespace_only_is_processed_without_text() -> None:
    result = extract_txt_text(b"  \r\n\t ", LIMITS)
    assert isinstance(result, ExtractionResult)
    assert result.text == ""
    assert result.has_warning(WarningCode.NO_TEXT)


def test_txt_char_limit() -> None:
    result = extract_txt_text(("я" * 600_000).encode(), LIMITS)
    assert isinstance(result, ExtractionResult)
    assert len(result.text) == 500_000
    assert result.truncation is TextTruncationReason.CHAR_LIMIT


def test_txt_binary_is_corrupted() -> None:
    result = extract_txt_text(b"abc\x00def", LIMITS)
    assert isinstance(result, ExtractionFailure)
    assert result.reason is R.CORRUPTED


# --- Протокол ---------------------------------------------------------------------------


def test_protocol_roundtrip_result() -> None:
    result = ExtractionResult(
        text="Ёлка\fвторая страница",
        extractor="pypdfium2 5.13.0 / pdfium 153",
        truncation=TextTruncationReason.PAGE_LIMIT,
        page_count=3,
        pages_processed=2,
        warnings=(ExtractionWarning(WarningCode.PAGES_WITHOUT_TEXT, (2,)),),
    )
    encoded = encode_extraction(result)
    assert encoded.isascii()
    assert decode_extraction(encoded, LIMITS) == result


def test_protocol_roundtrip_failure() -> None:
    failure = ExtractionFailure(R.ENCRYPTED, "pdfium err_code=4 \ud800")
    decoded = decode_extraction(encode_extraction(failure), LIMITS)
    assert isinstance(decoded, ExtractionFailure)
    assert decoded.reason is R.ENCRYPTED


@pytest.mark.parametrize("reason", sorted(WORKER_FAILURE_REASONS))
def test_protocol_accepts_worker_failure_reasons(reason: MaterialFailureReason) -> None:
    decoded = decode_extraction(encode_extraction(ExtractionFailure(reason, "x")), LIMITS)
    assert decoded == ExtractionFailure(reason, "x")


@pytest.mark.parametrize("reason", sorted(set(R) - WORKER_FAILURE_REASONS))
def test_protocol_rejects_failure_reasons_of_parent(reason: MaterialFailureReason) -> None:
    """Хранилище, тайм-аут, аварийное завершение, прерывание определяет только родитель."""
    raw = json.dumps({"ok": False, "reason": reason.value, "detail": "x"}).encode()
    with pytest.raises(ProtocolError):
        decode_extraction(raw, LIMITS)


def _payload(**overrides: object) -> bytes:
    payload: dict[str, object] = {
        "ok": True,
        "text": "abc",
        "extractor": "txt",
        "truncation": None,
        "page_count": None,
        "pages_processed": None,
        "warnings": [],
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        "кириллица".encode(),
        b"[]",
        b"{}",
        _payload(ok="yes"),
        _payload(text=1),
        _payload(text="a" * 500_001),
        _payload(text="a\x00b"),
        _payload(text="\ud800"),
        _payload(extractor=""),
        _payload(truncation="unknown"),
        _payload(page_count=2),
        _payload(page_count=1, pages_processed=2),
        _payload(page_count=-1, pages_processed=0),
        _payload(warnings={}),
        _payload(warnings=[{"code": "nope", "pages": []}]),
        _payload(warnings=[{"code": "no_text", "pages": [1]}]),
        json.dumps({"ok": False, "reason": "made_up", "detail": "x"}).encode(),
        json.dumps({"ok": False, "reason": "corrupted"}).encode(),
    ],
    # Длинные образцы в id не помещаются в переменную окружения PYTEST_CURRENT_TEST.
    ids=lambda raw: f"{len(raw)}b",
)
def test_protocol_rejects_invalid_output(raw: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode_extraction(raw, LIMITS)
