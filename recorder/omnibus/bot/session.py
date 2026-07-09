from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import structlog

from playwright.async_api import Error as PWError, TimeoutError as PWTimeout

from omnibus.bot.browser import teams_browser
from omnibus.bot.display import DisplayHandle, start_display
from omnibus.bot.events import EventLog
from omnibus.bot.recorder import RecorderHandle, start_recorder
from omnibus.bot.teams import MeetingEnded, SupervisedHandoff, TeamsSession
from omnibus.config import settings
from omnibus.resources import ResourceSlot, pool
from omnibus.security import User
from omnibus.storage import service as storage
from omnibus.supervise.vnc import VncHandle, start_vnc

log = structlog.get_logger(__name__)


class State(str, Enum):
    IDLE = "idle"
    PREPARING = "preparing"
    JOINING = "joining"
    IN_LOBBY = "in_lobby"
    IN_MEETING = "in_meeting"
    LEAVING = "leaving"
    NEEDS_SUPERVISION = "needs_supervision"
    FINALIZING = "finalizing"
    DONE = "done"
    FAILED = "failed"

ACTIVE_STATES = {
    State.PREPARING, State.JOINING, State.IN_LOBBY,
    State.IN_MEETING, State.LEAVING, State.NEEDS_SUPERVISION,
    State.FINALIZING,
}


@dataclass
class SessionStatus:
    session_id: str
    owner_id: str = ""
    owner_email: str = ""
    state: State = State.IDLE
    join_url: str = ""
    meeting_title: Optional[str] = None
    participants: list[str] = field(default_factory=list)
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    output_dir: Optional[str] = None
    supervision_reason: Optional[str] = None
    novnc_port: Optional[int] = None
    error: Optional[str] = None
    use_identity: bool = False
    recording_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["state"] = self.state.value
        return d


