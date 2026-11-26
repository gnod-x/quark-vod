"""Shared Bearer dependency for management routers."""
import hmac

from fastapi import HTTPException, Request

from . import config


def require_admin(request: Request):
    token = config.get_admin_token()
    if not token:
        raise HTTPException(503, detail="admin_not_configured")
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(value.encode(), token.encode()):
        raise HTTPException(401, detail="unauthorized", headers={"WWW-Authenticate": "Bearer"})
