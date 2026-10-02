import itertools

import pytest

from app.domain.enums import MaterialFailureReason as R
from app.domain.enums import MaterialProcessingStatus as M
from app.domain.enums import OrderStatus
from app.domain.errors import (
    InvalidMaterialTransitionError,
    MaterialRejectReason,
    MaterialsNotReadyError,
    MaterialsNotReadyReason,
    UnsupportedMaterialError,
)
from app.domain.materials import (
    MATERIAL_ACCEPTING_STATUSES,
    MATERIAL_TRANSITIONS,
    MAX_MATERIAL_SIZE_BYTES,
    MAX_PROCESSING_ATTEMPTS,
    MaterialCompletionState,
    check_material,
    check_material_transition,
    check_materials_ready,
    is_retryable_failure,
    is_supported_material,
    material_completion_state,
)

C = MaterialCompletionState

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


def test_materials_accepted_only_while_waiting_for_files() -> None:
    """Stage 2.6: после завершения приёма (ANALYZING_MATERIALS и дальше) файлы не принимаются."""
    assert frozenset({OrderStatus.WAITING_FOR_FILES}) == MATERIAL_ACCEPTING_STATUSES


# --- Завершение приёма материалов --------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "reason", "attempts", "char_count", "expected"),
    [
        (M.RECEIVED, None, 0, None, C.PENDING),
        (M.PROCESSING, None, 1, None, C.PENDING),
        (M.FAILED, R.STORAGE_ERROR, 1, None, C.PENDING),
        (M.FAILED, R.DATABASE_ERROR, MAX_PROCESSING_ATTEMPTS - 1, None, C.PENDING),
        (M.FAILED, R.STORAGE_ERROR, MAX_PROCESSING_ATTEMPTS, None, C.FAILED),
        (M.FAILED, R.CORRUPTED, 1, None, C.FAILED),
        (M.FAILED, R.RESULT_REJECTED, 1, None, C.FAILED),
        (M.PROCESSED, None, 1, 120, C.WITH_TEXT),
        (M.PROCESSED, None, 1, 0, C.WITHOUT_TEXT),
        (M.PROCESSED, None, 1, None, C.WITHOUT_TEXT),
    ],
)
def test_material_completion_state(
    status: M, reason: R | None, attempts: int, char_count: int | None, expected: C
) -> None:
    assert material_completion_state(status, reason, attempts, char_count) is expected


@pytest.mark.parametrize(
    ("states", "reason", "pending"),
    [
        ([], MaterialsNotReadyReason.NO_MATERIALS, 0),
        ([C.PENDING, C.PENDING], MaterialsNotReadyReason.PENDING, 2),
        ([C.WITH_TEXT, C.PENDING], MaterialsNotReadyReason.PENDING, 1),
        ([C.FAILED, C.FAILED], MaterialsNotReadyReason.NO_TEXT, 0),
        ([C.WITHOUT_TEXT, C.FAILED], MaterialsNotReadyReason.NO_TEXT, 0),
    ],
)
def test_materials_not_ready(
    states: list[C], reason: MaterialsNotReadyReason, pending: int
) -> None:
    with pytest.raises(MaterialsNotReadyError) as caught:
        check_materials_ready(states)

    assert (caught.value.reason, caught.value.pending) == (reason, pending)


@pytest.mark.parametrize(
    "states",
    [[C.WITH_TEXT], [C.WITH_TEXT, C.FAILED, C.WITHOUT_TEXT], [C.FAILED, C.WITH_TEXT]],
)
def test_materials_ready(states: list[C]) -> None:
    check_materials_ready(states)


def test_finished_states_never_change() -> None:
    """Всё, что не PENDING, — окончательно: обработка и восстановление его не тронут.

    На этом держится завершение приёма без блокировки материалов.
    """
    for status, reason, attempts in itertools.product(
        M, [None, *R], range(MAX_PROCESSING_ATTEMPTS + 1)
    ):
        for char_count in (None, 0, 10):
            state = material_completion_state(status, reason, attempts, char_count)
            if state is C.PENDING:
                continue
            assert status in (M.PROCESSED, M.FAILED)
            if status is M.PROCESSED:
                assert MATERIAL_TRANSITIONS[M.PROCESSED] == frozenset()
            else:
                assert not is_retryable_failure(reason, attempts)


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
