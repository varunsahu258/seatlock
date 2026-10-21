import hmac

from fastapi import Header, HTTPException

from app.core.config import settings


async def require_admin_token(authorization: str | None = Header(None)) -> None:
    """Require a bearer token for admin routes when one is configured."""
    expected_token = settings.admin_token
    if expected_token is None:
        return

    scheme, separator, supplied_token = (authorization or "").partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not hmac.compare_digest(supplied_token, expected_token)
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing admin token",
            headers={"WWW-Authenticate": "Bearer"},
        )
