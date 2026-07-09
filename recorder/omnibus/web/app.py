from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from omnibus.auth.teams_session import (
    AuthState,
    clear_profile,
    profile_status,
    registry as auth_registry,
)
from omnibus.bot.broadcaster import broadcaster
from omnibus.bot.session import ACTIVE_STATES, State, registry
from omnibus.calendar.watcher import watcher
from omnibus.config import settings
from omnibus.resources import pool
from omnibus.security import User, current_user
from omnibus.services import recordings as rec_svc
from omnibus.services import summarizer as summarizer_svc
from omnibus.storage import service as storage


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.local_rec_dir.mkdir(parents=True, exist_ok=True)
    settings.auth_profiles_dir.mkdir(parents=True, exist_ok=True)
    try:
        await rec_svc.backfill_from_share()
    except Exception:
        pass
    if settings.calendar_enabled:
        watcher.start()
    yield
    await watcher.stop()


app = FastAPI(title=f"{settings.app_name} Recorder API", lifespan=lifespan)


# ----- payloads --------------------------------------------------------------

class SessionCreate(BaseModel):
    join_url: str = Field(..., min_length=10)
    use_identity: bool = True  # accepted for compat; every join is signed-in


class AssignPayload(BaseModel):
    project_dir: str = Field(..., min_length=3)


# ----- sessions --------------------------------------------------------------

@app.post("/api/sessions")
async def create_session(payload: SessionCreate, user: User = Depends(current_user)) -> JSONResponse:
    join_url = payload.join_url.strip()
    if not join_url.startswith(("https://teams.microsoft.com/", "https://teams.live.com/")):
        raise HTTPException(400, "URL must be a Teams meeting link.")
    try:
        session = registry.create(join_url, owner=user)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    session.start()
    return JSONResponse({"session_id": session.id, "status": session.status.to_dict()})


@app.get("/api/sessions")
async def list_sessions(user: User = Depends(current_user)) -> JSONResponse:
    return JSONResponse(
        {
            "active": [s.status.to_dict() for s in registry.active()],
            "free_slots": pool.free_count,
            "total_slots": settings.max_concurrent_sessions,
        }
    )


@app.post("/api/sessions/{session_id}/resume")
async def resume_session(session_id: str, user: User = Depends(current_user)) -> JSONResponse:
    session = registry.get(session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    if session.status.state != State.NEEDS_SUPERVISION:
        raise HTTPException(409, "Session is not awaiting supervision")
    session.resume_after_supervision()
    return JSONResponse({"ok": True})


@app.post("/api/sessions/{session_id}/stop")
async def stop_session(session_id: str, user: User = Depends(current_user)) -> JSONResponse:
    session = registry.get(session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    if session.owner.id != user.id:
        raise HTTPException(403, "Only the session owner can stop it")
    await session.request_stop()
    return JSONResponse({"ok": True})


# ----- recordings ------------------------------------------------------------

@app.get("/api/recordings")
async def list_recordings(
    status: Optional[str] = None, user: User = Depends(current_user)
) -> JSONResponse:
    return JSONResponse(await rec_svc.list_all(status=status))


@app.get("/api/recordings/{recording_id}")
async def get_recording(recording_id: str, user: User = Depends(current_user)) -> JSONResponse:
    rec = await rec_svc.get(recording_id)
    if rec is None:
        raise HTTPException(404, "Recording not found")
    rec_dir = rec_svc.resolve_dir(rec)
    rec["transcript"] = rec_svc.read_transcript(rec_dir) if rec_dir else []
    rec["timeline"] = rec_svc.read_timeline(rec_dir) if rec_dir else []
    rec["has_video"] = bool(rec_dir and (rec_dir / "recording.mp4").exists())
    return JSONResponse(rec)


@app.post("/api/recordings/{recording_id}/assign")
async def assign_recording(
    recording_id: str, payload: AssignPayload, user: User = Depends(current_user)
) -> JSONResponse:
    try:
        rec = await rec_svc.assign(recording_id, payload.project_dir, user.email)
    except KeyError:
        raise HTTPException(404, "Recording not found")
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))
    return JSONResponse(rec)


@app.post("/api/recordings/{recording_id}/unassign")
async def unassign_recording(
    recording_id: str, user: User = Depends(current_user)
) -> JSONResponse:
    try:
        rec = await rec_svc.unassign(recording_id)
    except KeyError:
        raise HTTPException(404, "Recording not found")
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))
    return JSONResponse(rec)


