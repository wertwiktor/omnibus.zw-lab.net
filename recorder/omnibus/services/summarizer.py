"""Auto-summarization of meeting transcripts via the `claude` CLI."""
from __future__ import annotations

import asyncio
from typing import Optional

import structlog

from omnibus.config import settings
from omnibus.services import recordings as rec_svc

log = structlog.get_logger(__name__)

_locks: dict[str, asyncio.Lock] = {}


class SummarizationError(RuntimeError):
    pass


def _lock_for(recording_id: str) -> asyncio.Lock:
    lock = _locks.get(recording_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[recording_id] = lock
    return lock


SYSTEM_PROMPT = (
    "You summarize Microsoft Teams meeting transcripts. The transcript is "
    "scraped from the chat pane, so it may be incomplete (voice content is "
    "not transcribed). Be honest about that. Output plain text, no "
    "markdown headers — just short labeled sections separated by blank lines:\n"
    "Participants: comma-separated names\n"
    "Topics: 3-7 bullets prefixed with '- '\n"
    "Decisions: bullets, or 'None captured'\n"
    "Action items: 'who — what (when)' bullets, or 'None captured'\n"
    "Open questions: bullets, or 'None'\n"
    "If the transcript has no substantive content, output a single line: "
    "'No substantive content captured (voice-only call or very short chat).'"
)


def _build_input(rec: dict, transcript: list[dict]) -> str:
    parts: list[str] = []
    parts.append(f"Meeting title: {rec.get('title') or '(untitled)'}")
    parts.append(f"Started: {rec.get('started_at') or '?'}")
    dur = rec.get("duration_seconds")
    if dur is not None:
        parts.append(f"Duration: {dur // 60}m{dur % 60:02d}s")
    if rec.get("participants"):
        parts.append(f"Participants seen by bot: {', '.join(rec['participants'])}")
    parts.append("")
    parts.append("Chat transcript (oldest first):")
    if not transcript:
        parts.append("(no chat messages captured)")
    else:
        for msg in transcript:
            ts = (msg.get("ts") or "")[11:19]
            parts.append(f"[{ts}] {msg.get('author') or '?'}: {msg.get('body') or ''}")
    text = "\n".join(parts)
    if len(text) > settings.summary_max_input_chars:
        text = text[: settings.summary_max_input_chars] + "\n…[truncated]"
    return text


async def _run_claude(prompt_text: str) -> str:
    import shutil

    if shutil.which(settings.summary_claude_binary) is None:
        raise SummarizationError(
            f"'{settings.summary_claude_binary}' binary not found on this host"
        )
    cmd = [
        settings.summary_claude_binary,
        "--print",
        "--tools", "",
        "--model", settings.summary_model,
        "--max-budget-usd", str(settings.summary_max_budget_usd),
        "--output-format", "text",
        "--append-system-prompt", SYSTEM_PROMPT,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(prompt_text.encode("utf-8")),
            timeout=settings.summary_timeout_seconds,
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise SummarizationError(f"claude timed out after {settings.summary_timeout_seconds}s")
    if proc.returncode != 0:
        raise SummarizationError(
            f"claude exited {proc.returncode}: {stderr.decode('utf-8', 'replace')[:500]}"
        )
    out = stdout.decode("utf-8", "replace").strip()
    if not out:
        raise SummarizationError("claude returned empty output")
    return out


async def summarize_recording(recording_id: str) -> Optional[dict]:
    if not settings.summary_enabled:
        raise SummarizationError("summary_enabled=False")
    async with _lock_for(recording_id):
        rec = await rec_svc.get(recording_id)
        if rec is None:
            return None
        rec_dir = rec_svc.resolve_dir(rec)
        transcript = rec_svc.read_transcript(rec_dir) if rec_dir else []
        prompt = _build_input(rec, transcript)
        try:
            text = await _run_claude(prompt)
        except SummarizationError:
            raise
        except Exception as e:
            raise SummarizationError(str(e)) from e
        return await rec_svc.set_auto_summary(recording_id, text)
