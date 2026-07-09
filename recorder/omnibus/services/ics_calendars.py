"""ICS calendar subscriptions stored in Supabase (zw_omnibus.ics_calendar)."""
from __future__ import annotations

from typing import Optional

from omnibus import sb
from omnibus.storage.service import now_iso

TABLE = "ics_calendar"


async def list_subscriptions() -> list[dict]:
    return await sb.select(TABLE, {"order": "id.asc"})


async def create_subscription(name: str, url: str, enabled: bool, owner_email: str) -> dict:
    rows = await sb.upsert(
        TABLE,
        {"name": name, "url": url, "enabled": enabled, "created_by": owner_email},
        on_conflict="url",
    )
    return rows[0]


async def update_subscription(sub_id: int, patch: dict) -> Optional[dict]:
    rows = await sb.update(TABLE, {"id": str(sub_id)}, patch)
    return rows[0] if rows else None


async def delete_subscription(sub_id: int) -> bool:
    await sb.delete(TABLE, {"id": str(sub_id)})
    return True


async def record_poll(sub_id: int, *, error: Optional[str]) -> None:
    await sb.update(
        TABLE,
        {"id": str(sub_id)},
        {"last_polled_at": now_iso(), "last_error": error},
    )
