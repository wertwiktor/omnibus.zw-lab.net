"""Recording storage: local-first recording, TEMP inbox on the share, and
the assign-to-project move.

Flow:
  1. Session records into settings.local_rec_dir/<stamp>-<slug> (fast local disk).
  2. On session end, finalize() moves the folder to <share>/TEMP/<name> and
     writes metadata.json with everything we know about the meeting.
  3. The user assigns it from the UI: assign_to_project() moves the folder to
     <share>/<Project dir>/Meetings/<name> and updates the DB row.
  4. unassign() moves it back to TEMP.

All share paths in the DB are stored RELATIVE to the share root so the mount
point can move without breaking anything.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog

from omnibus.config import settings

log = structlog.get_logger(__name__)

_PROJECT_DIR_RE = re.compile(r"^P\d{5}\s*-\s*.+")


def _slug(s: str, max_len: int = 60) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s or "").strip("-._")
    return (s[:max_len] or "meeting").lower()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_local_dir(hint: Optional[str] = None) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{_slug(hint)}" if hint else ""
    path = settings.local_rec_dir / f"{stamp}{suffix}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def temp_root() -> Path:
    return settings.share_root / settings.temp_dirname


def share_available() -> bool:
    return settings.share_root.is_dir() and any(settings.share_root.iterdir())


def list_project_dirs() -> list[str]:
    """Project folders on the share (P##### - CLIENT - NAME pattern)."""
    if not settings.share_root.is_dir():
        return []
    out = []
    for p in sorted(settings.share_root.iterdir()):
        if p.is_dir() and _PROJECT_DIR_RE.match(p.name):
            out.append(p.name)
    return out


def write_metadata(rec_dir: Path, meta: dict) -> None:
    (rec_dir / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2)
    )


def read_metadata(rec_dir: Path) -> Optional[dict]:
    p = rec_dir / "metadata.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def _move_dir(src: Path, dst: Path) -> Path:
    """Move a directory across filesystems safely (copy+rm on EXDEV)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        # Never clobber: suffix with -1, -2, ...
        base = dst
        for i in range(1, 100):
            candidate = base.with_name(f"{base.name}-{i}")
            if not candidate.exists():
                dst = candidate
                break
    shutil.move(str(src), str(dst))
    return dst


def finalize_to_temp(local_dir: Path) -> Path:
    """Move a finished local recording into the share TEMP inbox.

    Returns the new absolute path. Raises if the share is not mounted —
    caller keeps the local copy and marks the DB row accordingly.
    """
    if not share_available():
        raise RuntimeError(f"Share not mounted at {settings.share_root}")
    dst = temp_root() / local_dir.name
    moved = _move_dir(local_dir, dst)
    log.info("storage.finalized", src=str(local_dir), dst=str(moved))
    return moved


def rel_to_share(path: Path) -> str:
    return str(path.relative_to(settings.share_root))


def abs_from_rel(rel: str) -> Path:
    p = (settings.share_root / rel).resolve()
    if settings.share_root.resolve() not in p.parents and p != settings.share_root.resolve():
        raise ValueError("Path escapes share root")
    return p


def assign_to_project(rel_path: str, project_dir: str) -> str:
    """Move a TEMP recording into <project_dir>/Meetings/. Returns new rel path."""
    if project_dir not in list_project_dirs():
        raise ValueError(f"Unknown project folder: {project_dir}")
    src = abs_from_rel(rel_path)
    if not src.is_dir():
        raise FileNotFoundError(f"Recording folder missing: {rel_path}")
    dst = settings.share_root / project_dir / settings.project_meetings_dirname / src.name
    moved = _move_dir(src, dst)
    log.info("storage.assigned", src=rel_path, dst=rel_to_share(moved))
    return rel_to_share(moved)


def unassign_to_temp(rel_path: str) -> str:
    """Move an assigned recording back to the TEMP inbox."""
    src = abs_from_rel(rel_path)
    if not src.is_dir():
        raise FileNotFoundError(f"Recording folder missing: {rel_path}")
    dst = temp_root() / src.name
    moved = _move_dir(src, dst)
    log.info("storage.unassigned", src=rel_path, dst=rel_to_share(moved))
    return rel_to_share(moved)


def scan_temp_folders() -> list[Path]:
    root = temp_root()
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())
