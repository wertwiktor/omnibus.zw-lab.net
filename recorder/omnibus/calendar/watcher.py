"""Calendar watcher: polls all enabled ICS subscriptions, auto-triggers a
meeting session shortly before each upcoming Teams event.

Guest joins are removed, so auto-joins must run under a real saved identity:
the subscription creator's (matched by their saved Teams sign-in). Events are
skipped with a loud log when no usable sign-in exists.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import structlog

from omnibus.auth.teams_session import email_for, profile_status
from omnibus.bot.session import registry
from omnibus.calendar.ics import IcsEvent, fetch_and_parse
from omnibus.config import settings
from omnibus.resources import pool
from omnibus.security import User
from omnibus.services import ics_calendars as ics_svc

log = structlog.get_logger(__name__)


def _signed_in_users() -> list[str]:
    """User ids that currently have a working saved Teams sign-in."""
    return [
        p.name.removesuffix(".cookies.json")
        for p in settings.auth_profiles_dir.glob("*.cookies.json")
        if profile_status(p.name.removesuffix(".cookies.json")).get("signed_in")
    ]


@dataclass
class WatcherState:
    enabled: bool = False
    last_polled_at: Optional[str] = None
    upcoming: list[dict] = field(default_factory=list)
    triggered_event_uids: set[str] = field(default_factory=set)


class CalendarWatcher:
    def __init__(self) -> None:
        self.state = WatcherState()
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self.state.enabled = True
        self._task = asyncio.create_task(self._run(), name="ics-watcher")

    async def stop(self) -> None:
        self.state.enabled = False
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        log.info("watcher.started")
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:
                log.exception("watcher.tick_failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=settings.calendar_poll_seconds
                )
            except asyncio.TimeoutError:
                pass
        log.info("watcher.stopped")

    async def _tick(self) -> None:
        subs = await ics_svc.list_subscriptions()
        all_events: list[IcsEvent] = []
        for sub in subs:
            if not sub.get("enabled"):
                continue
            try:
                events = await fetch_and_parse(sub["name"], sub["url"])
                await ics_svc.record_poll(sub["id"], error=None)
                all_events.extend(events)
            except Exception as e:
                log.warning("watcher.poll_failed", source=sub["name"], error=str(e))
                try:
                    await ics_svc.record_poll(sub["id"], error=str(e))
                except Exception:
                    pass

        horizon = settings.calendar_lookahead_minutes * 60
        all_events.sort(key=lambda e: e.start_utc)
        upcoming = [e for e in all_events if e.starts_in_seconds <= horizon]

        self.state.last_polled_at = datetime.now(timezone.utc).isoformat()
        self.state.upcoming = [
            {
                "uid": e.uid,
                "subject": e.subject,
                "source": e.source_name,
                "start_utc": e.start_utc.isoformat(),
                "starts_in_seconds": e.starts_in_seconds,
            }
            for e in upcoming
        ]

        for ev in upcoming:
            self._maybe_trigger(ev)

    def _maybe_trigger(self, ev: IcsEvent) -> None:
        if not ev.uid or ev.uid in self.state.triggered_event_uids:
            return
        if ev.starts_in_seconds > settings.calendar_join_lead_seconds:
            return
        if ev.starts_in_seconds < -120:
            self.state.triggered_event_uids.add(ev.uid)
            return
        if pool.free_count == 0:
            log.info("watcher.busy_skip", uid=ev.uid, subject=ev.subject)
            return

        log.info("watcher.triggering", uid=ev.uid, subject=ev.subject)
        try:
            owner = self._auto_join_owner()
            if owner is None:
                log.warning(
                    "watcher.no_identity_skip", uid=ev.uid, subject=ev.subject,
                    hint="Guest joins are removed; auto-join needs exactly one "
                         "user with a saved Teams sign-in.",
                )
                self.state.triggered_event_uids.add(ev.uid)
                return
            session = registry.create(ev.join_url, owner=owner)
            session.start()
            self.state.triggered_event_uids.add(ev.uid)
        except Exception:
            log.exception("watcher.create_failed")

    def _auto_join_owner(self) -> Optional[User]:
        """The identity calendar auto-joins run under.

        Single-identity rule for now: exactly one saved signed-in user on the
        box → auto-joins run as them; zero or several → skip (ambiguous).
        """
        users = _signed_in_users()
        if len(users) != 1:
            return None
        uid = users[0]
        email = email_for(uid) or "unknown@zw-engineering.de"
        return User(id=uid, email=email)


watcher = CalendarWatcher()
