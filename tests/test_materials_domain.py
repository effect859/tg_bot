import pytest

from app.domain.enums import MaterialProcessingStatus as M
from app.domain.enums import OrderStatus
from app.domain.errors import (
    InvalidMaterialTransitionError,
    MaterialRejectReason,
    UnsupportedMaterialError,
)
from app.domain.materials import (
    MATERIAL_ACCEPTING_STATUSES,
    MATERIAL_TRANSITIONS,
    MAX_MATERIAL_SIZE_BYTES,
    check_material,
    check_material_transition,
    is_supported_material,
)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.mark.parametrize(
    ("filename", "mime_type", "extension"),
    [
        ("requirements.pdf", "application/pdf", "pdf"),
        ("practice_assignment.docx", DOCX_MIME, "docx"),
        ("methodology.txt", "text/plain", "txt"),
        ("methodology.txt", "text/plain; charset=utf-8", "txt"),
        ("Report.PDF", "APPLICATION/PDF", "pdf"),
        ("отчёт по практике.v2.docx", DOCX_MIME, "docx"),
        ("sample.pdf", None, "pdf"),
        ("sample.pdf", "application/octet-stream", "pdf"),
        ("  padded.txt  ", "text/plain", "txt"),
    ],
)
def test_supported_material(filename: str, mime_type: str | None, extension: str) -> None:
    assert check_material(filename, mime_type, 1024) == extension
    assert is_supported_material(filename, mime_type, 1024)


@pytest.mark.parametrize(
    ("filename", "mime_type", "size_bytes"),
    [
        ("photo.png", "image/png", 1024),
        ("old.doc", "application/msword", 1024),
        ("virus.pdf.exe", None, 1024),
        ("no_extension", None, 1024),
        (".pdf", "application/pdf", 1024),
        ("report.", None, 1024),
        ("", None, 1024),
        ("   ", None, 1024),
        ("x" * 252 + ".pdf", None, 1024),
        # MIME не соответствует расширению.
        ("fake.pdf", "image/jpeg", 1024),
        ("fake.docx", "application/pdf", 1024),
        ("fake.txt", "application/pdf", 1024),
        # Размер.
        ("empty.pdf", "application/pdf", 0),
        ("negative.pdf", "application/pdf", -1),
        ("huge.pdf", "application/pdf", MAX_MATERIAL_SIZE_BYTES + 1),
    ],
)
def test_unsupported_material(filename: str, mime_type: str | None, size_bytes: int) -> None:
    with pytest.raises(UnsupportedMaterialError):
        check_material(filename, mime_type, size_bytes)
    assert not is_supported_material(filename, mime_type, size_bytes)


def test_max_size_is_inclusive() -> None:
    assert MAX_MATERIAL_SIZE_BYTES == 20 * 1024 * 1024
    assert check_material("big.pdf", "application/pdf", MAX_MATERIAL_SIZE_BYTES) == "pdf"


def test_transition_map_covers_every_status() -> None:
    assert set(MATERIAL_TRANSITIONS) == set(M)


@pytest.mark.parametrize(
    ("src", "dst"),
    [(src, dst) for src, targets in MATERIAL_TRANSITIONS.items() for dst in sorted(targets)],
)
def test_allowed_material_transition(src: M, dst: M) -> None:
    check_material_transition(src, dst)


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (M.PROCESSED, M.RECEIVED),
        (M.PROCESSED, M.PROCESSING),
        (M.PROCESSED, M.FAILED),
        (M.RECEIVED, M.PROCESSED),
        (M.RECEIVED, M.FAILED),
        (M.RECEIVED, M.RECEIVED),
        (M.FAILED, M.PROCESSED),
        (M.FAILED, M.RECEIVED),
        (M.PROCESSING, M.RECEIVED),
        (M.PROCESSING, M.PROCESSING),
    ],
)
def test_forbidden_material_transition(src: M, dst: M) -> None:
    with pytest.raises(InvalidMaterialTransitionError):
        check_material_transition(src, dst)


def test_failed_can_be_retried() -> None:
    assert MATERIAL_TRANSITIONS[M.FAILED] == {M.PROCESSING}
    assert MATERIAL_TRANSITIONS[M.PROCESSED] == frozenset()


def test_materials_accepted_after_consent_and_before_payment() -> None:
    assert OrderStatus.NEW not in MATERIAL_ACCEPTING_STATUSES
    assert OrderStatus.WAITING_FOR_CONSENT not in MATERIAL_ACCEPTING_STATUSES
    assert OrderStatus.WAITING_FOR_FILES in MATERIAL_ACCEPTING_STATUSES
    assert OrderStatus.WAITING_FOR_MISSING_DATA in MATERIAL_ACCEPTING_STATUSES
    for status in (OrderStatus.PAID, OrderStatus.COMPLETED, OrderStatus.CANCELLED):
        assert status not in MATERIAL_ACCEPTING_STATUSES


@pytest.mark.parametrize(
    ("filename", "mime_type", "size_bytes", "reason"),
    [
        ("", None, 1024, MaterialRejectReason.FILENAME),
        ("x" * 252 + ".pdf", None, 1024, MaterialRejectReason.FILENAME),
        ("photo.png", "image/png", 1024, MaterialRejectReason.FORMAT),
        ("no_extension", None, 1024, MaterialRejectReason.FORMAT),
        ("fake.pdf", "image/jpeg", 1024, MaterialRejectReason.MIME),
        ("empty.pdf", "application/pdf", 0, MaterialRejectReason.EMPTY),
        ("huge.pdf", "application/pdf", MAX_MATERIAL_SIZE_BYTES + 1, MaterialRejectReason.SIZE),
    ],
)
def test_rejection_reason(
    filename: str, mime_type: str | None, size_bytes: int, reason: MaterialRejectReason
) -> None:
    with pytest.raises(UnsupportedMaterialError) as exc_info:
        check_material(filename, mime_type, size_bytes)
    assert exc_info.value.reason is reason


def test_unknown_size_skips_size_check() -> None:
    # До скачивания Telegram может не сообщить размер: проверяется всё остальное.
    assert check_material("a.pdf", "application/pdf", None) == "pdf"
    with pytest.raises(UnsupportedMaterialError):
        check_material("a.png", None, None)
