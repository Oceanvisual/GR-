"""HTTP Basic with scrypt hashes and the same-origin guard against CSRF (R-15)."""

import asyncio
import hashlib
import hmac
import os
import secrets
from collections import OrderedDict
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from gr.errors import AppError, error_response
from gr.settings import Settings

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _DKLEN = 2**14, 8, 1, 32
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN
    )


def hash_password(password: str) -> str:
    """`scrypt$<salt hex>$<hash hex>`: the value after `login:` in AUTH_USERS."""
    salt = os.urandom(16)
    return f"scrypt${salt.hex()}${_scrypt(password, salt).hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, salt_hex, hash_hex = encoded.split("$")
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(hash_hex)
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    return hmac.compare_digest(_scrypt(password, salt), expected)


def _well_formed(encoded: str) -> bool:
    parts = encoded.split("$")
    if len(parts) != 3 or parts[0] != "scrypt":
        return False
    try:
        salt, digest = bytes.fromhex(parts[1]), bytes.fromhex(parts[2])
    except ValueError:
        return False
    return len(salt) >= 16 and len(digest) >= 32


def parse_users(raw: str) -> dict[str, str]:
    """`login:scrypt$…,login2:scrypt$…` → {login: encoded hash}."""
    users: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        login, sep, encoded = entry.partition(":")
        if not sep or not login or not _well_formed(encoded):
            # Fail at startup, not at every login: a deploy with a broken hash rolls back.
            raise ValueError(
                f"AUTH_USERS: {login or '?'}: expected login:scrypt$<salt>$<hash> from "
                "`gr hash-password`. In a docker compose env file put the value in single "
                "quotes: compose takes $… for a variable and drops it."
            )
        users[login] = encoded
    return users


_DUMMY_HASH = hash_password(secrets.token_hex(8))


class BasicAuthenticator:
    """FastAPI dependency. scrypt runs in a thread; successful logins are remembered so HTMX
    polling every 2 s does not pay ~50 ms of CPU per request."""

    _CACHE_SIZE = 64

    def __init__(self, settings: Settings) -> None:
        self.enabled = settings.auth_enabled
        raw = settings.auth_users.get_secret_value() if settings.auth_users else ""
        self.users = parse_users(raw)
        self._scheme = HTTPBasic(auto_error=False)
        self._cache_key = os.urandom(32)
        self._verified: OrderedDict[bytes, str] = OrderedDict()

    def _fingerprint(self, login: str, password: str) -> bytes:
        message = login.encode() + b"\x00" + password.encode()
        return hmac.new(self._cache_key, message, hashlib.sha256).digest()

    async def check(self, login: str, password: str) -> bool:
        fingerprint = self._fingerprint(login, password)
        if fingerprint in self._verified:
            self._verified.move_to_end(fingerprint)
            return True
        # Unknown logins still pay for a hash, so timing does not reveal which logins exist.
        encoded = self.users.get(login, _DUMMY_HASH)
        ok = await asyncio.to_thread(verify_password, password, encoded) and login in self.users
        if ok:
            self._verified[fingerprint] = login
            while len(self._verified) > self._CACHE_SIZE:
                self._verified.popitem(last=False)
        return ok

    async def __call__(self, request: Request) -> str:
        if not self.enabled:
            return "local"
        credentials: HTTPBasicCredentials | None = await self._scheme(request)
        if credentials is None or not await self.check(credentials.username, credentials.password):
            raise AppError("unauthorized", headers={"WWW-Authenticate": 'Basic realm="gr"'})
        return credentials.username


def _origin(url: str) -> tuple[str, str, int | None] | None:
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        return None
    default_port = {"http": 80, "https": 443}.get(parts.scheme)
    return parts.scheme, parts.hostname.lower(), parts.port or default_port


def is_cross_site(method: str, headers: dict[str, str], service_origin: str) -> bool:
    """True if a state-changing browser request comes from another origin (R-15)."""
    if method.upper() not in _UNSAFE_METHODS:
        return False
    fetch_site = headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site.lower() not in _ALLOWED_FETCH_SITES
    source = headers.get("origin") or headers.get("referer")
    if source is None:
        return False  # curl and scripts: no ambient browser credentials to abuse
    if source == "null":
        return True
    return _origin(source) != _origin(service_origin)


class SameOriginGuard:
    """ASGI middleware: rejects cross-site POST/PUT/PATCH/DELETE with 403 csrf_rejected.

    The service origin is APP_PUBLIC_URL when set (behind a reverse proxy), otherwise the
    scheme and Host of the request itself.
    """

    def __init__(self, app: ASGIApp, public_url: str | None = None) -> None:
        self.app = app
        self.public_url = public_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            request = Request(scope)
            service_origin = self.public_url or str(request.base_url)
            headers = {k.lower(): v for k, v in request.headers.items()}
            if is_cross_site(request.method, headers, service_origin):
                response: Response = error_response(AppError("csrf_rejected"))
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
