from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omnibus.bot.broadcaster import broadcaster


class EventLog:
    """Append-only JSONL event log for a single meeting.

    Also republishes events to the in-process broadcaster so the web UI's
    SSE stream sees them in real time.
    """

    def __init__(self, path: Path, *, session_id: str | None = None) -> None:
        self.path = path
        self.session_id = session_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self.path.touch(exist_ok=True)

    async def emit(self, kind: str, **fields: Any) -> dict[str, Any]:
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "mono": time.monotonic(),
            "kind": kind,
            "session_id": self.session_id,
            **fields,
        }
        line = json.dumps(event, ensure_ascii=False)
        async with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except FileNotFoundError:
                # The recording dir has already been finalized (moved to the
                # share TEMP inbox). Late events (repeat stop clicks, stragler
                # bot callbacks) still go to the live SSE stream — losing the
                # disk line is fine, crashing the caller is not.
                pass
        broadcaster.publish(event)
        return event
