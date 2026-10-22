"""CSV framing and values, the export schema (FR-092, FR-093, api.md § Экспорт, T054)."""

import csv
import io
from decimal import Decimal

from gr.domain.document import Citation, GroundingResult
from gr.domain.enums import GroundingStatus, KnowledgeLayer, OperationType
from gr.domain.process import CoverageReport, Grounded, Operation, Process, ProcessModel
from gr.export.csv_export import OPERATION_COLUMNS, number, operations_csv, text
from gr.export.json_export import SCHEMA_VERSION, AnalysisExport, export_schema


def test_numbers_are_rounded_half_even_with_a_decimal_comma() -> None:
    assert number(Decimal("1234.565"), 2) == "1234,56"  # to even
    assert number(Decimal("1234.575"), 2) == "1234,58"
    assert number(Decimal("-0.004"), 2) == "0,00"  # no «-0,00»
    assert number(Decimal("-1089600"), 2) == "-1089600,00"  # a spreadsheet reads «-»
    assert number(None, 2) == ""


def test_a_cell_a_spreadsheet_would_run_is_written_as_text() -> None:
    for formula in ('=HYPERLINK("http://x")', "+1", "-1", "@SUM(A1)", "\tx"):
        assert text(formula) == "'" + formula
    assert text("Регистрация заявления") == "Регистрация заявления"
    assert text(None) == ""


def fact(value: str, quote: str) -> Grounded[str]:
    grounding = GroundingResult(status=GroundingStatus.VERIFIED, score=100)
    citation = Citation(fragment_id="body/3.1", quote=quote, grounding=grounding)
    return Grounded[str](value=value, citations=[citation], layer=KnowledgeLayer.FACT)


def test_operations_csv_is_utf8_with_bom_semicolons_and_crlf() -> None:
    operation = Operation(id="proc_1.op_1", seq=0, name="=Регистрация; приём",
                          action=fact("регистрирует", "регистрирует заявление"),
                          operation_type=OperationType.REGISTRATION)  # fmt: skip
    model = ProcessModel(
        processes=[Process(id="proc_1", name=fact("Выдача", "Выдача пропуска"),
                           operations=[operation])],
        coverage=CoverageReport(fragments_total=1, fragments_used=1),
    )  # fmt: skip
    data = operations_csv(model, {"body/3.1": "п. 3.1"})
    assert data.startswith("\ufeff".encode())  # Excel opens UTF-8 with a BOM correctly
    content = data.decode("utf-8-sig")
    assert content.endswith("\r\n")
    header, row = csv.reader(io.StringIO(content), delimiter=";")
    assert tuple(header) == OPERATION_COLUMNS
    found = dict(zip(header, row, strict=True))
    assert found["name"] == "'=Регистрация; приём"  # quoted by the writer, kept as text
    assert found["seq"] == "1" and found["operation_type"] == "регистрация"
    assert found["fragment_labels"] == "п. 3.1" and found["grounding"] == "цитата найдена"
    assert found["legally_significant"] == "нет" and found["review_status"] == "На проверке"
    assert found["performer"] == "" and found["deadline_raw"] == ""


def test_the_schema_comes_from_the_model() -> None:
    schema = export_schema()
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["properties"]["schema_version"]["const"] == SCHEMA_VERSION == "1.0"
    assert set(schema["required"]) >= {"document", "processes", "initiatives", "economics",
                                       "reviews", "disclaimers"}  # fmt: skip
    assert set(schema["properties"]) == set(AnalysisExport.model_fields)
    for name in ("Process", "Operation", "DecisionPoint", "Finding", "Initiative"):
        assert "layer" in schema["$defs"][name]["properties"], name  # FR-093
