"""Per-user interactive Teams sign-in driven from the web UI.

Each Entra user gets their own persistent Chromium profile at
settings.auth_profiles_dir/<user_id>/. The user completes Microsoft SSO/MFA
once in the supervised noVNC view; afterwards their recordings can join
signed in as them (use_identity=True).
"""
from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import structlog

from omnibus.bot.browser import export_cookies, first_app_page, teams_browser_context
from omnibus.bot.display import DisplayHandle, start_display
from omnibus.config import settings
from omnibus.resources import ResourceSlot, pool
from omnibus.security import User
from omnibus.supervise.vnc import VncHandle, start_vnc

log = structlog.get_logger(__name__)


class AuthState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    AWAITING_USER = "awaiting_user"
    SAVING = "saving"
    DONE = "done"
    CANCELLED = "cancelled"
    FAILED = "failed"


ACTIVE = {AuthState.STARTING, AuthState.AWAITING_USER, AuthState.SAVING}


@dataclass
class AuthStatus:
    auth_id: str
    owner_id: str = ""
    owner_email: str = ""
    state: AuthState = AuthState.IDLE
    novnc_port: Optional[int] = None
    error: Optional[str] = None
    profile_path: Optional[str] = None

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["state"] = self.state.value
        return d


def profile_dir_for(user_id: str) -> Path:
    return settings.auth_profiles_dir / user_id


def cookie_snapshot_for(user_id: str) -> Path:
    """JSON snapshot of the user's live auth cookies, captured at sign-in.

    This — NOT the Chromium cookie DB — is the source of truth for identity
    joins. Chromium purges session cookies (ESTSAUTH) at every startup, so
    the on-disk DB loses the sign-in the moment a new browser launches; the
    snapshot is ours and survives.
    """
    return settings.auth_profiles_dir / f"{user_id}.cookies.json"


def _email_marker_for(user_id: str) -> Path:
    return settings.auth_profiles_dir / f"{user_id}.email"


def email_for(user_id: str) -> Optional[str]:
    """Owner email recorded at sign-in time (for calendar auto-joins)."""
    try:
        return _email_marker_for(user_id).read_text().strip() or None
    except OSError:
        return None


AUTH_COOKIE_NAMES = {"TSAUTHCOOKIE", "SSOAUTHCOOKIE", "skypetoken_asm"}


def _has_auth_cookies(cookies: list[dict]) -> bool:
    return any(
        c.get("name", "").startswith("ESTSAUTH") or c.get("name") in AUTH_COOKIE_NAMES
        for c in cookies
    )


def profile_status(user_id: str) -> dict:
    """Signed-in state of a user's saved Teams identity.

    Authoritative check = the cookie snapshot taken at sign-in time. The
    Chromium profile dir merely accelerates subsequent SSO; its cookie DB is
    NOT trusted (Chromium drops session cookies on startup).
    """
    profile = profile_dir_for(user_id)
    snap = cookie_snapshot_for(user_id)
    out = {"saved": profile.exists(), "signed_in": False, "path": str(profile)}
    if not snap.exists():
        return out
    out["size_bytes"] = snap.stat().st_size
    out["modified_at"] = snap.stat().st_mtime
    try:
        cookies = json.loads(snap.read_text())
        auth = [c for c in cookies if _has_auth_cookies([c])]
        out["signed_in"] = bool(auth)
        out["auth_cookies"] = len(auth)
    except (json.JSONDecodeError, OSError):
        out["signed_in"] = None
    return out


def clear_profile(user_id: str) -> None:
    cookie_snapshot_for(user_id).unlink(missing_ok=True)
    _email_marker_for(user_id).unlink(missing_ok=True)
    profile = profile_dir_for(user_id)
    if not profile.exists():
        return
    shutil.rmtree(profile, ignore_errors=True)
    if profile.exists():
        # rmtree left residue (open file handles from a live Chromium).
        raise RuntimeError(
            "Profile directory could not be fully removed — a browser may "
            "still be running on it. Cancel any sign-in/recording first."
        )


