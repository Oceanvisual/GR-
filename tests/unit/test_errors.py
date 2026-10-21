"""Error format and status table of contracts/api.md (T010)."""

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from gr.errors import ERRORS, AppError, install_error_handlers

API_MD = Path(__file__).resolve().parents[2] / "specs" / "001-npa-ai-analyzer" / "contracts"


def _contract_table() -> dict[str, int]:
    text = (API_MD / "api.md").read_text(encoding="utf-8")
    section = text.split("### Формат ошибки", 1)[1].split("\n## ", 1)[0]
    table: dict[str, int] = {}
    for line in section.splitlines():
        if m := re.match(r"^\| (\d{3}) \| (.+?) \|", line):
            for code in re.findall(r"`(\w+)`", m.group(2)):
                table[code] = int(m.group(1))
    return table


def test_error_table_matches_contract() -> None:
    contract = _contract_table()
    assert contract, "error table not found in api.md"
    assert {code: status for code, (status, _) in ERRORS.items()} == contract


class Body(BaseModel):
    count: int


def _app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/raise/{code}")
    async def raise_code(code: str) -> None:
        raise AppError(code, details={"hint": 1})

    @app.post("/body")
    async def body(payload: Body) -> int:
        return payload.count

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("секретная деталь реализации")

    return app


@pytest.mark.parametrize("code", sorted(ERRORS))
def test_each_code_serialized_with_status(code: str) -> None:
    response = TestClient(_app()).get(f"/raise/{code}")
    assert response.status_code == ERRORS[code][0]
    error = response.json()["error"]
    assert error["code"] == code
    assert error["message"] == ERRORS[code][1]
    assert error["details"] == {"hint": 1}


def test_unknown_code_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="unknown error code"):
        AppError("teapot")


def test_validation_error_hides_input() -> None:
    response = TestClient(_app()).post("/body", json={"count": "секрет"})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert "секрет" not in response.text
    assert error["details"]["errors"][0]["loc"] == ["body", "count"]


def test_not_found_route() -> None:
    response = TestClient(_app()).get("/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_unhandled_exception_has_no_details() -> None:
    response = TestClient(_app(), raise_server_exceptions=False).get("/boom")
    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "internal_error",
        "message": ERRORS["internal_error"][1],
        "details": {},
    }
    assert "секретная" not in response.text
