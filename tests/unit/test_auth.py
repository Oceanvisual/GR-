"""HTTP Basic with scrypt (R-15, T010)."""

import base64
import io
from collections.abc import Callable

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from gr.__main__ import main
from gr.auth import BasicAuthenticator, hash_password, parse_users, verify_password
from gr.errors import install_error_handlers
from gr.settings import Settings

PASSWORD = "correct horse"


def _client(settings: Settings) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    auth = BasicAuthenticator(settings)

    @app.get("/me")
    async def me(user: str = Depends(auth)) -> str:
        return user

    return TestClient(app)


@pytest.fixture
def secured(make_settings: Callable[..., Settings]) -> TestClient:
    users = f"expert:{hash_password(PASSWORD)},viewer:{hash_password('other pass')}"
    return _client(make_settings(auth_enabled=True, auth_users=users))


def _basic(login: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{login}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_hash_round_trip() -> None:
    encoded = hash_password(PASSWORD)
    assert encoded.startswith("scrypt$")
    assert verify_password(PASSWORD, encoded)
    assert not verify_password("wrong", encoded)
    assert hash_password(PASSWORD) != encoded  # salted
    for broken in ("", "scrypt$zz$00", "bcrypt$00$00", encoded + "$x"):
        assert not verify_password(PASSWORD, broken)


def test_parse_users() -> None:
    a, b = hash_password("first password"), hash_password("second password")
    assert parse_users(f" a:{a} , b:{b},") == {"a": a, "b": b}
    salt = a.split("$")[1]
    # A docker compose env file without quotes eats `$<salt>` as a variable (T056): what is
    # left must stop the start, not every login after it.
    broken = a.replace(f"${salt}", "")
    for bad in ("a", "a:plain", f":{a}", "a:scrypt$1$2", f"a:{broken}", f"a:{a}x"):
        with pytest.raises(ValueError, match="AUTH_USERS"):
            parse_users(bad)


def test_valid_credentials(secured: TestClient) -> None:
    response = secured.get("/me", headers=_basic("expert", PASSWORD))
    assert response.status_code == 200 and response.json() == "expert"
    again = secured.get("/me", headers=_basic("expert", PASSWORD))  # cached path
    assert again.json() == "expert"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        _basic("expert", "wrong password"),
        _basic("nobody", PASSWORD),
        _basic("viewer", PASSWORD),  # another user's password
        {"Authorization": "Basic !!!not-base64"},
        {"Authorization": "Bearer token"},
    ],
)
def test_rejected_credentials(secured: TestClient, headers: dict[str, str]) -> None:
    response = secured.get("/me", headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.headers["www-authenticate"].startswith("Basic")


def test_auth_disabled_locally(make_settings: Callable[..., Settings]) -> None:
    response = _client(make_settings()).get("/me")
    assert response.status_code == 200 and response.json() == "local"


def test_cli_hash_passes_verification(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert main(["hash-password", "--stdin", "--login", "expert"]) == 0
    printed = capsys.readouterr()
    line = printed.out.strip()
    assert "одинарных кавычках" in printed.err  # the hint stays out of the line
    users = parse_users(line)
    assert verify_password(PASSWORD, users["expert"])


def test_cli_rejects_short_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert main(["hash-password", "--stdin"]) == 1