@app.delete("/api/recordings/{recording_id}")
async def delete_recording(
    recording_id: str, delete_files: bool = False, user: User = Depends(current_user)
) -> JSONResponse:
    rec = await rec_svc.get(recording_id)
    if rec is None:
        raise HTTPException(404, "Recording not found")
    if rec.get("owner_id") != user.id:
        raise HTTPException(403, "Only the owner can delete a recording")
    await rec_svc.delete(recording_id, delete_files=delete_files)
    return JSONResponse({"ok": True})


@app.post("/api/recordings/{recording_id}/summarize")
async def summarize_recording(
    recording_id: str, user: User = Depends(current_user)
) -> JSONResponse:
    try:
        rec = await summarizer_svc.summarize_recording(recording_id)
    except summarizer_svc.SummarizationError as e:
        raise HTTPException(502, f"Summarization failed: {e}")
    if rec is None:
        raise HTTPException(404, "Recording not found")
    return JSONResponse(rec)


# Video streaming with Range support (video players need seeking).
@app.get("/api/recordings/{recording_id}/video")
async def serve_video(
    recording_id: str, request: Request, user: User = Depends(current_user)
):
    rec = await rec_svc.get(recording_id)
    if rec is None:
        raise HTTPException(404, "Recording not found")
    rec_dir = rec_svc.resolve_dir(rec)
    if rec_dir is None:
        raise HTTPException(404, "Recording folder missing")
    path = rec_dir / "recording.mp4"
    if not path.exists():
        raise HTTPException(404, "Video file missing")
    return _range_file_response(path, request)


def _range_file_response(path: Path, request: Request):
    file_size = path.stat().st_size
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(str(path), media_type="video/mp4")
    try:
        unit, rng = range_header.split("=", 1)
        start_s, _, end_s = rng.partition("-")
        start = int(start_s)
        end = int(end_s) if end_s else min(start + 4 * 1024 * 1024 - 1, file_size - 1)
        end = min(end, file_size - 1)
    except ValueError:
        raise HTTPException(416, "Bad Range header")

    def iter_chunk():
        with path.open("rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(1024 * 256, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
    }
    return StreamingResponse(iter_chunk(), status_code=206, media_type="video/mp4", headers=headers)


# ----- projects (share folders) ----------------------------------------------

@app.get("/api/projects")
async def list_projects(user: User = Depends(current_user)) -> JSONResponse:
    dirs = await asyncio.to_thread(storage.list_project_dirs)
    return JSONResponse(dirs)


# ----- Teams identity (per-user sign-in) --------------------------------------

@app.get("/api/auth/teams/status")
async def auth_status(user: User = Depends(current_user)) -> JSONResponse:
    current = auth_registry.current_for(user.id)
    return JSONResponse(
        {
            "saved_state": profile_status(user.id),
            "in_progress": current.status.to_dict() if current else None,
        }
    )


@app.post("/api/auth/teams/begin")
async def auth_begin(user: User = Depends(current_user)) -> JSONResponse:
    try:
        session = auth_registry.begin(user)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    session.start()
    return JSONResponse({"auth_id": session.id, "status": session.status.to_dict()})


@app.post("/api/auth/teams/finish")
async def auth_finish(user: User = Depends(current_user)) -> JSONResponse:
    current = auth_registry.current_for(user.id)
    if current is None:
        raise HTTPException(404, "No sign-in in progress")
    if current.status.state != AuthState.AWAITING_USER:
        raise HTTPException(409, f"Sign-in is in state {current.status.state.value}")
    current.request_finish()
    await current.wait_done(timeout=20)
    return JSONResponse({"status": current.status.to_dict()})


@app.post("/api/auth/teams/cancel")
async def auth_cancel(user: User = Depends(current_user)) -> JSONResponse:
    current = auth_registry.current_for(user.id)
    if current is None:
        raise HTTPException(404, "No sign-in in progress")
    current.request_cancel()
    await current.wait_done(timeout=10)
    return JSONResponse({"status": current.status.to_dict()})


@app.delete("/api/auth/teams")
async def auth_clear(user: User = Depends(current_user)) -> JSONResponse:
    if auth_registry.current_for(user.id) is not None:
        raise HTTPException(409, "Sign-in in progress — finish or cancel first")
    for s in registry.active():
        if s.owner.id == user.id:
            raise HTTPException(409, "You have an active session — stop it first")
    try:
        clear_profile(user.id)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return JSONResponse({"saved_state": profile_status(user.id)})


# ----- events / health --------------------------------------------------------

@app.get("/api/events/stream")
async def events_stream(user: User = Depends(current_user)) -> StreamingResponse:
    async def gen():
        yield ": connected\n\n"
        async for event in broadcaster.subscribe():
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/healthz")
async def healthz() -> dict:
    return {
        "ok": True,
        "app": settings.app_name,
        "share_mounted": storage.share_available(),
        "free_slots": pool.free_count,
    }
