"""Small valid initiative/finding fixtures shared by US3 tests."""

from typing import Any

from gr.domain.enums import ProblemPattern, SolutionCategory
from gr.domain.initiative import Finding, Initiative


def finding(pattern: ProblemPattern, key: str = "f1", process: str = "p") -> Finding:
    return Finding(
        id=key, process_id=process, operation_ids=["op1"], pattern=pattern, description="x"
    )


def initiative(category: SolutionCategory, **updates: Any) -> Initiative:
    rating = {"level": "medium", "justification": "x"}
    fields: dict[str, Any] = {
        "id": "i1",
        "process_id": "p",
        "finding_ids": ["f1"],
        "operation_ids": ["op1"],
        "title": "x",
        "description": "x",
        "solution_category": category,
        "architecture_idea": "x",
        "tech_rationale": "a detailed justification",
        "alternatives_considered": [{"category": "classic_automation", "why_not": "x"}],
        "regulatory_mode": "within_current",
        "automation_level": "assistive",
        "automation_level_proposed": "assistive",
        "reduction_potential": [{"operation_id": "op1", "level": "medium", "justification": "x"}],
        "ratings": {
            **{
                key: rating
                for key in (
                    "feasibility",
                    "data_availability",
                    "integration_complexity",
                    "legal_risk",
                    "operational_risk",
                )
            },
            "duration": {"bucket": "m3_6", "justification": "x"},
        },
    }
    return Initiative.model_validate(fields | updates)
