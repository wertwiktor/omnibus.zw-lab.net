from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import structlog
from playwright.async_api import Browser, BrowserContext, Page, TimeoutError as PWTimeout

from omnibus.bot.browser import first_app_page
from omnibus.config import settings

log = structlog.get_logger(__name__)

# Selector candidates — Teams' web UI changes; we try them in order and use the
# first one that resolves. Add new variants here when something breaks.
SEL_NAME_INPUT = [
    'input[data-tid="prejoin-display-name-input"]',
    'input#username',
    'input[placeholder*="name" i]',
]
SEL_JOIN_NOW = [
    'button[data-tid="prejoin-join-button"]',
    'button:has-text("Join now")',
    'button:has-text("Dołącz teraz")',  # PL
    'button:has-text("Beitreten")',     # DE
]
SEL_MIC_TOGGLE = [
    'div[data-tid="toggle-mute"]',
    'button[data-tid="toggle-mute"]',
    'button[aria-label*="mic" i]',
]
SEL_CAM_TOGGLE = [
    'div[data-tid="toggle-video"]',
    'button[data-tid="toggle-video"]',
    'button[aria-label*="camera" i]',
]
SEL_LEAVE = [
    'button[data-tid="hangup-main-btn"]',
    'button[data-tid="call-hangup"]',
    'button[aria-label*="Leave" i]',
    'button:has-text("Leave")',
]
SEL_PEOPLE_TOGGLE = [
    'button[data-tid="roster-button"]',
    'button[aria-label*="People" i]',
    'button[aria-label*="Participants" i]',
]
SEL_ROSTER_ITEM = [
    # Current Teams web (Fluent v9). Two known prefixes depending on the
    # meeting type:
    #   participantsInCall-<name>  — /l/meetup-join, /l/chat call panel
    #   attendeesInMeeting-<name>  — /meet/<id> meet-now style URLs
    'div[data-tid^="participantsInCall-"]',
    'div[data-tid^="attendeesInMeeting-"]',
    'div[data-tid^="participantsFromThread-"]',
    # Older layouts kept as fallbacks.
    'div[data-tid="roster-list-item"]',
    'li[data-tid="participant-list-item"]',
    '[role="treeitem"][data-tid*="participant"]',
]
# Prefixes the bot strips from data-tid to recover the participant's name.
# Ordered so the most likely match comes first.
ROSTER_TID_PREFIXES = (
    "participantsInCall-",
    "attendeesInMeeting-",
    "participantsFromThread-",
)
# Substrings that mark the ACTIVE-SPEAKER state on a participant row/tile (the
# "frame lights up" indicator). Teams toggles a voice-level animation / ring
# when someone talks. These are PROVISIONAL — our only DOM captures are solo
# meetings, so the exact token is confirmed on the first multi-party run via
# dump_speaker_diagnostics(), then this list is tightened. Matched
# case-insensitively against data-tid / class / aria-label; data-is-speaking
# ="true" is treated as a definitive signal regardless of this list.
SPEAKER_SIGNAL_TOKENS = (
    "isspeaking",
    "is-speaking",
    "speaking",
    "voicelevel",
    "voice-level",
    "dominantspeaker",
    "dominant-speaker",
    "activespeaker",
    "active-speaker",
    "speaker-ring",
    "speaking-indicator",
)
# Small badge in the meeting top bar that shows the total in-call participant
# count (including the bot). Stays in the DOM even when the People pane is
# collapsed, so it's the most reliable solo-detection signal.
SEL_ROSTER_COUNT_BADGE = [
    # Top-bar People button badge — visible whenever the People pane *can* be
    # opened. Survives the pane being collapsed; lost when the chat pane is
    # docked instead (Teams web only renders one side panel at a time).
    '[data-tid="roster-button-tile"]',
    'button[data-tid="roster-button"] [aria-label]',
    # Chat pane header counter — visible whenever the chat pane is open.
    # Together with roster-button-tile this covers both side-panel states.
    '[data-tid="chat-header-participant-count"]',
    'button[data-tid="chat-header-participant-count"]',
]
# Wrapper around the roster pane. Its inner text starts with a header line
# like "Currently in this call (2)" or "In this meeting (7)" — we parse the
# number out of it as a fallback count source when the badge is missing.
SEL_ROSTER_WRAPPER = [
    'div[data-tid="calling-roster-attendees"]',
    'div[data-tid="calling-roster-wrapper"]',
]
ROSTER_HEADER_COUNT_RE = re.compile(
    r"(?:Currently in this call|In this meeting|In this call|Participants)\s*\((\d+)\)",
    re.IGNORECASE,
)
SEL_CHAT_TOGGLE = [
    'button[data-tid="chat-button"]',
    'button[aria-label*="chat" i]',
    'button:has-text("Chat")',
]
SEL_CHAT_MESSAGE = [
    'div[data-tid="chat-pane-message"]',
    'div[data-tid="chat-message"]',
    '[role="group"][data-tid*="message"]',
    'div.ui-chat__message',
]
SEL_CHAT_AUTHOR = [
    '[data-tid="message-author-name"]',
    '.ui-chat__message__author',
]
SEL_CHAT_BODY = [
    '[data-tid="message-body-content"]',
    '.ui-chat__message__content',
]
SEL_LOBBY_BANNER = [
    'text=When the meeting starts',
    'text=Someone in the meeting should let you in soon',
    'text=Waiting for others to join',
    'text=You\'re in the lobby',
]
# Dialogs/overlays that block the prejoin screen — try to dismiss before
# clicking Join, and again periodically while we wait to be admitted.
#
# Each entry must be SAFE TO CLICK whenever it's visible — never include a
# selector that could mean "abort" in any context the bot might encounter
# (e.g. a bare "Cancel" hits the Cancel button on the audio-confirmation
# dialog and silently aborts the join). When in doubt, leave it to the
# human via noVNC supervised mode.
SEL_DISMISS_OVERLAY = [
    # Post-join confirmation when joining with mic+cam off — must press through.
    'button:has-text("Continue without audio or video")',
    'button:has-text("Continue without audio and video")',
    'button:has-text("Join without audio or video")',
    'button:has-text("Continue anyway")',
    # First-launch / app-picker dialogs.
    'button:has-text("Continue on this browser")',
    'button:has-text("Use the web app instead")',
    'button:has-text("Use the web app")',
    'button:has-text("Join on the web instead")',
    # Generic dismiss / close affordances (only inside dialogs, not free Cancel).
    'button[data-tid="dismiss-button"]',
    'button[aria-label="Dismiss" i]',
    'button[aria-label="Close" i]',
    '[role="dialog"] button:has-text("Got it")',
    '[role="dialog"] button:has-text("OK")',
    # Cookie banners last (least urgent).
    'button:has-text("Accept all")',
    'button:has-text("Reject all")',
    'button:has-text("Accept")',
]