class MeetingSession:
    """End-to-end orchestration for one meeting recording (one slot).

    Multiuser changes vs the original single-user app:
      - owner (Entra user via Supabase JWT) stamped on everything
      - resources come from the slot pool (own display/sink/ports)
      - identity mode uses the OWNER's persistent Chromium profile
      - recording goes to local disk, then to the share TEMP inbox
      - the meeting row lives in Supabase, not local sqlite
    """

    def __init__(self, join_url: str, *, owner: User) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.owner = owner
        self.join_url = join_url
        self.use_identity = True  # guest joins removed — always join as owner
        self.status = SessionStatus(
            session_id=self.id,
            owner_id=owner.id,
            owner_email=owner.email,
            join_url=join_url,
            use_identity=True,
        )

        self._slot: Optional[ResourceSlot] = None
        self._display: Optional[DisplayHandle] = None
        self._recorder: Optional[RecorderHandle] = None
        self._vnc: Optional[VncHandle] = None
        self._teams: Optional[TeamsSession] = None
        self._event_log: Optional[EventLog] = None
        self._dir: Optional[Path] = None
        self._task: Optional[asyncio.Task] = None
        self._shutdown_task: Optional[asyncio.Task] = None
        self._resume = asyncio.Event()
        self._stop_requested = False

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name=f"meeting-{self.id}")

    async def request_stop(self) -> None:
        """Flag the session to stop and shut it down in the BACKGROUND.

        This must return fast: the HTTP stop handler awaits it, and browsers/
        proxies give up on slow POSTs — uvicorn then cancels the handler task
        mid-await, which surfaced as CancelledError 500s and users hammering
        the Stop button. The actual grace-then-cancel dance runs detached.
        """
        if self._stop_requested:
            # Already stopping — don't spawn another shutdown task or emit
            # duplicate events (repeat clicks raced the finalizer and tried
            # to write events.jsonl after the dir moved to TEMP).
            return
        self._stop_requested = True
        await self._safe_emit("session.stop_requested")
        if self._task is None:
            return
        self._resume.set()
        self._shutdown_task = asyncio.create_task(
            self._graceful_shutdown(), name=f"stop-{self.id}"
        )

    async def _graceful_shutdown(self) -> None:
        # Give the task a grace window to unwind on its own, then hard cancel.
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=30)
        except asyncio.TimeoutError:
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("session.shutdown_failed", id=self.id)

    def resume_after_supervision(self) -> None:
        self._resume.set()

    # --- main loop ---------------------------------------------------------

    async def _run(self) -> None:
        try:
            self._set_state(State.PREPARING)
            self._slot = pool.acquire()
            self._dir = storage.new_local_dir(hint=None)
            self.status.output_dir = str(self._dir)
            self.status.recording_id = self._dir.name
            self._event_log = EventLog(self._dir / "events.jsonl", session_id=self.id)
            await self._event_log.emit(
                "session.start",
                session_id=self.id,
                join_url=self.join_url,
                owner=self.owner.email,
                app=settings.app_name,
            )
            await self._upsert_db(status="recording")

            self._display = start_display(
                display_number=self._slot.display_number,
                width=settings.display_width,
                height=settings.display_height,
                depth=settings.display_depth,
                pulse_sink_name=self._slot.pulse_sink,
            )

            if settings.vnc_always_on:
                self._ensure_vnc()

            self._recorder = start_recorder(
                display=self._display.display,
                width=self._display.width,
                height=self._display.height,
                framerate=settings.video_framerate,
                pulse_monitor=self._display.pulse_monitor,
                output_path=self._dir / "recording.mp4",
                crf=settings.video_crf,
                preset=settings.video_preset,
                audio_bitrate=settings.audio_bitrate,
                env=self._display.env,
            )
            self.status.started_at = datetime.now(timezone.utc).isoformat()

            # Every join is an identity join: the owner's persistent profile
            # plus the cookie snapshot captured at sign-in. The snapshot is
            # injected at launch because Chromium purges session cookies
            # (ESTSAUTH) from its own store at startup.
            from omnibus.auth.teams_session import cookie_snapshot_for, profile_status

            if not profile_status(self.owner.id).get("signed_in"):
                raise RuntimeError(
                    "No saved Teams sign-in for your account — complete "
                    "the one-time sign-in (Teams Sign-in tab) first."
                )

            async with teams_browser(
                env=self._display.env,
                width=self._display.width,
                height=self._display.height,
                profile_dir=self._owner_profile_dir(),
                inject_cookies=cookie_snapshot_for(self.owner.id),
            ) as (_browser, context):
                self._teams = TeamsSession(
                    context=context,
                    display_name=f"{settings.display_name} ({self.owner.display})",
                    join_url=self.join_url,
                    on_event=self._on_teams_event,
                    debug_dir=self._dir,
                    display=self._display.display,
                    anonymous=False,
                    should_stop=lambda: self._stop_requested,
                    owner_email=self.owner.email,
                    cookie_snapshot=cookie_snapshot_for(self.owner.id),
                )

                self._set_state(State.JOINING)
                await self._teams.open()
                # fill_prejoin owns the lazy identity warmup now, so it's the
                # step that may demand an interactive sign-in (or hit a guest
                # prejoin that survived the warmup). On handoff, let the human
                # resolve it in the live view, then re-run open()+fill so the
                # bot re-navigates signed-in and re-detects the prejoin.
                try:
                    await self._teams.fill_prejoin()
                except SupervisedHandoff as h:
                    await self._enter_supervision(h)
                    await self._teams.open()
                    await self._teams.fill_prejoin()
                except (PWTimeout, PWError) as e:
                    await self._enter_supervision(
                        SupervisedHandoff(
                            "playwright-timeout",
                            f"A Teams UI action timed out ({type(e).__name__}). "
                            "Take over via the live view, complete what's "
                            "blocking the bot, then click 'I've handled it'.",
                        )
                    )

                try:
                    await self._teams.wait_until_admitted(
                        join_timeout=settings.join_timeout_seconds,
                        on_lobby=lambda: self._set_state_async(State.IN_LOBBY),
                    )
                except SupervisedHandoff as h:
                    await self._enter_supervision(h)
                    await self._teams.wait_until_admitted(
                        join_timeout=settings.join_timeout_seconds,
                    )

                self._set_state(State.IN_MEETING)
                title = await self._teams.meeting_title()
                if title:
                    self.status.meeting_title = title
                    await self._upsert_db(status="recording")
                await self._teams.open_roster()
                await asyncio.sleep(2)
                await self._teams.dump_roster_diagnostics(label="t0")

                dump_task = (
                    asyncio.create_task(self._dump_loop(), name=f"dump-{self.id}")
                    if settings.debug_dump_seconds > 0
                    else None
                )
                try:
                    await self._meeting_loop()
                finally:
                    if dump_task is not None:
                        dump_task.cancel()
                        try:
                            await dump_task
                        except (asyncio.CancelledError, Exception):
                            pass

                self._set_state(State.LEAVING)
                await self._teams.leave()

        except MeetingEnded as e:
            log.info("session.meeting_ended", reason=str(e))
            await self._safe_emit("session.meeting_ended", reason=str(e))
        except asyncio.CancelledError:
            await self._safe_emit("session.cancelled")
            raise
        except Exception as e:
            log.exception("session.failed")
            self.status.state = State.FAILED
            self.status.error = str(e)
            await self._safe_emit("session.error", error=str(e))
        finally:
            self.status.ended_at = datetime.now(timezone.utc).isoformat()
            await self._cleanup()
            if self.status.state not in (State.FAILED,):
                self._set_state(State.FINALIZING)
            await self._safe_emit("session.end", state=self.status.state.value)
            await self._finalize()
            if self.status.state not in (State.FAILED,):
                self._set_state(State.DONE)

    async def _meeting_loop(self) -> None:
        assert self._teams is not None
        solo_since: Optional[float] = None
        last_set: set[str] = set()
        loop = asyncio.get_event_loop()

        while not self._stop_requested:
            if await self._teams.detect_meeting_end():
                raise MeetingEnded("Teams reports meeting ended")

            snap = await self._teams.snapshot_participants()
            now_set = set(snap.names)
            self.status.participants = sorted(now_set)

            for name in now_set - last_set:
                await self._safe_emit("participant.joined", name=name, count=len(now_set))
            for name in last_set - now_set:
                await self._safe_emit("participant.left", name=name, count=len(now_set))
            last_set = now_set

            if snap.count is None:
                await asyncio.sleep(settings.participant_poll_seconds)
                continue
            real_others = max(0, snap.count - 1)
            if real_others == 0:
                if solo_since is None:
                    solo_since = loop.time()
                    await self._safe_emit("session.solo_started")
                elif loop.time() - solo_since >= settings.solo_grace_seconds:
                    await self._safe_emit(
                        "session.solo_timeout", grace_seconds=settings.solo_grace_seconds
                    )
                    return
            else:
                if solo_since is not None:
                    await self._safe_emit("session.solo_ended")
                solo_since = None

            await asyncio.sleep(settings.participant_poll_seconds)

    async def _dump_loop(self) -> None:
        interval = max(5, settings.debug_dump_seconds)
        try:
            while not self._stop_requested:
                await asyncio.sleep(interval)
                if self._teams is None or self._teams.page is None:
                    continue
                label = f"dump_{int(time.time())}"
                try:
                    await self._teams.dump_roster_diagnostics(label=label)
                except Exception:
                    log.exception("dump.failed")
        except asyncio.CancelledError:
            pass

    # --- supervision -------------------------------------------------------

    def _ensure_vnc(self) -> None:
        if self._vnc is not None or self._slot is None or self._display is None:
            return
        try:
            self._vnc = start_vnc(
                display=self._display.display,
                vnc_port=self._slot.vnc_port,
                novnc_port=self._slot.novnc_port,
                novnc_dir=settings.novnc_dir,
                env=self._display.env,
            )
            self.status.novnc_port = self._vnc.novnc_port
        except Exception as e:
            log.exception("vnc.start_failed")
            self.status.novnc_port = None
            asyncio.create_task(self._safe_emit("session.vnc_failed", error=str(e)))

    async def _enter_supervision(self, handoff: SupervisedHandoff) -> None:
        self.status.state = State.NEEDS_SUPERVISION
        self.status.supervision_reason = handoff.message
        await self._safe_emit(
            "session.supervision_needed", code=handoff.code, message=handoff.message
        )
        self._ensure_vnc()
        self._resume.clear()
        await self._resume.wait()
        await self._safe_emit("session.supervision_resumed")
        self.status.supervision_reason = None

    # --- helpers -----------------------------------------------------------

    def _owner_profile_dir(self) -> Path:
        return settings.auth_profiles_dir / self.owner.id

    def _set_state(self, state: State) -> None:
        self.status.state = state
        log.info("session.state", session=self.id, state=state.value)

    async def _set_state_async(self, state: State) -> None:
        self._set_state(state)

    async def _on_teams_event(self, kind: str, fields: dict) -> None:
        await self._safe_emit(kind, **fields)

    async def _safe_emit(self, kind: str, **fields) -> None:
        if self._event_log is None:
            return
        try:
            await self._event_log.emit(kind, **fields)
        except Exception:
            log.exception("event_log.emit_failed", kind=kind)

    async def _cleanup(self) -> None:
        if self._recorder is not None:
            try:
                await asyncio.to_thread(self._recorder.stop)
            except Exception:
                log.exception("recorder.stop_failed")
        if self._vnc is not None:
            try:
                self._vnc.stop()
            except Exception:
                log.exception("vnc.stop_failed")
        if self._display is not None:
            try:
                self._display.stop()
            except Exception:
                log.exception("display.stop_failed")
        if self._slot is not None:
            pool.release(self._slot)
            self._slot = None

    async def _upsert_db(self, *, status: str, extra: Optional[dict] = None) -> None:
        from omnibus.services import recordings

        duration = None
        if self.status.started_at and self.status.ended_at:
            try:
                a = datetime.fromisoformat(self.status.started_at)
                b = datetime.fromisoformat(self.status.ended_at)
                duration = max(0, int((b - a).total_seconds()))
            except ValueError:
                pass
        row = {
            "id": self.status.recording_id,
            "session_id": self.id,
            "owner_id": self.owner.id,
            "owner_email": self.owner.email,
            "title": self.status.meeting_title,
            "join_url": self.join_url,
            "status": status,
            "state": self.status.state.value,
            "started_at": self.status.started_at,
            "ended_at": self.status.ended_at,
            "duration_seconds": duration,
            "participants": sorted(set(self.status.participants)),
            "used_identity": self.use_identity,
            "error": self.status.error,
        }
        if extra:
            row.update(extra)
        try:
            await recordings.upsert_row({k: v for k, v in row.items() if v is not None or k in ("error",)})
        except Exception:
            log.exception("session.db_upsert_failed")

    async def _finalize(self) -> None:
        """Write metadata, move to the share TEMP inbox, update the DB."""
        if self._dir is None:
            return
        meta = {
            "recording_id": self.status.recording_id,
            "session_id": self.id,
            "meeting_title": self.status.meeting_title,
            "join_url": self.join_url,
            "owner_id": self.owner.id,
            "owner_email": self.owner.email,
            "state": self.status.state.value,
            "started_at": self.status.started_at,
            "ended_at": self.status.ended_at,
            "duration_seconds": None,
            "participants_seen": sorted(set(self.status.participants)),
            "used_identity": self.use_identity,
            "error": self.status.error,
            "app": settings.app_name,
        }
        if self.status.started_at and self.status.ended_at:
            try:
                a = datetime.fromisoformat(self.status.started_at)
                b = datetime.fromisoformat(self.status.ended_at)
                meta["duration_seconds"] = max(0, int((b - a).total_seconds()))
            except ValueError:
                pass
        try:
            storage.write_metadata(self._dir, meta)
        except OSError:
            log.exception("session.metadata_write_failed")

        share_rel: Optional[str] = None
        local_path: Optional[str] = str(self._dir)
        try:
            moved = await asyncio.to_thread(storage.finalize_to_temp, self._dir)
            share_rel = storage.rel_to_share(moved)
            local_path = None
        except Exception as e:
            log.error("session.finalize_to_temp_failed", error=str(e))
            await self._safe_emit("session.share_move_failed", error=str(e))

        db_status = (
            "failed" if self.status.state == State.FAILED
            else "inbox" if share_rel
            else "local_only"
        )
        await self._upsert_db(
            status=db_status,
            extra={"share_path": share_rel, "local_path": local_path},
        )

        # Auto-summary in the background (chat transcript based). Skip for
        # failed sessions and when the claude binary isn't installed.
        import shutil as _shutil

        if (
            settings.summary_enabled
            and settings.summary_auto_on_end
            and share_rel
            and self.status.state != State.FAILED
            and _shutil.which(settings.summary_claude_binary)
        ):
            rec_id = self.status.recording_id

            async def _bg() -> None:
                try:
                    from omnibus.services.summarizer import summarize_recording
                    await summarize_recording(rec_id)
                except Exception:
                    log.exception("session.summary_failed", recording_id=rec_id)

            asyncio.create_task(_bg(), name=f"summary-{rec_id}")


