"""Same-origin guard for state-changing requests (R-15, T010)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gr.auth import SameOriginGuard, is_cross_site
from gr.errors import install_error_handlers

SERVICE = "https://gr.example.org"


def _client(public_url: str | None) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(SameOriginGuard, public_url=public_url)

    @app.post("/change")
    async def change() -> str:
        return "ok"

    @app.get("/read")
    async def read() -> str:
        return "ok"

    return TestClient(app, base_url="http://testserver")


@pytest.mark.parametrize(
    ("headers", "rejected"),
    [
        ({"sec-fetch-site": "cross-site"}, True),
        ({"sec-fetch-site": "same-site"}, True),
        ({"sec-fetch-site": "same-origin", "origin": "https://evil.example"}, False),
        ({"sec-fetch-site": "none"}, False),
        ({"origin": "https://evil.example"}, True),
        ({"origin": "null"}, True),
        ({"origin": "http://gr.example.org"}, True),  # scheme differs
        ({"origin": "https://gr.example.org:8443"}, True),  # port differs
        ({"origin": "https://GR.example.org"}, False),
        ({"origin": "https://gr.example.org:443"}, False),
        ({"referer": "https://evil.example/page"}, True),
        ({"referer": "https://gr.example.org/documents"}, False),
        ({}, False),  # curl and scripts
    ],
)
def test_is_cross_site(headers: dict[str, str], rejected: bool) -> None:
    assert is_cross_site("POST", headers, SERVICE) is rejected


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_safe_methods_pass(method: str) -> None:
    assert not is_cross_site(method, {"sec-fetch-site": "cross-site"}, SERVICE)


def test_middleware_rejects_foreign_origin_behind_proxy() -> None:
    client = _client(SERVICE)
    response = client.post("/change", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_rejected"
    assert client.post("/change", headers={"Origin": SERVICE}).status_code == 200
    assert client.post("/change", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/change").status_code == 200
    assert client.get("/read", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_middleware_uses_request_origin_locally() -> None:
    client = _client(None)
    assert client.post("/change", headers={"Origin": "http://testserver"}).status_code == 200
    assert client.post("/change", headers={"Origin": "http://evil.local"}).status_code == 403
