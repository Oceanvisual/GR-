"""LLM schemas are flat and strict-compatible (R-08, T027)."""

import json
from collections.abc import Iterator
from typing import Any

import pytest

from gr.extraction.schemas import (
    NO_NUMBER,
    STAGE_SCHEMAS,
    DetailOut,
    DiscoveryOut,
    Fact,
    LLMSchema,
    OpportunitiesOut,
    ProcessOut,
)
from gr.llm.cache import strict_json_schema

FORBIDDEN_KEYWORDS = {"anyOf", "oneOf", "allOf", "not", "minLength", "maxLength", "pattern",
                      "minItems", "maxItems", "format", "const"}  # fmt: skip
MAX_SCHEMA_CHARS = 12_000  # the schema is pasted into the prompt in json_object mode


def nodes(schema: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every subschema; the keys of `properties` and `$defs` are names, not keywords."""
    yield schema
    for key in ("properties", "$defs"):
        for sub in schema.get(key, {}).values():
            yield from nodes(sub)
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        for sub in schema.get(key, []):
            yield from nodes(sub)
    for key in ("items", "additionalProperties", "not"):
        if isinstance(schema.get(key), dict):
            yield from nodes(schema[key])


@pytest.mark.parametrize("schema", STAGE_SCHEMAS, ids=lambda s: s.__name__)
def test_strict_schema_is_closed_and_flat(schema: type[LLMSchema]) -> None:
    strict = strict_json_schema(schema)
    for node in nodes(strict):
        assert not FORBIDDEN_KEYWORDS & node.keys(), node
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert node["required"] == list(node["properties"])
        assert node.get("type") != "null"


@pytest.mark.parametrize("schema", STAGE_SCHEMAS, ids=lambda s: s.__name__)
def test_pydantic_schema_has_no_nullable_fields(schema: type[LLMSchema]) -> None:
    raw = schema.model_json_schema()
    assert not any("anyOf" in node for node in nodes(raw))


@pytest.mark.parametrize("schema", STAGE_SCHEMAS, ids=lambda s: s.__name__)
def test_schema_fits_into_a_prompt(schema: type[LLMSchema]) -> None:
    text = json.dumps(strict_json_schema(schema), ensure_ascii=False)
    assert len(text) <= MAX_SCHEMA_CHARS, len(text)


FACT = {"text": "регистрирует заявление", "ref": "F12", "quote": "регистрирует заявление"}


def test_discovery_with_optional_keys_omitted() -> None:
    answer = DiscoveryOut.model_validate(
        {"processes": [{"key": "p1", "name": "Приём", "core_refs": ["F3"], "evidence": FACT}]}
    )
    process = answer.processes[0]
    assert (process.trigger, process.parent_key, process.related_refs) == ("", "", [])
    assert answer.non_process_refs == [] and answer.notes == ""


def test_detail_with_optional_keys_omitted() -> None:
    answer = DetailOut.model_validate(
        {
            "process": {"name": FACT},
            "operations": [{"key": "o1", "name": "Регистрация", "action": FACT}],
            "unknown_key": "ignored",
        }
    )
    operation = answer.operations[0]
    assert operation.performer == [] and operation.deadline == []
    assert operation.operation_type == "other" and operation.legally_significant is False
    assert answer.decision_points == []


def test_deadline_without_a_number_is_minus_one() -> None:
    answer = DetailOut.model_validate(
        {
            "process": {
                "name": FACT,
                "deadlines": [{"raw": FACT, "unit": "next_working_day"}],
            },
            "operations": [],
        }
    )
    assert answer.process.deadlines[0].value == NO_NUMBER


def test_opportunities_defaults() -> None:
    answer = OpportunitiesOut.model_validate({})
    assert answer.initiatives == [] and answer.no_change.applies is False


def test_enum_values_are_validated() -> None:
    with pytest.raises(ValueError, match="operation_type"):
        DetailOut.model_validate(
            {
                "process": {"name": FACT},
                "operations": [
                    {"key": "o1", "name": "x", "action": FACT, "operation_type": "teleport"}
                ],
            }
        )


def test_one_element_answered_without_its_list_is_wrapped() -> None:
    # Haiku answers «0 или 1 элемент» fields with the object itself now and then (T059).
    fact = {"text": "заявление", "ref": "F3", "quote": "поступление заявления"}
    process = ProcessOut.model_validate({"name": fact, "trigger": fact, "result": [fact]})
    assert process.trigger == [Fact.model_validate(fact)]
    assert process.result == [Fact.model_validate(fact)] and process.goal == []
