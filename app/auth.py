import hmac

from fastapi import Depends, Header, HTTPException, Request, status

from app.config import Settings, get_settings


def _matches(value: str | None, expected: str) -> bool:
    return bool(value) and hmac.compare_digest(value, expected)


def require_tester(
    request: Request,
    x_access_code: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    supplied = x_access_code or request.cookies.get("tester_access")
    if not _matches(supplied, settings.tester_access_code.get_secret_value()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Tester access required"
        )


def require_admin(
    request: Request,
    x_admin_code: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    supplied = x_admin_code or request.cookies.get("admin_access")
    if not _matches(supplied, settings.admin_access_code.get_secret_value()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin access required"
        )