class TeamsAuthSession:
    def __init__(self, owner: User) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.owner = owner
        self.status = AuthStatus(
            auth_id=self.id, owner_id=owner.id, owner_email=owner.email
        )
        self._slot: Optional[ResourceSlot] = None
        self._display: Optional[DisplayHandle] = None
        self._vnc: Optional[VncHandle] = None
        self._task: Optional[asyncio.Task] = None
        self._finish = asyncio.Event()
        self._cancel = asyncio.Event()

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name=f"teams-auth-{self.id}")

    def request_finish(self) -> None:
        self._finish.set()

    def request_cancel(self) -> None:
        self._cancel.set()

    async def _watch_signed_in(self, context) -> None:
        """Resolve once the user has actually signed in.

        Polls the live BrowserContext's cookie jar — unlike a page object it
        survives the MS login redirect chain (redirects orphan pages, which
        made a page-bound probe silently never fire), and unlike Chromium's
        on-disk cookie DB it always reflects current state.
        """
        while True:
            await asyncio.sleep(5)
            try:
                cookies = await context.cookies(
                    ["https://teams.microsoft.com", "https://login.microsoftonline.com"]
                )
                if _has_auth_cookies(cookies):
                    log.info("teams_auth.signed_in_detected", user=self.owner.email)
                    return
            except Exception:
                # Context mid-navigation / closing — keep polling.
                continue

    async def wait_done(self, timeout: float = 30.0) -> None:
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=timeout)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def _run(self) -> None:
        try:
            self.status.state = AuthState.STARTING
            self._slot = pool.acquire()
            self._display = start_display(
                display_number=self._slot.display_number,
                width=settings.auth_display_width,
                height=settings.auth_display_height,
                depth=settings.display_depth,
                pulse_sink_name=self._slot.pulse_sink,
            )
            self._vnc = start_vnc(
                display=self._display.display,
                vnc_port=self._slot.vnc_port,
                novnc_port=self._slot.novnc_port,
                novnc_dir=settings.novnc_dir,
                env=self._display.env,
            )
            self.status.novnc_port = self._vnc.novnc_port

            async with teams_browser_context(
                env=self._display.env,
                width=self._display.width,
                height=self._display.height,
                profile_dir=profile_dir_for(self.owner.id),
            ) as context:
                page = await first_app_page(context)
                await page.goto("https://teams.microsoft.com/")
                try:
                    cdp = await context.new_cdp_session(page)
                    target = await cdp.send("Browser.getWindowForTarget")
                    await cdp.send(
                        "Browser.setWindowBounds",
                        {
                            "windowId": target["windowId"],
                            "bounds": {"windowState": "fullscreen"},
                        },
                    )
                except Exception:
                    log.exception("teams_auth.fullscreen_failed")
                self.status.state = AuthState.AWAITING_USER

                # Wait for whichever comes first: user clicks Save/Cancel in
                # the UI, sign-in auto-detected (Teams app shell loaded), or
                # the whole thing times out abandoned.
                finish_task = asyncio.create_task(self._finish.wait())
                cancel_task = asyncio.create_task(self._cancel.wait())
                detect_task = asyncio.create_task(self._watch_signed_in(context))
                done, pending = await asyncio.wait(
                    [finish_task, cancel_task, detect_task],
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=settings.auth_session_timeout_seconds,
                )
                for t in pending:
                    t.cancel()

                if self._cancel.is_set():
                    self.status.state = AuthState.CANCELLED
                elif not done:
                    self.status.state = AuthState.FAILED
                    self.status.error = "Sign-in timed out (abandoned)"
                else:
                    # Snapshot the live cookies NOW, while the context holds
                    # them — this JSON file (not Chromium's purge-happy cookie
                    # DB) is what future identity joins inject at launch.
                    self.status.state = AuthState.SAVING
                    await asyncio.sleep(2)
                    n = await export_cookies(
                        context, cookie_snapshot_for(self.owner.id)
                    )
                    _email_marker_for(self.owner.id).write_text(self.owner.email)
                    self.status.profile_path = str(profile_dir_for(self.owner.id))
                    self.status.state = AuthState.DONE
                    log.info(
                        "teams_auth.saved",
                        user=self.owner.email,
                        auto_detected=detect_task in done,
                        cookies=n,
                        profile=self.status.profile_path,
                    )

        except Exception as e:
            log.exception("teams_auth.failed", id=self.id)
            self.status.state = AuthState.FAILED
            self.status.error = str(e)
        finally:
            for handle, name in ((self._vnc, "vnc"), (self._display, "display")):
                if handle is not None:
                    try:
                        handle.stop()
                    except Exception:
                        log.exception(f"teams_auth.{name}_stop_failed")
            if self._slot is not None:
                pool.release(self._slot)
                self._slot = None


class TeamsAuthRegistry:
    """One in-flight sign-in per user (different users may overlap)."""

    def __init__(self) -> None:
        self._by_user: dict[str, TeamsAuthSession] = {}

    def current_for(self, user_id: str) -> Optional[TeamsAuthSession]:
        s = self._by_user.get(user_id)
        if s is None or s.status.state not in ACTIVE:
            return None
        return s

    def begin(self, owner: User) -> TeamsAuthSession:
        if self.current_for(owner.id) is not None:
            raise RuntimeError("You already have a Teams sign-in in progress")
        session = TeamsAuthSession(owner)
        self._by_user[owner.id] = session
        return session


registry = TeamsAuthRegistry()