class SessionRegistry:
    """Multi-session registry bounded by the slot pool."""

    def __init__(self) -> None:
        self._sessions: dict[str, MeetingSession] = {}

    def active(self) -> list[MeetingSession]:
        return [s for s in self._sessions.values() if s.status.state in ACTIVE_STATES]

    def create(self, join_url: str, *, owner: User, use_identity: bool = True) -> MeetingSession:
        # Guest/anonymous joins are GONE — every recording joins signed in as
        # its owner. `use_identity` is accepted for API compat and ignored.
        if pool.free_count == 0:
            raise RuntimeError(
                f"All {settings.max_concurrent_sessions} recording slots are busy."
            )
        # A sign-in in progress holds the user's Chromium profile — a
        # second launch on the same profile fails with 'already in use'.
        from omnibus.auth.teams_session import registry as auth_registry

        if auth_registry.current_for(owner.id) is not None:
            raise RuntimeError(
                "Your Teams sign-in is still in progress — save or cancel "
                "it (Teams Sign-in tab) before recording."
            )
        if any(s.owner.id == owner.id for s in self.active()):
            raise RuntimeError(
                "You already have a recording running — your Teams profile "
                "can only drive one browser at a time. Wait for it to finish."
            )
        session = MeetingSession(join_url=join_url, owner=owner)
        self._sessions[session.id] = session
        # GC finished sessions so the dict doesn't grow forever.
        for sid in [s.id for s in self._sessions.values()
                    if s.status.state in (State.DONE, State.FAILED)][:-20]:
            self._sessions.pop(sid, None)
        return session

    def get(self, session_id: str) -> Optional[MeetingSession]:
        return self._sessions.get(session_id)

    def list(self) -> list[MeetingSession]:
        return list(self._sessions.values())


registry = SessionRegistry()
