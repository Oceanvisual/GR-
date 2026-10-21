"""Validation rules of data-model §1, §2, §8, §9 (T007)."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from gr.domain.document import (
    Block,
    Citation,
    Document,
    Fragment,
    GroundingResult,
    IngestionDiagnostics,
    OcrInfo,
)
from gr.domain.enums import (
    DocFormat,
    FragmentKind,
    GroundingStatus,
    OcrStatus,
    ReviewStatus,
    RunStatus,
    SolutionCategory,
    StageName,
    StageStatus,
    StructureMode,
    TextLayerStatus,
)
from gr.domain.ids import new_id, new_ulid
from gr.domain.review import ReviewAction, ReviewEvent, ReviewState, next_review_status
from gr.domain.run import AnalysisRun, ErrorInfo, InvalidTransitionError, StageProgress

NOW = datetime(2026, 10, 21, tzinfo=UTC)


def _fragment(**overrides: Any) -> Fragment:
    fields: dict[str, Any] = {
        "id": "body/1",
        "doc_id": "doc_x",
        "kind": FragmentKind.POINT,
        "label": "п. 1",
        "text": "Текст.",
        "start": 0,
        "end": 6,
        "order": 0,
    }
    return Fragment(**(fields | overrides))


def _document(**overrides: Any) -> Document:
    fields: dict[str, Any] = {
        "id": new_id("doc"),
        "filename": "reg.docx",
        "format": DocFormat.DOCX,
        "size_bytes": 10,
        "sha256": "a" * 64,
        "uploaded_at": NOW,
        "uploaded_by": "expert",
        "char_count": 100,
        "diagnostics": IngestionDiagnostics(text_layer=TextLayerStatus.OK),
        "structure_mode": StructureMode.NUMBERED,
    }
    return Document(**(fields | overrides))


def _run(**overrides: Any) -> AnalysisRun:
    fields: dict[str, Any] = {
        "id": new_id("run"),
        "doc_id": "doc_x",
        "created_by": "expert",
        "created_at": NOW,
    }
    return AnalysisRun(**(fields | overrides))


def _event(action: ReviewAction, **overrides: Any) -> ReviewEvent:
    fields: dict[str, Any] = {
        "id": "ev_1",
        "run_id": "run_x",
        "entity_type": "operation",
        "entity_id": "proc_1.op_1",
        "action": action,
        "user": "expert",
        "created_at": NOW,
    }
    return ReviewEvent(**(fields | overrides))


# --- ids and enums ---------------------------------------------------------------------------


def test_ulid_is_sortable_and_unique() -> None:
    first, second = new_ulid(1_000), new_ulid(2_000)
    assert len(first) == 26 and first < second
    assert len({new_ulid() for _ in range(1000)}) == 1000
    assert new_id("doc").startswith("doc_")


def test_ulids_in_one_millisecond_keep_creation_order() -> None:
    ids = [new_ulid(5_000) for _ in range(1000)]
    assert ids == sorted(ids) and len(set(ids)) == 1000


def test_category_order_for_tech_guard() -> None:
    ordered = sorted(SolutionCategory, key=lambda c: c.rank)
    assert [c.value for c in ordered] == [
        "no_change",
        "classic_automation",
        "ai_nlp",
        "hybrid",
        "llm_rag",
        "ai_agent",
    ]


def test_only_verified_and_relocated_pass() -> None:
    assert {s for s in GroundingStatus if s.passes} == {
        GroundingStatus.VERIFIED,
        GroundingStatus.RELOCATED,
    }


# --- models are frozen and strict ------------------------------------------------------------


def test_models_are_frozen() -> None:
    fragment = _fragment()
    with pytest.raises(ValidationError):
        fragment.text = "другое"  # type: ignore[misc]


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError, match="extra"):
        _fragment(surprise=1)


# --- document and structure ------------------------------------------------------------------


@pytest.mark.parametrize(("start", "end"), [(5, 5), (6, 5)])
def test_fragment_span_must_be_positive(start: int, end: int) -> None:
    with pytest.raises(ValidationError, match="start must be < end"):
        _fragment(start=start, end=end)


def test_fragment_rejects_zero_page_and_self_parent() -> None:
    with pytest.raises(ValidationError, match="1-based"):
        _fragment(pages=[0])
    with pytest.raises(ValidationError, match="own parent"):
        _fragment(parent_id="body/1")


@pytest.mark.parametrize("name", ["../etc/passwd", "a/b.docx", "a\\b.docx", "..", "x" * 256, ""])
def test_document_filename_without_path(name: str) -> None:
    with pytest.raises(ValidationError):
        _document(filename=name)


@pytest.mark.parametrize("digest", ["A" * 64, "a" * 63, "g" * 64])
def test_document_sha256_format(digest: str) -> None:
    with pytest.raises(ValidationError, match="sha256"):
        _document(sha256=digest)


def test_document_id_prefix() -> None:
    with pytest.raises(ValidationError):
        _document(id="run_" + new_ulid())


def test_block_bbox_ordered() -> None:
    assert Block(text="a", page=1, bbox=(0, 0, 10, 10)).bbox == (0, 0, 10, 10)
    with pytest.raises(ValidationError, match="bbox"):
        Block(text="a", bbox=(10, 0, 0, 10))
    with pytest.raises(ValidationError):
        Block(text="a", page=0)


@pytest.mark.parametrize(
    ("text_layer", "ocr_status", "expected"),
    [
        (TextLayerStatus.OK, OcrStatus.NOT_NEEDED, True),
        (TextLayerStatus.PARTIAL, OcrStatus.NOT_NEEDED, True),
        (TextLayerStatus.PARTIAL, OcrStatus.RUNNING, False),
        (TextLayerStatus.NEEDS_OCR, OcrStatus.QUEUED, False),
        (TextLayerStatus.NEEDS_OCR, OcrStatus.RUNNING, False),
        (TextLayerStatus.NEEDS_OCR, OcrStatus.FAILED, False),
        (TextLayerStatus.NEEDS_OCR, OcrStatus.UNAVAILABLE, False),
        (TextLayerStatus.NEEDS_OCR, OcrStatus.DONE, True),
        (TextLayerStatus.EMPTY, OcrStatus.NOT_NEEDED, False),
        (TextLayerStatus.EMPTY, OcrStatus.DONE, True),
    ],
)
def test_analyzable_is_derived(
    text_layer: TextLayerStatus, ocr_status: OcrStatus, expected: bool
) -> None:
    diagnostics = IngestionDiagnostics(text_layer=text_layer, ocr=OcrInfo(status=ocr_status))
    assert diagnostics.analyzable is expected


def test_analyzable_cannot_be_forged_and_round_trips() -> None:
    forged = IngestionDiagnostics.model_validate(
        {"text_layer": "needs_ocr", "ocr": {"status": "failed"}, "analyzable": True}
    )
    assert forged.analyzable is False
    restored = IngestionDiagnostics.model_validate_json(forged.model_dump_json())
    assert restored == forged


def test_document_round_trips_through_json() -> None:
    document = _document()
    assert Document.model_validate_json(document.model_dump_json()) == document


def test_grounding_result_rules() -> None:
    with pytest.raises(ValidationError):
        GroundingResult(status=GroundingStatus.VERIFIED, score=101)
    with pytest.raises(ValidationError, match="together"):
        GroundingResult(status=GroundingStatus.VERIFIED, score=100, matched_start=1)
    with pytest.raises(ValidationError, match="< matched_end"):
        GroundingResult(status=GroundingStatus.VERIFIED, score=100, matched_start=5, matched_end=5)
    with pytest.raises(ValidationError, match="original_fragment_id"):
        GroundingResult(status=GroundingStatus.RELOCATED, score=95)
    relocated = GroundingResult(
        status=GroundingStatus.RELOCATED, score=95, original_fragment_id="body/2"
    )
    with pytest.raises(ValidationError):
        Citation(fragment_id="body/1", quote="", grounding=relocated)


# --- run -------------------------------------------------------------------------------------


def test_new_run_has_all_stages_pending() -> None:
    run = _run()
    assert run.status == RunStatus.QUEUED and run.is_active
    assert [s.name for s in run.stages] == list(StageName)
    assert run.first_unfinished_stage() == StageName.DISCOVER


def test_run_stages_must_be_complete_and_ordered() -> None:
    stages = _run().stages
    with pytest.raises(ValidationError, match="stages"):
        _run(stages=list(reversed(stages)))
    with pytest.raises(ValidationError, match="stages"):
        _run(stages=stages[:-1])


ALLOWED = {
    (RunStatus.QUEUED, RunStatus.RUNNING),
    (RunStatus.QUEUED, RunStatus.CANCELLED),
    (RunStatus.RUNNING, RunStatus.SUCCEEDED),
    (RunStatus.RUNNING, RunStatus.FAILED),
    (RunStatus.RUNNING, RunStatus.INTERRUPTED),
    (RunStatus.RUNNING, RunStatus.CANCELLED),
    (RunStatus.FAILED, RunStatus.RUNNING),
    (RunStatus.INTERRUPTED, RunStatus.RUNNING),
}


@pytest.mark.parametrize("source", list(RunStatus))
@pytest.mark.parametrize("target", list(RunStatus))
def test_run_transitions(source: RunStatus, target: RunStatus) -> None:
    error = ErrorInfo(code="llm_timeout", message="таймаут")
    run = _run(status=source, error=error if source == RunStatus.FAILED else None)
    if (source, target) in ALLOWED:
        moved = run.transition(target, error=error)
        assert moved.status == target
        assert (moved.error is not None) == (target == RunStatus.FAILED)
    else:
        with pytest.raises(InvalidTransitionError):
            run.transition(target, error=error)


def test_failed_run_requires_error() -> None:
    with pytest.raises(ValidationError, match="error"):
        _run(status=RunStatus.FAILED)
    with pytest.raises(ValueError, match="error"):
        _run(status=RunStatus.RUNNING).transition(RunStatus.FAILED)


def test_resume_starts_from_first_unfinished_stage() -> None:
    run = _run()
    for name in (StageName.DISCOVER, StageName.DETAIL):
        run = run.with_stage(run.stage(name).model_copy(update={"status": StageStatus.SUCCEEDED}))
    run = run.with_stage(
        run.stage(StageName.VALIDATE).model_copy(update={"status": StageStatus.FAILED})
    )
    assert run.first_unfinished_stage() == StageName.VALIDATE


def test_stage_progress_bounded() -> None:
    with pytest.raises(ValidationError, match="exceed"):
        StageProgress(done=3, total=2)


# --- review ----------------------------------------------------------------------------------


def test_reject_requires_comment() -> None:
    with pytest.raises(ValidationError, match="comment"):
        _event("reject")
    with pytest.raises(ValidationError, match="comment"):
        _event("reject", comment="   ")
    assert _event("reject", comment="не процесс").comment == "не процесс"


def test_edit_requires_patch() -> None:
    with pytest.raises(ValidationError, match="patch"):
        _event("edit")


REVIEW_ALLOWED = {
    (ReviewStatus.PENDING, "confirm"): ReviewStatus.CONFIRMED,
    (ReviewStatus.PENDING, "edit"): ReviewStatus.EDITED,
    (ReviewStatus.PENDING, "reject"): ReviewStatus.REJECTED,
    (ReviewStatus.CONFIRMED, "edit"): ReviewStatus.EDITED,
    (ReviewStatus.CONFIRMED, "reject"): ReviewStatus.REJECTED,
    (ReviewStatus.CONFIRMED, "reopen"): ReviewStatus.PENDING,
    (ReviewStatus.EDITED, "edit"): ReviewStatus.EDITED,
    (ReviewStatus.EDITED, "reject"): ReviewStatus.REJECTED,
    (ReviewStatus.EDITED, "reopen"): ReviewStatus.PENDING,
    (ReviewStatus.REJECTED, "reopen"): ReviewStatus.PENDING,
}


@pytest.mark.parametrize("current", list(ReviewStatus))
@pytest.mark.parametrize("action", ["confirm", "edit", "reject", "reopen"])
def test_review_transitions(current: ReviewStatus, action: ReviewAction) -> None:
    expected = REVIEW_ALLOWED.get((current, action))
    if expected is None:
        with pytest.raises(InvalidTransitionError):
            next_review_status(current, action)
    else:
        assert next_review_status(current, action) == expected


def test_set_gate_keeps_review_status() -> None:
    for status in ReviewStatus:
        assert next_review_status(status, "set_gate") == status


def test_review_state_tracks_edited_fields() -> None:
    state = ReviewState()
    state = state.apply(_event("edit", id="ev_1", patch={"name": "A", "performer": "Отдел"}))
    state = state.apply(_event("edit", id="ev_2", patch={"name": "B"}))
    assert state.status == ReviewStatus.EDITED
    assert state.edited_fields == ["name", "performer"]
    assert state.last_event_id == "ev_2"
    reopened = state.apply(_event("reopen", id="ev_3"))
    assert reopened.status == ReviewStatus.PENDING
    assert reopened.edited_fields == ["name", "performer"]
