"""ICS feed fetcher + parser. No Microsoft API dependency.

A user publishes their Outlook/Google/iCloud calendar as a read-only ICS URL,
gives that URL to the bot, and we poll it like any other web resource. Works
across any tenant and any calendar provider.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

import httpx
import structlog
from icalendar import Calendar

log = structlog.get_logger(__name__)

# Matches a Teams meet-up join URL anywhere in a string. Same RE as before,
# kept here so this module has no Graph-side dependency.
_TEAMS_URL_RE = re.compile(
    r'https?://teams\.(?:microsoft|live)\.com/[lL]/meetup-join/[^\s"\'<>]+',
    re.I,
)


@dataclass
class IcsEvent:
    uid: str
    subject: str
    start_utc: datetime
    end_utc: datetime
    join_url: str
    source_name: str

    @property
    def starts_in_seconds(self) -> float:
        return (self.start_utc - datetime.now(timezone.utc)).total_seconds()


async def fetch_and_parse(name: str, url: str) -> list[IcsEvent]:
    """Fetch an ICS URL and return future events that contain a Teams join URL."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(url, headers={"Accept": "text/calendar"})
    response.raise_for_status()
    try:
        cal = Calendar.from_ical(response.text)
    except Exception as e:
        raise ValueError(f"Could not parse ICS feed: {e}")

    events: list[IcsEvent] = []
    now_utc = datetime.now(timezone.utc)
    for comp in cal.walk("VEVENT"):
        join_url = _extract_teams_url(comp)
        if not join_url:
            continue
        start, end = _extract_times(comp)
        if start is None:
            continue
        # Skip events that have already ended.
        if end is not None and end < now_utc:
            continue
        events.append(
            IcsEvent(
                uid=str(comp.get("UID") or ""),
                subject=str(comp.get("SUMMARY") or "(no subject)"),
                start_utc=start,
                end_utc=end or start,
                join_url=join_url,
                source_name=name,
            )
        )
    log.info("ics.parsed", source=name, events=len(events))
    return events


def _extract_teams_url(comp) -> Optional[str]:
    # Outlook publishes the URL in two well-known places. Try the explicit
    # X- properties first since they're not subject to HTML escaping; fall
    # back to body-text scanning for non-Outlook ICS feeds.
    for key in (
        "X-MICROSOFT-SKYPETEAMSMEETINGURL",
        "X-MICROSOFT-ONLINEMEETINGEXTERNALLINK",
    ):
        val = comp.get(key)
        if val:
            s = str(val).strip()
            if s.startswith(("http://", "https://")):
                return s
    for key in ("DESCRIPTION", "X-ALT-DESC", "LOCATION"):
        body = comp.get(key)
        if not body:
            continue
        match = _TEAMS_URL_RE.search(str(body))
        if match:
            return match.group(0)
    return None


def _extract_times(comp) -> tuple[Optional[datetime], Optional[datetime]]:
    dtstart = comp.get("DTSTART")
    dtend = comp.get("DTEND")
    start = _to_utc(dtstart.dt) if dtstart is not None else None
    end = _to_utc(dtend.dt) if dtend is not None else None
    return start, end


def _to_utc(d) -> datetime:
    if isinstance(d, datetime):
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
    if isinstance(d, date):
        # All-day event; treat as midnight UTC of that day.
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    raise TypeError(f"Unexpected date type: {type(d)!r}")
