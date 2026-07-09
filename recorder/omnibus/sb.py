"""Thin async client for the korfu Supabase PostgREST API (service_role).

The recorder is the only writer to zw_omnibus.recording — the SPA reads via
its own anon/user JWT under RLS. Using REST instead of a direct Postgres
connection keeps the deployment dependency-light and goes through the same
Kong gateway as everything else.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx
import structlog

from omnibus.config import settings

log = structlog.get_logger(__name__)


def _headers(*, write: bool = False) -> dict[str, str]:
    h = {
        "apikey": settings.supabase_service_key,
        "Authorization": f"Bearer {settings.supabase_service_key}",
        "Accept-Profile": settings.supabase_schema,
    }
    if write:
        h["Content-Profile"] = settings.supabase_schema
        h["Prefer"] = "return=representation,resolution=merge-duplicates"
    return h


def _url(table: str) -> str:
    return f"{settings.supabase_url}/rest/v1/{table}"


async def select(table: str, params: Optional[dict[str, str]] = None) -> list[dict]:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(_url(table), headers=_headers(), params=params or {})
        r.raise_for_status()
        return r.json()


async def upsert(table: str, rows: list[dict] | dict, on_conflict: str = "id") -> list[dict]:
    payload = rows if isinstance(rows, list) else [rows]
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            _url(table),
            headers=_headers(write=True),
            params={"on_conflict": on_conflict},
            json=payload,
        )
        r.raise_for_status()
        return r.json()


async def update(table: str, match: dict[str, str], patch: dict[str, Any]) -> list[dict]:
    params = {k: f"eq.{v}" for k, v in match.items()}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.patch(
            _url(table), headers=_headers(write=True), params=params, json=patch
        )
        r.raise_for_status()
        return r.json()


async def delete(table: str, match: dict[str, str]) -> list[dict]:
    params = {k: f"eq.{v}" for k, v in match.items()}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.delete(_url(table), headers=_headers(write=True), params=params)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return []