# "Forward" actions — pages where the bot needs to click a button to make
# progress (not dismiss an overlay). Separate from SEL_DISMISS_OVERLAY because
# their semantics are different: failure to find them is not an error, but
# success means we expect a new page to load.
SEL_REJOIN = [
    'button:has-text("Rejoin")',
    '[role="button"]:has-text("Rejoin")',
]
SEL_REMOVED_BANNER = [
    'text=You have been removed',
    'text=Meeting ended',
    'text=Call ended',
]

# In-meeting "User Facing Diagnostics" (ufd_*) notification banners — benign
# device/network alerts Teams stacks over the meeting view (No Microphone, No
# Speaker, No Camera, poor network, …). They aren't modal, but they clutter
# the recording and the live view, so we auto-dismiss them. Every ufd alert
# renders a close button tagged callingAlertDismissButton_<name>; matching the
# prefix covers the whole family without chasing each variant. Every one of
# these is purely a "dismiss this notice" button — none abort the call — so
# they're safe to click unconditionally.
SEL_DISMISS_CALL_BANNER = [
    'button[data-tid^="callingAlertDismissButton_"]',
]


@dataclass
class ParticipantSnapshot:
    names: list[str]
    # Best-effort total in-call participant count (includes the bot). Read from
    # the roster badge, the wrapper header text, or the count of named rows —
    # whichever yields the largest number. None = no signal, treat as
    # "unknown" rather than zero.
    count: int | None = None
    # Raw signals kept on the side for the per-poll debug event.
    badge_count: int | None = None
    wrapper_count: int | None = None
    roster_selector: str | None = None


@dataclass
class ChatMessage:
    author: str
    body: str
    key: str  # stable-ish dedup key

    @classmethod
    def make(cls, author: str, body: str) -> "ChatMessage":
        return cls(author=author, body=body, key=f"{author}\x00{body}")


async def _first_visible(page: Page, selectors: list[str], timeout: float = 0):
    """Return locator for the first selector that's visible right now.

    The timeout is the TOTAL budget across all selectors, not per-selector.
    All candidates are polled every ~100ms in parallel, so we return as soon
    as any one is visible — typical happy-path latency is well under 200ms.

    `timeout` is in milliseconds (matches Playwright's convention).
    """
    deadline = asyncio.get_event_loop().time() + (timeout / 1000.0) if timeout > 0 else None
    while True:
        for sel in selectors:
            locator = page.locator(sel).first
            try:
                if await locator.count() == 0:
                    continue
                if await locator.is_visible():
                    return locator
            except Exception:
                continue
        if deadline is None or asyncio.get_event_loop().time() >= deadline:
            return None
        await asyncio.sleep(0.1)


def normalize_join_url(url: str, *, anonymous: bool = True) -> str:
    """Force the 'use the web app' path so we don't get punted to a deeplink.

    `anon=true` makes Teams treat us as an anonymous guest — correct for
    guest joins, but it OVERRIDES saved sign-in cookies, so identity joins
    must NOT carry it.
    """
    parsed = urlparse(url.strip())
    qs = parse_qs(parsed.query, keep_blank_values=True)
    if anonymous:
        qs.setdefault("anon", ["true"])
    else:
        qs.pop("anon", None)
    new_query = urlencode({k: v[0] for k, v in qs.items()})
    return urlunparse(parsed._replace(query=new_query))


def _is_teams_app_host(host: str) -> bool:
    """True when `host` serves the signed-in Teams web app.

    Microsoft is migrating the web client from teams.microsoft.com to
    teams.cloud.microsoft (unified *.cloud.microsoft domain); a signed-in
    warmup routinely lands on either.
    """
    return host in ("teams.microsoft.com", "teams.live.com") or (
        host.endswith(".cloud.microsoft") and host.startswith("teams")
    )


