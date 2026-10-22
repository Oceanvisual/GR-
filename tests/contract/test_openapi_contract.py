"""The OpenAPI of the service against contracts/api.md (T055).

The same routes with the same methods and success codes in both; errors in the common format
of api.md (an invalid request is a 400 validation_error, never FastAPI's 422); every route of
/api/v1 behind authentication (R-15).
"""

import re
from collections.abc import Callable
from itertools import takewhile
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gr.app import create_app
from gr.auth import hash_password
from gr.settings import Settings

API_MD = Path(__file__).resolve().parents[2] / "specs" / "001-npa-ai-analyzer" / "contracts"
HEADING = re.compile(r"^### `(GET|POST|PUT|PATCH|DELETE) (/[^`\s?]+)")
ROW = re.compile(r"^\| (GET|POST|PUT|PATCH|DELETE) \| `(/[^`?]+)")
SUCCESS = re.compile(r"`(2\d\d)\b")
# Described in api.md, built by a later task.
PENDING = {("POST", "/api/v1/documents/{doc_id}/ocr"): "T070"}

Route = tuple[str, str]


def documented() -> dict[Route, int]:
    """(method, path) → the success code api.md names first for it (200 if none)."""
    lines = (API_MD / "api.md").read_text(encoding="utf-8").splitlines()
    found: dict[Route, int] = {}
    for n, line in enumerate(lines):
        if heading := HEADING.match(line):
            section = [line, *takewhile(lambda text: not text.startswith("#"), lines[n + 1 :])]
            codes = SUCCESS.findall(" ".join(section))
            found[(heading.group(1), heading.group(2))] = int(codes[0]) if codes else 200
        elif row := ROW.match(line):
            codes = SUCCESS.findall(line)
            found[(row.group(1), row.group(2))] = int(codes[0]) if codes else 200
    return found


@pytest.fixture
def app(make_settings: Callable[..., Settings], tmp_path: Path) -> FastAPI:
    return create_app(make_settings(data_dir=tmp_path))


def operations(app: FastAPI) -> dict[Route, dict[str, Any]]:
    spec = app.openapi()
    return {
        (method.upper(), path): operation
        for path, methods in spec["paths"].items()
        for method, operation in methods.items()
    }


def test_the_contract_is_read() -> None:
    found = documented()
    assert len(found) >= 30
    assert found[("POST", "/api/v1/documents")] == 201
    assert found[("POST", "/api/v1/analyses/{run_id}/reviews")] == 201
    assert found[("POST", "/api/v1/analyses/{run_id}/resume")] == 202
    assert found[("GET", "/api/v1/analyses/{run_id}/export.json")] == 200


def test_routes_methods_and_success_codes_match_the_contract(app: FastAPI) -> None:
    contract = documented()
    built = {
        route: min(int(code) for code in operation["responses"] if code.startswith("2"))
        for route, operation in operations(app).items()
    }
    assert set(contract) - set(PENDING) == set(built), "routes differ from api.md"
    wrong = {route: (code, contract[route]) for route, code in built.items()
             if code != contract[route]}  # fmt: skip
    assert not wrong, f"success code (OpenAPI, api.md): {wrong}"


def test_errors_are_in_the_common_format(app: FastAPI) -> None:
    for route, operation in operations(app).items():
        responses = operation["responses"]
        assert "422" not in responses, route  # the service answers 400 validation_error
        if route[1].startswith("/api/v1"):
            schema = responses["4XX"]["content"]["application/json"]["schema"]
            assert schema["$ref"].endswith("/ErrorResponse"), route


def test_every_api_route_needs_authentication(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    users = f"expert:{hash_password('correct horse')}"
    secured = create_app(make_settings(data_dir=tmp_path, auth_enabled=True, auth_users=users))
    routes = [route for route in operations(secured) if route[1].startswith("/api/v1")]
    assert len(routes) == len(documented()) - len(PENDING) - 1  # all but /healthz
    with TestClient(secured) as client:
        for method, path in routes:
            url = re.sub(r"\{[^}]+\}", "x", path)  # any values: refused before they matter
            response = client.request(method, url)
            assert response.status_code == 401, (method, path, response.status_code)
            assert response.json()["error"]["code"] == "unauthorized"
