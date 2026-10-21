"""Enumerations from data-model.md §1."""

from enum import StrEnum


class KnowledgeLayer(StrEnum):
    FACT = "fact"
    INTERPRETATION = "interpretation"
    HYPOTHESIS = "hypothesis"
    ASSUMPTION = "assumption"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    EDITED = "edited"
    REJECTED = "rejected"


class GroundingStatus(StrEnum):
    VERIFIED = "verified"
    RELOCATED = "relocated"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"
    INVALID_QUOTE = "invalid_quote"

    @property
    def passes(self) -> bool:
        return self in (GroundingStatus.VERIFIED, GroundingStatus.RELOCATED)


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DocFormat(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"
    HTML = "html"
    DOC = "doc"
    RTF = "rtf"
    ODT = "odt"


class TextLayerStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    NEEDS_OCR = "needs_ocr"
    EMPTY = "empty"


class OcrStatus(StrEnum):
    NOT_NEEDED = "not_needed"
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class TextSource(StrEnum):
    NATIVE = "native"
    OCR = "ocr"


class NumberingProfile(StrEnum):
    REGULATION = "regulation"
    LAW = "law"
    GENERIC = "generic"


class FragmentKind(StrEnum):
    ROOT = "root"
    PREAMBLE = "preamble"
    APPROVED_DOCUMENT = "approved_document"
    SECTION = "section"
    CHAPTER = "chapter"
    PARAGRAPH_SECTION = "paragraph_section"
    SUBSECTION = "subsection"
    ARTICLE = "article"
    PART = "part"
    POINT = "point"
    SUBPOINT = "subpoint"
    LIST_ITEM = "list_item"
    PARAGRAPH = "paragraph"
    APPENDIX = "appendix"


class StructureMode(StrEnum):
    NUMBERED = "numbered"
    PARAGRAPHS = "paragraphs"


class OperationType(StrEnum):
    INTAKE = "intake"
    REGISTRATION = "registration"
    COMPLETENESS_CHECK = "completeness_check"
    DATA_VERIFICATION = "data_verification"
    INTERAGENCY_REQUEST = "interagency_request"
    DOCUMENT_PREPARATION = "document_preparation"
    APPROVAL = "approval"
    DECISION = "decision"
    SIGNING = "signing"
    NOTIFICATION = "notification"
    ISSUANCE = "issuance"
    RECORD_KEEPING = "record_keeping"
    CONTROL = "control"
    OTHER = "other"


class DeadlineUnit(StrEnum):
    MINUTES = "minutes"
    HOURS = "hours"
    WORKING_DAYS = "working_days"
    CALENDAR_DAYS = "calendar_days"
    DAYS = "days"
    MONTHS = "months"
    NEXT_WORKING_DAY = "next_working_day"
    OTHER = "other"


class ProblemPattern(StrEnum):
    MANUAL_DOCUMENT_PROCESSING = "manual_document_processing"
    DUPLICATE_DATA_ENTRY = "duplicate_data_entry"
    LOOKUP_AND_RECONCILIATION = "lookup_and_reconciliation"
    REQUEST_CLASSIFICATION = "request_classification"
    TEMPLATE_DOCUMENT_PREPARATION = "template_document_preparation"
    DUPLICATE_CHECKS = "duplicate_checks"
    LONG_APPROVALS = "long_approvals"
    INTERDEPARTMENTAL_HANDOFF = "interdepartmental_handoff"
    LARGE_TEXT_ANALYSIS = "large_text_analysis"
    OTHER = "other"


class SolutionCategory(StrEnum):
    CLASSIC_AUTOMATION = "classic_automation"
    AI_NLP = "ai_nlp"
    LLM_RAG = "llm_rag"
    AI_AGENT = "ai_agent"
    HYBRID = "hybrid"
    NO_CHANGE = "no_change"

    @property
    def rank(self) -> int:
        """Order for the tech guard: no_change < classic < ai_nlp < hybrid < llm_rag < ai_agent."""
        return _CATEGORY_RANK[self]


_CATEGORY_RANK = {
    SolutionCategory.NO_CHANGE: 0,
    SolutionCategory.CLASSIC_AUTOMATION: 1,
    SolutionCategory.AI_NLP: 2,
    SolutionCategory.HYBRID: 3,
    SolutionCategory.LLM_RAG: 4,
    SolutionCategory.AI_AGENT: 5,
}


class RegulatoryMode(StrEnum):
    WITHIN_CURRENT = "within_current"
    REQUIRES_CHANGE = "requires_change"


class AutomationLevel(StrEnum):
    ASSISTIVE = "assistive"
    HUMAN_APPROVAL = "human_approval"
    AUTONOMOUS = "autonomous"


class Level5(StrEnum):
    VERY_LOW = "very_low"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"


class DurationBucket(StrEnum):
    LT_3M = "lt_3m"
    M3_6 = "m3_6"
    M6_12 = "m6_12"
    GT_12M = "gt_12m"


class LegalGate(StrEnum):
    ALLOWED = "allowed"
    ALLOWED_WITH_HITL = "allowed_with_hitl"
    REQUIRES_REGULATORY_CHANGE = "requires_regulatory_change"
    BLOCKED = "blocked"


class InitiativeRelation(StrEnum):
    ALTERNATIVE = "alternative"
    COMPLEMENTARY = "complementary"


class PriorityGroup(StrEnum):
    QUICK_WIN = "quick_win"
    STRATEGIC = "strategic"
    COMPLEX = "complex"
    NOT_RECOMMENDED = "not_recommended"


class Scenario(StrEnum):
    CONSERVATIVE = "conservative"
    BASE = "base"
    OPTIMISTIC = "optimistic"


class AssumptionSource(StrEnum):
    DEFAULT_PROFILE = "default_profile"
    USER = "user"
    DOCUMENT = "document"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class StageName(StrEnum):
    DISCOVER = "discover"
    DETAIL = "detail"
    VALIDATE = "validate"
    OPPORTUNITIES = "opportunities"
    POSTPROCESS = "postprocess"


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