class TeamsSession:
    """One Teams web-client meeting session driven by a Playwright Page.

    Designed to be defensive: every interaction goes through `_first_visible`
    with selector fallbacks, and surfaces a `SupervisedHandoff` exception when
    the bot hits a challenge it can't solve (lobby never resolves, captcha,
    sign-in challenge).
    """

    def __init__(
        self,
        context: BrowserContext,
        display_name: str,
        join_url: str,
        *,
        on_event: Optional[Callable[[str, dict], Awaitable[None]]] = None,
        debug_dir: Optional[Path] = None,
        display: Optional[str] = None,
        anonymous: bool = True,
        should_stop: Optional[Callable[[], bool]] = None,
        owner_email: Optional[str] = None,
        cookie_snapshot: Optional[Path] = None,
    ) -> None:
        self.context = context
        self.display_name = display_name
        self.join_url = normalize_join_url(join_url, anonymous=anonymous)
        self.anonymous = anonymous
        self.owner_email = owner_email
        self.cookie_snapshot = cookie_snapshot
        self._should_stop = should_stop or (lambda: False)
        self.page: Optional[Page] = None
        self._on_event = on_event or (lambda *_: asyncio.sleep(0))
        self.debug_dir = debug_dir
        self.display = display
        # The identity warmup is now lazy — run at most once, only if the
        # guest prejoin actually shows. Tracked so a supervised retry doesn't
        # loop back into it.
        self._warmup_done = False

    async def _emit(self, kind: str, **fields) -> None:
        await self._on_event(kind, fields)

    async def _emit_action(self, action: str, **fields) -> None:
        """Granular debug entry: every meaningful bot action goes through here.

        Separate stream from semantic `teams.*` events so the existing
        machine-readable timeline isn't polluted; tools that want a fine-grain
        trace can filter `bot.action` instead.
        """
        try:
            fields.setdefault("url", self.page.url if self.page is not None else None)
        except Exception:
            pass
        await self._on_event("bot.action", {"action": action, **fields})

    async def open(self) -> Page:
        # Use the app-mode window Chromium spawned at launch — opening a fresh
        # page would create a *second* window with a normal address bar.
        page = await first_app_page(self.context)
        page.set_default_timeout(30_000)
        self.page = page
        # Stream every cross-document navigation as a debug event so a chat
        # drift (landing on /l/chat/ instead of a meeting) is visible in the
        # timeline.
        def _on_nav(frame) -> None:
            if frame.parent_frame is not None:
                return  # only main frame
            url = frame.url
            asyncio.create_task(
                self._emit("bot.nav.observed", url=url, is_chat="/l/chat/" in url)
            )
        try:
            page.on("framenavigated", _on_nav)
        except Exception:
            pass
        if "/l/chat/" in self.join_url:
            # The user pasted a chat URL, not a meeting URL. The web app will
            # render a 1:1/group chat, not a prejoin screen — the bot won't
            # find a Join button on its own. Surface it loudly.
            await self._emit(
                "bot.url.chat_drift",
                where="initial",
                url=self.join_url,
                hint="This is a chat URL, not a meeting URL. The bot can't "
                     "auto-start the call from here — paste the /meet/ or "
                     "/l/meetup-join/ URL instead, or take over via VNC and "
                     "press Join.",
            )
        # Optimistic direct join: go straight to the meeting URL. A fully
        # signed-in profile (fresh cookies) renders the signed-in prejoin with
        # NO warmup — the old unconditional /v2/ warmup loaded the entire Teams
        # SPA (~10s+) before every join. If the /meet/ launcher instead punts
        # us to the anonymous guest prejoin, fill_prejoin() detects that and
        # runs the warmup lazily, then reloads (see _identity_warmup).
        await self._emit_action("open.goto", target=self.join_url)
        await self._emit("teams.navigate", url=self.join_url)
        await page.goto(self.join_url, wait_until="domcontentloaded")
        await self._emit_action("open.loaded", target=self.join_url)
        await self._go_fullscreen()
        return page

    async def _identity_warmup(self) -> None:
        """Load the signed-in Teams web app so /meet/ launchers stop dropping
        us to the anonymous guest flow.

        Called LAZILY by fill_prejoin, only when the guest prejoin actually
        shows — the happy path (fresh cookies land straight on the signed-in
        prejoin) never pays for it. Waits for the silent MSAL/SSO redirect
        chain (login.microsoftonline.com -> /v2/authv2 -> app) to finish;
        raises SupervisedHandoff when an interactive sign-in is required.
        """
        assert self.page is not None
        page = self.page
        self._warmup_done = True
        await self._emit_action("open.identity_warmup", target="https://teams.microsoft.com/v2/")
        try:
            await page.goto("https://teams.microsoft.com/v2/", wait_until="domcontentloaded")
            deadline = asyncio.get_event_loop().time() + 90
            settled = 0
            login_stuck = 0
            while asyncio.get_event_loop().time() < deadline:
                if self._should_stop():
                    raise MeetingEnded("stop requested during identity warmup")
                # Check the host BEFORE sleeping — a warm profile is usually
                # already on the app host on the first tick, so the old
                # sleep-then-check burned a needless 2s on every warmup.
                host = urlparse(page.url).hostname or ""
                on_app = _is_teams_app_host(host)
                # Verbose poll trace — one line per tick so a stuck warmup is
                # fully explainable from events.jsonl alone.
                await self._emit_action(
                    "warmup.poll", host=host, on_app=on_app,
                    settled=settled, login_stuck=login_stuck,
                )
                # Two consecutive polls on a Teams app host = redirect chain
                # done. MS serves the signed-in web app from BOTH
                # teams.microsoft.com and teams.cloud.microsoft (2026 rollout).
                settled = settled + 1 if on_app else 0
                if settled >= 2:
                    break
                if host.startswith("login."):
                    # Saved profiles routinely park on NON-credential
                    # interstitials (account picker, "Stay signed in?"). Those
                    # are safe to click through — do it before concluding a
                    # human is needed.
                    advanced = await self._advance_login_interstitial()
                    if advanced:
                        login_stuck = 0
                        await page.wait_for_timeout(2_000)
                        continue
                    # A password/MFA prompt (or anything we don't recognise)
                    # parked for several ticks means real interactive auth —
                    # escalate rather than burning the full budget.
                    login_stuck += 1
                    if login_stuck >= 5:
                        break
                else:
                    login_stuck = 0
                await page.wait_for_timeout(2_000)
            signed_in = settled >= 2
            await self._emit("teams.identity_warmup", url=page.url, signed_in=signed_in)
            if signed_in and self.cookie_snapshot is not None:
                # Roll the snapshot forward: Entra just refreshed the tokens,
                # so persist them for the NEXT launch too.
                try:
                    from omnibus.bot.browser import export_cookies
                    await export_cookies(self.context, self.cookie_snapshot)
                except Exception as e:
                    await self._emit_action("cookie_snapshot.refresh_failed",
                                            error=str(e)[:200])
            if not signed_in:
                # Stuck on an interactive login page — silent SSO refused
                # (expired session / Conditional Access). Hand to a human.
                raise SupervisedHandoff(
                    "identity-signin-required",
                    "Teams wants an interactive sign-in for your saved "
                    "profile. Open the live view, complete the login, then "
                    "click 'I've handled it'.",
                )
        except (SupervisedHandoff, MeetingEnded):
            raise
        except Exception as e:
            await self._emit_action("open.identity_warmup_failed", error=str(e)[:200])

    async def _reload_join_url(self) -> None:
        """Re-navigate to the meeting URL after a lazy warmup, so the now
        signed-in session renders the identity prejoin, not the guest one."""
        assert self.page is not None
        await self._emit_action("open.goto", target=self.join_url, reason="post_warmup_reload")
        await self.page.goto(self.join_url, wait_until="domcontentloaded")
        await self._emit_action("open.loaded", target=self.join_url)
        await self._go_fullscreen()

    async def _advance_login_interstitial(self) -> bool:
        """Click through NON-credential Microsoft login interstitials.

        Saved-profile SSO frequently stalls on pages that need one click but
        no secrets: the account picker ("Pick an account"), the KMSI prompt
        ("Stay signed in?"), or a plain Continue button. Clicking these is
        safe; typing credentials is not — if a password/OTP field is visible
        we return False so the caller escalates to a human.

        Returns True if something was clicked (caller should keep polling).
        """
        page = self.page
        if page is None:
            return False
        try:
            # Credential prompt present? Never touch it.
            for sel in ("input[type=password]", "#idTxtBx_SAOTCC_OTC",
                        "input[name=otc]"):
                el = page.locator(sel).first
                if await el.count() and await el.is_visible():
                    await self._emit_action(
                        "login.credential_prompt", selector=sel)
                    return False
            # Account picker: prefer the tile matching the owner's email,
            # else click the only tile there is.
            tiles = page.locator("#tilesHolder div[role=button], "
                                 "div.table[role=button]")
            n = await tiles.count()
            if n:
                target = None
                if self.owner_email:
                    for i in range(n):
                        t = tiles.nth(i)
                        txt = (await t.inner_text()) or ""
                        if self.owner_email.lower() in txt.lower():
                            target = t
                            break
                if target is None and n == 1:
                    target = tiles.first
                if target is not None and await target.is_visible():
                    await target.click()
                    await self._emit_action("login.account_tile_clicked",
                                            tiles=n)
                    return True
            # KMSI ("Stay signed in?") / generic primary submit — only when
            # no credential field is on the page (checked above).
            for sel in ("#idSIButton9", "#acceptButton",
                        "input[type=submit][value=Yes]",
                        "input[type=submit][value=Continue]"):
                el = page.locator(sel).first
                if await el.count() and await el.is_visible():
                    await el.click()
                    await self._emit_action("login.interstitial_clicked",
                                            selector=sel)
                    return True
        except Exception as e:
            await self._emit_action("login.interstitial_error",
                                    error=str(e)[:200])
        return False

    async def _go_fullscreen(self) -> None:
        """Hide Chromium's internal chrome (tab strip, URL bar, menu).

        `--kiosk` in LAUNCH_ARGS is the right intent but Playwright's
        automation channel often suppresses it. `page.keyboard.press("F11")`
        sends the keystroke to the page, not the browser process, so it
        doesn't toggle browser-level fullscreen.

        The reliable path is CDP's `Browser.setWindowBounds` with
        windowState=fullscreen — it changes the actual OS window state.
        """
        if self.page is None or self.context is None:
            return
        try:
            cdp = await self.context.new_cdp_session(self.page)
            target = await cdp.send("Browser.getWindowForTarget")
            window_id = target["windowId"]
            await cdp.send(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": "fullscreen"}},
            )
            await self._emit_action("fullscreen.cdp", window_id=window_id)
        except Exception as e:
            await self._emit_action("fullscreen.cdp_failed", error=str(e)[:200])
            # Fallback: synthesize a real F11 keypress at the X server level
            # via xdotool. This reaches Chromium's main process (unlike
            # Playwright's keyboard.press which only reaches the page).
            try:
                proc = await asyncio.create_subprocess_exec(
                    "xdotool", "search", "--class", "chrome",
                    stdout=asyncio.subprocess.PIPE,
                    env={"DISPLAY": self.display or ":0"},
                )
                out, _ = await proc.communicate()
                ids = out.decode().strip().splitlines()
                if ids:
                    await asyncio.create_subprocess_exec(
                        "xdotool", "key", "--window", ids[0], "F11",
                        env={"DISPLAY": self.display or ":0"},
                    )
                    await self._emit_action("fullscreen.xdotool", window=ids[0])
            except Exception as ee:
                await self._emit_action(
                    "fullscreen.xdotool_failed", error=str(ee)[:200],
                )

    async def fill_prejoin(self) -> None:
        assert self.page is not None
        page = self.page
        await self._emit_action("fill_prejoin.enter")

        await self._dismiss_overlays()
        await self._click_rejoin_if_present()

        # The Join button renders on BOTH the signed-in and the guest prejoin,
        # so it's the reliable "prejoin is interactive" signal. Wait for it,
        # then decide which flow we're on from whether the display-name field
        # is present — a signed-in join skips that field, so we no longer burn
        # a fixed 8s polling for a field that never comes.
        join_btn = await _first_visible(page, SEL_JOIN_NOW, timeout=15_000)
        # Probe the name field briefly: on the guest prejoin it renders in the
        # same React commit as the Join button, so a short probe suffices. If
        # the Join button never showed, fall back to the longer probe so a slow
        # prejoin still gets a fair chance before we error.
        name_input = await _first_visible(
            page, SEL_NAME_INPUT, timeout=1_000 if join_btn is not None else 8_000
        )

        # Identity join but Teams shows the guest name field -> the /meet/
        # launcher dropped the sign-in (cold cookies). Warm the signed-in app
        # up ONCE and reload, then re-detect. Only escalate to a human if the
        # guest prejoin survives the warmup. Guest joins are removed, so we
        # must NEVER fill the name and join anonymously.
        if name_input is not None and not self.anonymous and not self._warmup_done:
            await self._emit_action("prejoin.guest_detected_warming_up")
            await self._identity_warmup()
            await self._reload_join_url()
            await self._dismiss_overlays()
            await self._click_rejoin_if_present()
            join_btn = await _first_visible(page, SEL_JOIN_NOW, timeout=15_000)
            name_input = await _first_visible(page, SEL_NAME_INPUT, timeout=1_000)

        if name_input is not None and not self.anonymous:
            # Guest prejoin persisted even after the warmup — silent SSO
            # refused. Hand off so the human can fix the login in the live view.
            await self._save_debug_screenshot("guest_prejoin_on_identity.png")
            raise SupervisedHandoff(
                "identity-dropped-to-guest",
                "Teams presented the anonymous guest prejoin even though a "
                "signed-in join was requested. Complete the sign-in in the "
                "live view, then resume.",
            )

        if name_input is None:
            # No name field. Fine when the Join button is present (signed-in,
            # or a guest prejoin that pre-filled the name); otherwise the
            # prejoin never rendered.
            if join_btn is None:
                await self._save_debug_screenshot("no_name_input.png")
                raise SupervisedHandoff(
                    "prejoin-name-input-not-found",
                    "Could not find the display-name field or Join button on "
                    "the prejoin screen.",
                )
            await self._emit("teams.prejoin.signed_in_no_name_field")
        else:
            # Anonymous guest join with a name field — fill it.
            await self._emit_action("prejoin.fill_name", name=self.display_name)
            await name_input.fill(self.display_name)
            await self._emit("teams.prejoin.name_filled", name=self.display_name)

        # Best-effort: ensure mic+cam are off before joining.
        for selectors, kind in ((SEL_MIC_TOGGLE, "mic"), (SEL_CAM_TOGGLE, "cam")):
            toggle = await _first_visible(page, selectors)
            if toggle is None:
                continue
            try:
                aria = (await toggle.get_attribute("aria-checked")) or ""
                pressed = (await toggle.get_attribute("aria-pressed")) or ""
                # Teams renders "on" with aria-checked=true / aria-pressed=true.
                if "true" in (aria, pressed):
                    await toggle.click(timeout=2000)
                    await self._emit("teams.prejoin.muted", device=kind)
            except Exception:
                continue

        await self._dismiss_overlays()

        join_btn = await _first_visible(page, SEL_JOIN_NOW, timeout=15_000)
        if join_btn is None:
            await self._save_debug_screenshot("no_join_button.png")
            raise SupervisedHandoff(
                "prejoin-join-button-not-found",
                "Could not find the 'Join now' button on the prejoin screen.",
            )
        await self._click_with_overlay_retries(join_btn, "prejoin-join")
        await self._emit("teams.prejoin.join_clicked")

    async def _dismiss_overlays(self) -> None:
        """Click any dialogs/banners that commonly sit on top of the prejoin UI."""
        if self.page is None:
            return
        await self._emit_action("dismiss_overlays.scan")
        for sel in SEL_DISMISS_OVERLAY:
            try:
                loc = self.page.locator(sel).first
                if not await loc.count():
                    continue
                if not await loc.is_visible():
                    continue
                try:
                    await self._emit_action("dismiss_overlays.click", selector=sel)
                    await loc.click(timeout=2_000)
                    await self._emit("teams.overlay.dismissed", selector=sel)
                    await self._emit_action(
                        "dismiss_overlays.clicked", selector=sel, result="ok"
                    )
                    await asyncio.sleep(0.5)
                except Exception as e:
                    await self._emit_action(
                        "dismiss_overlays.clicked",
                        selector=sel,
                        result="fail",
                        error=str(e)[:200],
                    )
                    continue
            except Exception:
                continue

    async def _click_rejoin_if_present(self) -> bool:
        """If Teams shows a 'Rejoin' landing (post-meeting page), click it.

        Returns True if we clicked Rejoin, in which case the page will navigate
        to the prejoin name-input screen.
        """
        if self.page is None:
            return False
        await self._emit_action("rejoin.scan")
        for sel in SEL_REJOIN:
            try:
                loc = self.page.locator(sel).first
                if not await loc.count():
                    continue
                if not await loc.is_visible():
                    continue
                try:
                    await self._emit_action("rejoin.click", selector=sel)
                    await loc.click(timeout=3_000)
                    await self._emit("teams.rejoin_clicked")
                    await self._emit_action("rejoin.clicked", selector=sel, result="ok")
                    # Give the prejoin screen a moment to render.
                    await asyncio.sleep(2)
                    return True
                except Exception as e:
                    await self._emit_action(
                        "rejoin.clicked", selector=sel, result="fail",
                        error=str(e)[:200],
                    )
                    continue
            except Exception:
                continue
        return False

    async def _click_with_overlay_retries(self, locator, label: str) -> None:
        """Click that survives ui-dialog__overlay intercepts.

        Strategy:
          1. normal click (10s),
          2. dismiss overlays, retry normal click (10s),
          3. force-click (bypass actionability),
          4. raise SupervisedHandoff for a human takeover.
        """
        await self._emit_action("click.attempt", label=label, attempt=1, mode="normal")
        try:
            await locator.click(timeout=10_000)
            await self._emit_action("click.attempt", label=label, attempt=1, result="ok")
            return
        except PWTimeout as e:
            await self._emit("teams.click.intercepted", label=label, attempt=1)
            await self._emit_action(
                "click.attempt", label=label, attempt=1, result="intercepted",
                error=str(e)[:200],
            )
            await self._dismiss_overlays()

        await self._emit_action("click.attempt", label=label, attempt=2, mode="normal")
        try:
            await locator.click(timeout=10_000)
            await self._emit_action("click.attempt", label=label, attempt=2, result="ok")
            return
        except PWTimeout:
            await self._emit("teams.click.intercepted", label=label, attempt=2)
            await self._emit_action(
                "click.attempt", label=label, attempt=2, result="intercepted"
            )

        await self._emit_action("click.attempt", label=label, attempt=3, mode="force")
        try:
            await locator.click(force=True, timeout=5_000)
            await self._emit("teams.click.forced", label=label)
            await self._emit_action(
                "click.attempt", label=label, attempt=3, result="forced"
            )
            return
        except Exception as e:
            await self._emit_action(
                "click.attempt", label=label, attempt=3, result="fail",
                error=str(e)[:200],
            )
            await self._save_debug_screenshot(f"{label}_blocked.png")
            raise SupervisedHandoff(
                "click-blocked-by-overlay",
                f"Could not click '{label}' — an overlay/dialog is intercepting "
                f"pointer events. Take over via noVNC, dismiss the dialog, then "
                f"click 'I've handled it'. ({e.__class__.__name__})",
            )

    async def dump_roster_diagnostics(self, label: str) -> None:
        """One-shot DOM dump used to recalibrate roster selectors.

        Saves into `debug_dir`:
          * `roster_<label>.png`   — full-page screenshot
          * `roster_<label>.html`  — full page HTML
          * `roster_<label>.json`  — counts per candidate selector + the
                                     attributes of every node that looks
                                     participant-ish (data-tid containing
                                     'roster' or 'participant')
        """
        if self.page is None or self.debug_dir is None:
            return
        import json as _json

        self.debug_dir.mkdir(parents=True, exist_ok=True)
        png = self.debug_dir / f"roster_{label}.png"
        html = self.debug_dir / f"roster_{label}.html"
        meta = self.debug_dir / f"roster_{label}.json"

        try:
            await self.page.screenshot(path=str(png), full_page=True)
        except Exception:
            pass
        try:
            html.write_text(await self.page.content(), encoding="utf-8")
        except Exception:
            pass

        diag: dict = {"candidate_counts": {}, "participant_nodes": []}
        for sel in SEL_ROSTER_ITEM:
            try:
                diag["candidate_counts"][sel] = await self.page.locator(sel).count()
            except Exception:
                diag["candidate_counts"][sel] = "error"
        try:
            diag["badge_count"] = await self._read_roster_badge_count()
        except Exception:
            diag["badge_count"] = "error"

        # Generic scan: anything whose data-tid mentions participant/roster.
        try:
            nodes = await self.page.evaluate(
                """() => {
                  const sel = '[data-tid]';
                  return Array.from(document.querySelectorAll(sel))
                    .filter(el => /participant|roster|attendee|member/i.test(el.getAttribute('data-tid') || ''))
                    .slice(0, 50)
                    .map(el => ({
                      tag: el.tagName.toLowerCase(),
                      'data-tid': el.getAttribute('data-tid'),
                      'aria-label': el.getAttribute('aria-label'),
                      role: el.getAttribute('role'),
                      class: (el.className || '').toString().slice(0, 120),
                      text: (el.innerText || '').trim().slice(0, 80),
                    }));
                }"""
            )
            diag["participant_nodes"] = nodes
        except Exception as e:
            diag["participant_nodes_error"] = str(e)

        try:
            meta.write_text(_json.dumps(diag, ensure_ascii=False, indent=2))
        except Exception:
            pass

        await self._emit("teams.roster.diagnostic_saved", label=label, dir=str(self.debug_dir))

    async def _save_debug_screenshot(self, filename: str) -> Optional[Path]:
        if self.page is None or self.debug_dir is None:
            return None
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            path = self.debug_dir / filename
            await self.page.screenshot(path=str(path), full_page=True)
            await self._emit("teams.screenshot", path=str(path))
            # Save the matching HTML next to the PNG so prejoin failures can be
            # diagnosed offline (a PNG alone doesn't tell us which selectors
            # would have matched).
            try:
                html_path = path.with_suffix(".html")
                html_path.write_text(await self.page.content(), encoding="utf-8")
            except Exception:
                pass
            try:
                url_path = path.with_suffix(".url.txt")
                url_path.write_text(self.page.url, encoding="utf-8")
            except Exception:
                pass
            return path
        except Exception:
            return None

    async def wait_until_admitted(
        self,
        *,
        join_timeout: float,
        on_lobby: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        """Wait until we're inside the meeting; raise SupervisedHandoff on lobby timeout."""
        assert self.page is not None
        page = self.page

        deadline = asyncio.get_event_loop().time() + join_timeout
        in_lobby_announced = False
        ticks = 0

        while True:
            if self._should_stop():
                raise MeetingEnded("stop requested while waiting for admission")
            people = await _first_visible(page, SEL_PEOPLE_TOGGLE)
            if people is not None:
                await self._emit("teams.admitted")
                return

            removed = await _first_visible(page, SEL_REMOVED_BANNER)
            if removed is not None:
                raise MeetingEnded("Removed or meeting ended before admit")

            # Teams may pop a confirmation dialog AFTER the join click
            # (e.g. "Continue without audio or video?"). Sweep overlays every
            # tick so the bot can keep advancing on its own.
            await self._dismiss_overlays()

            lobby = await _first_visible(page, SEL_LOBBY_BANNER)
            if lobby is not None and not in_lobby_announced:
                in_lobby_announced = True
                await self._emit("teams.lobby")
                if on_lobby is not None:
                    await on_lobby()

            if asyncio.get_event_loop().time() >= deadline:
                await self._save_debug_screenshot("admit_timeout.png")
                raise SupervisedHandoff(
                    "join-timeout",
                    "Timed out waiting to be admitted. May be stuck in the lobby, "
                    "facing a sign-in challenge, or behind a Teams dialog the "
                    "bot doesn't recognise.",
                )
            ticks += 1
            await asyncio.sleep(2)

    async def open_roster(self) -> None:
        assert self.page is not None
        await self.toggle_people_pane_no_css()
        await self._hide_side_panel_visually()

    async def toggle_people_pane_no_css(self) -> None:
        """Click the People pane toggle without applying any CSS hide.

        Split out so the layout-debug A/B dumps can capture the panel-open
        state with native Teams styling intact (no injected overrides).
        """
        assert self.page is not None
        toggle = await _first_visible(self.page, SEL_PEOPLE_TOGGLE)
        if toggle is None:
            return
        # If the panel is already open, aria-pressed=true; clicking would close it.
        pressed = (await toggle.get_attribute("aria-pressed")) or ""
        if "true" not in pressed:
            try:
                await toggle.click()
            except Exception:
                pass

    # Hide whatever side panel is currently docked (People, Chat, info, …)
    # AND the parent column that holds it so the meeting tiles reclaim the
    # freed horizontal space. `display: none` removes the element from layout
    # but leaves it in the DOM tree — Playwright's `locator.count()` and
    # attribute reads still work on display:none nodes, so participant
    # scraping is unaffected.
    # Hide the side panel WITHOUT using `display: none`. Teams web v2
    # virtualises the roster — with `display: none` on any ancestor of the
    # panel, React unmounts every `participantsInCall-*` row and we lose
    # name scraping. We move the panel offscreen with `position: fixed`
    # instead: it's still in the DOM tree, still rendered (children mount,
    # data-tids stay queryable), but it's painted at -10000px so it doesn't
    # appear in the recording, and `position: fixed` takes it out of the
    # meeting-view layout flow so the tiles can expand.
    # Derived from a three-way HTML diff of panel_closed vs panel_open_no_css:
    # Teams sizes the right column of `experience-layout` using two CSS
    # custom properties on `app-layout-area--end`:
    #   --slot-width: 60rem
    #   --other-inline-slots-with-minimal-main: calc(68px + 360px)
    # When the pane is closed, Teams ALSO sets inline `display: none;
    # visibility: hidden` on the same element — which unmounts the
    # virtualised roster underneath.
    #
    # Goal: keep the panel mounted (so `participantsInCall-*` rows stay in
    # the DOM for scraping) but collapse the layout slot to zero width so
    # the meeting tiles fill the viewport. Strategy:
    #   * Position `calling-right-side-panel` offscreen — it still renders,
    #     so React keeps its children mounted, but no pixels reach ffmpeg.
    #   * Override the layout-area's slot-width custom properties + width
    #     to 0 so the grid track collapses. NO `display: none` anywhere.
    _SIDE_PANEL_HIDE_CSS = """
      [data-tid="app-layout-area--end"] {
        --slot-width: 0 !important;
        --other-inline-slots-with-minimal-main: 0 !important;
        width: 0 !important;
        min-width: 0 !important;
        max-width: 0 !important;
        overflow: hidden !important;
      }
      /* Top bar (with search field) + left rail (with app icons +
         hamburger). These don't contain any virtualised content we need
         to scrape, so `display: none` is safe — the experience-layout
         grid automatically collapses empty rows/columns. */
      [data-tid="app-layout-area--title-bar"],
      [data-tid="app-layout-area--header"],
      [data-tid="app-layout-area--nav"],
      [data-tid="app-layout-area--mid-nav"],
      [data-tid="app-layout-area--sub-nav"],
      [data-tid="app-bar-wrapper"] {
        display: none !important;
      }
      [data-tid="calling-right-side-panel"],
      [data-tid="meetup-app-right-pane"],
      [data-tid="meeting-side-pane"],
      [data-tid="calling-side-pane"],
      [data-tid="right-rail-pane"],
      [data-tid="right-rail"],
      [aria-label*="Side pane" i],
      aside[role="complementary"] {
        position: fixed !important;
        left: -10000px !important;
        top: 0 !important;
        width: 360px !important;
        height: 100vh !important;
        opacity: 0 !important;
        pointer-events: none !important;
        z-index: -1 !important;
      }
    """

    async def _hide_side_panel_visually(self) -> None:
        assert self.page is not None
        try:
            await self.page.add_style_tag(content=self._SIDE_PANEL_HIDE_CSS)
            await self._emit_action("side_panel.hidden_via_css")
        except Exception as e:
            await self._emit_action(
                "side_panel.hide_failed", error=str(e)[:200],
            )

    async def snapshot_participants(self) -> ParticipantSnapshot:
        assert self.page is not None
        page = self.page
        names: list[str] = []
        used_selector: str | None = None
        for sel in SEL_ROSTER_ITEM:
            items = page.locator(sel)
            count = await items.count()
            if count == 0:
                continue
            used_selector = sel
            for i in range(count):
                node = items.nth(i)
                name = ""
                # Preferred: data-tid encodes the exact display name. Try each
                # known prefix (Teams uses different ones per meeting type).
                try:
                    tid = await node.get_attribute("data-tid")
                    if tid:
                        for prefix in ROSTER_TID_PREFIXES:
                            if tid.startswith(prefix):
                                name = tid[len(prefix):].strip()
                                break
                except Exception:
                    pass
                # Fallbacks: inner_text first line, then aria-label first segment.
                if not name:
                    try:
                        text = await node.inner_text()
                        name = (text or "").strip().splitlines()[0].strip()
                    except Exception:
                        pass
                if not name:
                    try:
                        aria = await node.get_attribute("aria-label")
                        # aria-label form: "Name, External, Has context menu, ..."
                        name = (aria or "").split(",")[0].strip()
                    except Exception:
                        pass
                if name:
                    names.append(name)
            break
        seen: set[str] = set()
        unique = []
        for n in names:
            if n not in seen:
                seen.add(n)
                unique.append(n)
        badge = await self._read_roster_badge_count()
        wrapper = await self._read_roster_wrapper_count()
        candidates = [v for v in (badge, wrapper, len(unique) if unique else None) if v is not None]
        total: int | None = max(candidates) if candidates else None
        return ParticipantSnapshot(
            names=unique,
            count=total,
            badge_count=badge,
            wrapper_count=wrapper,
            roster_selector=used_selector,
        )

    async def _read_roster_wrapper_count(self) -> int | None:
        """Parse the count out of headers like 'In this meeting (7)'."""
        assert self.page is not None
        for sel in SEL_ROSTER_WRAPPER:
            try:
                loc = self.page.locator(sel)
                if await loc.count() == 0:
                    continue
                text = (await loc.first.inner_text()).strip()
                m = ROSTER_HEADER_COUNT_RE.search(text)
                if m:
                    return int(m.group(1))
            except Exception:
                continue
        return None

    async def _read_roster_badge_count(self) -> int | None:
        assert self.page is not None
        for sel in SEL_ROSTER_COUNT_BADGE:
            try:
                loc = self.page.locator(sel)
                if await loc.count() == 0:
                    continue
                text = (await loc.first.inner_text()).strip()
                m = re.search(r"\d+", text)
                if m:
                    return int(m.group(0))
                aria = await loc.first.get_attribute("aria-label")
                if aria:
                    m = re.search(r"\d+", aria)
                    if m:
                        return int(m.group(0))
            except Exception:
                continue
        return None

    async def snapshot_active_speakers(self) -> set[str]:
        """Return the set of participant names Teams currently marks as speaking.

        Reads the roster rows (whose data-tid carries the exact name) and looks
        for the active-speaker signal on the row or any descendant. Roster-based
        detection is used because the row name is unambiguous and the row stays
        in the DOM even with the People pane hidden offscreen; Teams lights the
        roster voice indicator in lockstep with the stage tile's frame.

        Best-effort and defensive: never raises, returns an empty set on any
        error or when nothing matches. The signal tokens are calibration
        -pending (see SPEAKER_SIGNAL_TOKENS).
        """
        if self.page is None:
            return set()
        try:
            names = await self.page.evaluate(
                """(cfg) => {
                  const { prefixes, tokens } = cfg;
                  const speaking = new Set();
                  const hasSignal = (el) => {
                    const nodes = [el, ...el.querySelectorAll('*')];
                    for (const n of nodes) {
                      if (!n.getAttribute) continue;
                      if ((n.getAttribute('data-is-speaking') || '').toLowerCase() === 'true') return true;
                      const hay = (
                        (n.getAttribute('data-tid') || '') + ' ' +
                        (n.getAttribute('class') || '') + ' ' +
                        (n.getAttribute('aria-label') || '')
                      ).toLowerCase();
                      for (const t of tokens) { if (hay.includes(t)) return true; }
                    }
                    return false;
                  };
                  for (const el of document.querySelectorAll('[data-tid]')) {
                    const tid = el.getAttribute('data-tid') || '';
                    let name = null;
                    for (const p of prefixes) {
                      if (tid.startsWith(p)) { name = tid.slice(p.length).trim(); break; }
                    }
                    if (name && hasSignal(el)) speaking.add(name);
                  }
                  return Array.from(speaking);
                }""",
                {"prefixes": list(ROSTER_TID_PREFIXES), "tokens": list(SPEAKER_SIGNAL_TOKENS)},
            )
            return {n for n in (names or []) if n}
        except Exception:
            return set()

    async def dump_speaker_diagnostics(self, label: str) -> None:
        """Dump the raw roster/tile DOM so the speaking selector can be pinned.

        Saves speaker_<label>.json (per-candidate outerHTML + attributes) and
        speaker_<label>.png. Running this across a real multi-party meeting —
        with someone talking — lets us diff a speaking row against a silent one
        and lock SPEAKER_SIGNAL_TOKENS to the token that actually toggles.
        """
        if self.page is None or self.debug_dir is None:
            return
        import json as _json

        self.debug_dir.mkdir(parents=True, exist_ok=True)
        png = self.debug_dir / f"speaker_{label}.png"
        meta = self.debug_dir / f"speaker_{label}.json"
        try:
            await self.page.screenshot(path=str(png))
        except Exception:
            pass
        try:
            data = await self.page.evaluate(
                """(prefixes) => {
                  const trim = (s) => (s || '').slice(0, 4000);
                  const rows = [];
                  for (const el of document.querySelectorAll('[data-tid]')) {
                    const tid = el.getAttribute('data-tid') || '';
                    if (!prefixes.some(p => tid.startsWith(p))) continue;
                    rows.push({ 'data-tid': tid, html: trim(el.outerHTML) });
                  }
                  // Stage tiles — candidate video-tile containers for the frame.
                  const tiles = [];
                  const tileSel = '[data-tid*="tile" i],[data-tid*="stream" i],[data-tid*="stage" i],[data-tid*="render" i]';
                  for (const el of document.querySelectorAll(tileSel)) {
                    tiles.push({
                      'data-tid': el.getAttribute('data-tid'),
                      'aria-label': el.getAttribute('aria-label'),
                      class: (el.className || '').toString().slice(0, 200),
                      html: trim(el.outerHTML),
                    });
                  }
                  return { rows, tiles: tiles.slice(0, 30) };
                }""",
                list(ROSTER_TID_PREFIXES),
            )
            meta.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            await self._emit_action("speaker.diag_failed", error=str(e)[:200])
        await self._emit("teams.speaker.diagnostic_saved", label=label)

    async def meeting_title(self) -> str | None:
        assert self.page is not None
        try:
            title = await self.page.title()
        except Exception:
            return None
        # Teams uses "Meeting | Microsoft Teams" — strip the suffix.
        m = re.match(r"^(.*?)\s*[|]\s*Microsoft Teams\s*$", title or "")
        return (m.group(1) if m else title).strip() or None

    async def leave(self) -> None:
        if self.page is None:
            return
        await self._emit_action("leave.enter")
        btn = await _first_visible(self.page, SEL_LEAVE)
        if btn is not None:
            try:
                await self._emit_action("leave.click")
                await btn.click(timeout=5_000)
                await self._emit_action("leave.click", result="ok")
            except Exception as e:
                await self._emit_action("leave.click", result="fail", error=str(e)[:200])
        else:
            await self._emit_action("leave.click", result="no_button")
        await self._emit("teams.left")

    async def detect_meeting_end(self) -> bool:
        assert self.page is not None
        ended = await _first_visible(self.page, SEL_REMOVED_BANNER)
        return ended is not None

    async def dismiss_call_banners(self) -> int:
        """Dismiss in-meeting ufd_* notification banners (No Microphone, etc.).

        Returns the number dismissed. Safe to call every poll: each click hits
        a benign close button and it's a no-op when none are showing. Banners
        are removed from the DOM on click, which reshuffles the list, so we
        re-query and take the first visible one each pass rather than indexing
        a stale locator — bounded so a never-closing banner can't spin.
        """
        if self.page is None:
            return 0
        dismissed = 0
        for _ in range(6):
            btn = await _first_visible(self.page, SEL_DISMISS_CALL_BANNER)
            if btn is None:
                break
            try:
                tid = await btn.get_attribute("data-tid")
                await btn.click(timeout=2_000)
                dismissed += 1
                await self._emit("teams.call_banner.dismissed", selector=tid)
            except Exception:
                break
        return dismissed

    async def open_chat(self) -> bool:
        assert self.page is not None
        toggle = await _first_visible(self.page, SEL_CHAT_TOGGLE)
        if toggle is None:
            return False
        pressed = (await toggle.get_attribute("aria-pressed")) or ""
        if "true" not in pressed:
            try:
                await toggle.click(timeout=3_000)
                # Give the pane a moment to render.
                await asyncio.sleep(1.0)
            except Exception:
                return False
        await self._hide_chat_pane_visually()
        return True

    # CSS injected once per session. Positions the docked chat pane off the
    # visible viewport so it doesn't show up in the x11grab recording, while
    # leaving the DOM intact for scraping (snapshot_chat reads from the same
    # nodes regardless of paint). Also expands the meeting view to reclaim
    # the freed horizontal space.
    _CHAT_HIDE_CSS = """
      [data-tid="chat-pane-item"],
      [data-tid="message-pane-layout"],
      [data-tid="message-pane-list-viewport"] {
        position: fixed !important;
        left: -10000px !important;
        top: 0 !important;
        width: 360px !important;
        height: 100vh !important;
        pointer-events: none !important;
      }
    """

    async def _hide_chat_pane_visually(self) -> None:
        assert self.page is not None
        try:
            await self.page.add_style_tag(content=self._CHAT_HIDE_CSS)
            await self._emit_action("chat_pane.hidden_via_css")
        except Exception as e:
            await self._emit_action(
                "chat_pane.hide_failed", error=str(e)[:200],
            )

    async def snapshot_chat(self) -> list[ChatMessage]:
        assert self.page is not None
        page = self.page
        messages: list[ChatMessage] = []
        for sel in SEL_CHAT_MESSAGE:
            items = page.locator(sel)
            count = await items.count()
            if count == 0:
                continue
            for i in range(count):
                node = items.nth(i)
                author = ""
                body = ""
                for asel in SEL_CHAT_AUTHOR:
                    a = node.locator(asel).first
                    if await a.count():
                        try:
                            author = (await a.inner_text(timeout=500)).strip()
                            break
                        except Exception:
                            pass
                for bsel in SEL_CHAT_BODY:
                    b = node.locator(bsel).first
                    if await b.count():
                        try:
                            body = (await b.inner_text(timeout=500)).strip()
                            break
                        except Exception:
                            pass
                if not body:
                    try:
                        body = (await node.inner_text(timeout=500)).strip()
                    except Exception:
                        continue
                if body:
                    messages.append(ChatMessage.make(author=author or "?", body=body))
            break
        return messages


class SupervisedHandoff(Exception):
    """Raised when the bot can't make progress and needs a human via noVNC."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MeetingEnded(Exception):
    pass
