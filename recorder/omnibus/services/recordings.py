"""Recording rows in Supabase (zw_omnibus.recording) + assignment workflow."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Optional

import structlog

from omnibus import sb
from omnibus.config import settings
from omnibus.storage import service as storage

log = structlog.get_logger(__name__)

TABLE = "recording"


async def upsert_row(row: dict[str, Any]) -> dict:
    rows = await sb.upsert(TABLE, row, on_conflict="id")
    return rows[0]


async def get(recording_id: str) -> Optional[dict]:
    rows = await sb.select(TABLE, {"id": f"eq.{recording_id}"})
    return rows[0] if rows else None


async def list_all(status: Optional[str] = None) -> list[dict]:
    params = {"order": "started_at.desc.nullslast"}
    if status:
        params["status"] = f"eq.{status}"
    return await sb.select(TABLE, params)


async def assign(recording_id: str, project_dir: str, user_email: str) -> dict:
    rec = await get(recording_id)
    if rec is None:
        raise KeyError(recording_id)
    if rec["status"] != "inbox":
        raise ValueError(f"Recording is '{rec['status']}', only inbox items can be assigned")
    new_rel = await asyncio.to_thread(
        storage.assign_to_project, rec["share_path"], project_dir
    )
    rows = await sb.update(
        TABLE,
        {"id": recording_id},
        {
            "status": "assigned",
            "share_path": new_rel,
            "project_dir": project_dir,
            "assigned_by": user_email,
            "assigned_at": storage.now_iso(),
        },
    )
    return rows[0]


async def unassign(recording_id: str) -> dict:
    rec = await get(recording_id)
    if rec is None:
        raise KeyError(recording_id)
    if rec["status"] != "assigned":
        raise ValueError("Recording is not assigned")
    new_rel = await asyncio.to_thread(storage.unassign_to_temp, rec["share_path"])
    rows = await sb.update(
        TABLE,
        {"id": recording_id},
        {
            "status": "inbox",
            "share_path": new_rel,
            "project_dir": None,
            "assigned_by": None,
            "assigned_at": None,
        },
    )
    return rows[0]


async def delete(recording_id: str, *, delete_files: bool = False) -> bool:
    rec = await get(recording_id)
    if rec is None:
        return False
    if delete_files and rec.get("share_path"):
        try:
            folder = storage.abs_from_rel(rec["share_path"])
            if folder.is_dir():
                import shutil

                await asyncio.to_thread(shutil.rmtree, folder, ignore_errors=True)
        except ValueError:
            pass
    await sb.delete(TABLE, {"id": recording_id})
    return True


async def set_auto_summary(recording_id: str, text: str) -> Optional[dict]:
    rows = await sb.update(
        TABLE,
        {"id": recording_id},
        {"auto_summary": text, "auto_summary_at": storage.now_iso()},
    )
    return rows[0] if rows else None


def resolve_dir(rec: dict) -> Optional[Path]:
    """Absolute folder for a recording row, wherever it currently lives."""
    if rec.get("share_path"):
        try:
            p = storage.abs_from_rel(rec["share_path"])
            if p.is_dir():
                return p
        except ValueError:
            return None
    if rec.get("local_path"):
        p = Path(rec["local_path"])
        if p.is_dir():
            return p
    return None


def read_transcript(rec_dir: Path) -> list[dict]:
    events = _read_jsonl(rec_dir / "events.jsonl")
    return [
        {"ts": e.get("ts"), "author": e.get("author") or "?", "body": e.get("body") or ""}
        for e in events
        if e.get("kind") == "chat.message"
    ]


_TIMELINE_KINDS = {
    "session.start", "session.end", "session.solo_started", "session.solo_ended",
    "session.solo_timeout", "session.supervision_needed", "session.supervision_resumed",
    "session.error", "teams.navigate", "teams.prejoin.join_clicked",
    "teams.prejoin.name_filled", "teams.admitted", "teams.lobby", "teams.left",
    "teams.rejoin_clicked", "participant.joined", "participant.left",
}


def read_timeline(rec_dir: Path) -> list[dict]:
    events = _read_jsonl(rec_dir / "events.jsonl")
    return [
        {
            "ts": e.get("ts"),
            "kind": e.get("kind"),
            "detail": {k: v for k, v in e.items() if k not in ("ts", "mono", "kind", "session_id")},
        }
        for e in events
        if e.get("kind") in _TIMELINE_KINDS
    ]


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


async def backfill_from_share() -> int:
    """Reconcile DB with TEMP folders on the share (e.g. after manual copies).

    Creates inbox rows for folders that have metadata.json but no DB row.
    """
    n = 0
    for folder in await asyncio.to_thread(storage.scan_temp_folders):
        meta = storage.read_metadata(folder) or {}
        rec_id = meta.get("recording_id") or folder.name
        existing = await get(rec_id)
        if existing:
            continue
        await upsert_row(
            {
                "id": rec_id,
                "title": meta.get("meeting_title"),
                "join_url": meta.get("join_url"),
                "owner_id": meta.get("owner_id"),
                "owner_email": meta.get("owner_email"),
                "status": "inbox",
                "share_path": storage.rel_to_share(folder),
                "started_at": meta.get("started_at"),
                "ended_at": meta.get("ended_at"),
                "duration_seconds": meta.get("duration_seconds"),
                "participants": meta.get("participants_seen", []),
                "state": meta.get("state"),
            }
        )
        n += 1
    if n:
        log.info("recordings.backfilled", count=n)
    return n
