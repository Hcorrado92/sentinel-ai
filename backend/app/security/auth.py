"""Admin bearer-token authentication and per-application ingest keys."""

import hashlib
import hmac
import secrets

from fastapi import HTTPException, Request, status

from app.config import Settings

INGEST_KEY_PREFIX = "sk_sentinel_"


def require_admin(request: Request) -> None:
    """FastAPI dependency: require ``Authorization: Bearer <admin token>``.

    503 when no admin token is configured on the server, 401 when the header
    is missing or the token does not match.
    """
    settings: Settings = request.app.state.settings
    expected = settings.admin_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin API is disabled: no admin token is configured",
        )

    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or not hmac.compare_digest(
        token.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def generate_ingest_key() -> str:
    return INGEST_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_ingest_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def verify_ingest_key(key: str | None, stored_hash: str | None) -> bool:
    """Constant-time comparison of a presented key against a stored hash."""
    if not key or not stored_hash:
        return False
    return hmac.compare_digest(hash_ingest_key(key), stored_hash)
