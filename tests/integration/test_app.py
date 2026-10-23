"""Application skeleton: lifespan, health, auth on screens, static files (T011)."""

import base64
import hashlib
import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gr.app import create_app
from gr.auth import hash_password
from gr.settings import Settings

PASSWORD = "expert password"
VENDOR = Path(__file__).resolve().parents[2] / "src" / "gr" / "web" / "static" / "vendor"


@pytest.fixture
def client(tmp_path: Path, make_settings: Callable[..., Settings]) -> Iterator[TestClient]:
    settings = make_settings(
        auth_enabled=True,
        auth_users=f"expert:{hash_password(PASSWORD)}",
        data_dir=tmp_path / "data",
        ocr_enabled=False,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def auth() -> dict[str, str]:
    token = base64.b64encode(f"expert:{PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_healthz_without_auth(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/", "/api/v1/readyz"])
def test_everything_else_needs_auth(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


def test_documents_page(client: TestClient) -> None:
    response = client.get("/", headers=auth())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Документы" in response.text
    assert "/static/vendor/htmx.min.js" in response.text
    assert "LLM: не настроена" in response.text


def test_readyz_reports_components(client: TestClient) -> None:
    body = client.get("/api/v1/readyz", headers=auth()).json()
    assert body["db"] == "ok"
    assert body["llm"] == {
        "policy": "disabled",
        "configured": False,
        "error": "llm_not_configured",
    }
    assert body["ocr"]["available"] is False and "OCR_ENABLED" in body["ocr"]["reason"]
    assert body["version"]


def test_static_and_vendor_files(client: TestClient) -> None:
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/vendor/htmx.min.js").status_code == 200


def test_vendor_checksums_match_versions_file() -> None:
    entries = re.findall(
        r"^(\S+\.js)\s+\S+\s+\S+\s+sha256=([0-9a-f]{64})$", (VENDOR / "VERSIONS").read_text(), re.M
    )
    assert {name for name, _ in entries} == {"htmx.min.js", "mermaid.min.js"}
    for name, digest in entries:
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == digest, name


def test_cross_site_post_rejected(client: TestClient) -> None:
    response = client.post("/", headers={**auth(), "Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_rejected"


def test_unknown_route_is_not_found(client: TestClient) -> None:
    response = client.get("/api/v1/nothing", headers=auth())
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_database_created_in_data_dir(tmp_path: Path, client: TestClient) -> None:
    assert (tmp_path / "data" / "gr.db").exists()
