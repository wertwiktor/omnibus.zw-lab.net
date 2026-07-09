"""Request authentication: verify Supabase (GoTrue) JWTs issued by korfu.

The SPA logs the user in via Entra ID SSO -> Supabase session. Every call to
this backend carries `Authorization: Bearer <access_token>`; we verify it
against the shared JWT secret (HS256) and expose the user identity to
handlers. No second login, no separate accounts.
"""
from __future__ import annotations

from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request

from omnibus.config import settings


@dataclass(frozen=True)
class User:
    id: str
    email: str

    @property
    def display(self) -> str:
        return self.email or self.id


def _decode(token: str) -> dict:
    return jwt.decode(
        token,
        settings.supabase_jwt_secret,
        algorithms=["HS256"],
        audience="authenticated",
        options={"require": ["exp", "sub"]},
    )


async def current_user(request: Request) -> User:
    auth = request.headers.get("authorization", "")
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    elif request.query_params.get("token"):
        # <video>/<img> elements can't send headers — allow ?token= there.
        token = request.query_params["token"]
    if not token:
        raise HTTPException(401, "Missing bearer token")
    try:
        claims = _decode(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(401, f"Invalid token: {e}")
    email = claims.get("email") or (claims.get("user_metadata") or {}).get("email", "")
    return User(id=claims["sub"], email=email)


CurrentUser = Depends(current_user)
