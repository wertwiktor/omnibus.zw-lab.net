"""Presence and active-speaker tracking for one meeting.

Both trackers are pure state machines driven by periodic snapshots. Times are
OFFSET SECONDS from the meeting start (t0), so intervals read as "spoke from
00:03:12 to 00:03:45" and don't depend on wall-clock/timezone parsing. The
caller stamps absolute timestamps on the emitted events separately.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def _fmt(seconds: float) -> str:
    """mm:ss (or h:mm:ss) label for an offset, for human-readable metadata."""
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


@dataclass
class _Span:
    """One presence/speaking record for a single name across the meeting."""
    first: float
    last: float
    intervals: list[list[float]] = field(default_factory=list)
    seconds: float = 0.0
    _open: float | None = None

    def open_at(self, t: float) -> None:
        if self._open is None:
            self._open = t
        self.last = t

    def close_at(self, t: float) -> float:
        """Close the open interval; return the segment length (0 if none)."""
        if self._open is None:
            return 0.0
        seg = max(0.0, t - self._open)
        self.intervals.append([round(self._open, 1), round(t, 1)])
        self.seconds += seg
        self._open = None
        self.last = t
        return seg


class _BaseTracker:
    def __init__(self) -> None:
        self._active: set[str] = set()
        self._spans: dict[str, _Span] = {}

    def _ensure(self, name: str, t: float) -> _Span:
        span = self._spans.get(name)
        if span is None:
            span = _Span(first=t, last=t)
            self._spans[name] = span
        return span

    def finalize(self, t: float) -> None:
        """Close any still-open intervals at meeting end."""
        for name in list(self._active):
            self._spans[name].close_at(t)
        self._active.clear()

    @property
    def everyone(self) -> list[str]:
        return sorted(self._spans)

    @property
    def active_now(self) -> set[str]:
        return set(self._active)


class PresenceTracker(_BaseTracker):
    """Who is currently in the meeting, and the union of everyone ever seen."""

    def update(self, present: set[str], t: float) -> tuple[list[str], list[str]]:
        """Ingest the current roster. Returns (joined, left) name lists."""
        joined = sorted(present - self._active)
        left = sorted(self._active - present)
        for name in joined:
            self._ensure(name, t).open_at(t)
        for name in left:
            self._spans[name].close_at(t)
        for name in present:
            self._spans[name].last = t
        self._active = set(present)
        return joined, left

    def timeline(self) -> list[dict]:
        """Per-person presence record: when they were in the meeting."""
        out: list[dict] = []
        for name in self.everyone:
            span = self._spans[name]
            out.append({
                "name": name,
                "first_seen": round(span.first, 1),
                "last_seen": round(span.last, 1),
                "first_seen_label": _fmt(span.first),
                "intervals": span.intervals,
                "seconds_present": round(span.seconds, 1),
                "still_present": span._open is not None,
            })
        return out


class SpeakerTracker(_BaseTracker):
    """Who is actively speaking, as a timeline of segments + talk-time totals."""

    def update(self, speaking: set[str], t: float) -> tuple[list[str], list[tuple[str, float]]]:
        """Ingest the currently-speaking set.

        Returns (started, stopped) where started is a list of names and stopped
        is a list of (name, segment_seconds) so the caller can log durations.
        """
        started = sorted(speaking - self._active)
        stopped_names = sorted(self._active - speaking)
        for name in started:
            self._ensure(name, t).open_at(t)
        stopped: list[tuple[str, float]] = []
        for name in stopped_names:
            seg = self._spans[name].close_at(t)
            stopped.append((name, round(seg, 1)))
        self._active = set(speaking)
        return started, stopped

    def totals(self, *, meeting_seconds: float | None = None) -> list[dict]:
        """Per-person talk time, longest-talker first, with % share of meeting."""
        out: list[dict] = []
        for name, span in self._spans.items():
            share = None
            if meeting_seconds and meeting_seconds > 0:
                share = round(100.0 * span.seconds / meeting_seconds, 1)
            out.append({
                "name": name,
                "seconds": round(span.seconds, 1),
                "label": _fmt(span.seconds),
                "segments": len(span.intervals),
                "share_pct": share,
            })
        out.sort(key=lambda r: r["seconds"], reverse=True)
        return out

    def timeline(self) -> list[dict]:
        """Every speaking segment, chronological: {name, start, end, ...}."""
        segs: list[dict] = []
        for name, span in self._spans.items():
            for start, end in span.intervals:
                segs.append({
                    "name": name,
                    "start": start,
                    "end": end,
                    "seconds": round(end - start, 1),
                    "start_label": _fmt(start),
                })
        segs.sort(key=lambda s: s["start"])
        return segs
