from __future__ import annotations

import json
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

import structlog
from playwright.async_api import BrowserContext, Page, async_playwright

log = structlog.get_logger(__name__)

# Base Chromium flags shared by auth + meeting sessions.
#
#   --app=about:blank                   → Open in app-window mode (no tabs,
#                                         no address bar, no bookmarks bar).
#                                         The recording captures only the
#                                         Teams UI, and the operator's noVNC
#                                         view is uncluttered.
#   --use-fake-ui-for-media-stream      → Auto-grant mic/cam (we still mute on
#                                         prejoin).
#   --autoplay-policy=...               → Audio from other participants plays
#                                         without a user gesture.
#   --disable-blink-features=AutomationControlled
#                                       → Hide the "automated test" hint.
#   --disable-features=...              → Kill features that pop modals during
#                                         sign-in (password manager, autofill
#                                         server pings, translate, etc.).
#   --disable-notifications             → Stop site notification prompts.
#   --password-store=basic              → Don't try to bind gnome-keyring /
#                                         kwallet (would prompt on Linux).
#   --no-first-run --no-default-browser-check
#                                       → Skip the first-run welcome.
LAUNCH_ARGS = [
    # Kiosk mode = fullscreen, no tab strip, no URL bar, no menu. Playwright's
    # automation channel ignores --app=URL so we use --kiosk for a guaranteed
    # chromeless window. Combined with --window-size we get a known geometry
    # that matches the Xvfb display the recorder grabs.
    "--kiosk",
    "--window-size=1920,1080",
    "--window-position=0,0",
    "--use-fake-ui-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-blink-features=AutomationControlled",
    "--disable-features="
    "IsolateOrigins,"
    "site-per-process,"
    "PasswordManager,"
    "PasswordManagerOnboarding,"
    "PasswordCheck,"
    "PasswordImport,"
    "AutofillServerCommunication,"
    "AutofillEnableAccountWalletStorage,"
    "Translate,"
    "TranslateUI,"
    "GlobalMediaControls,"
    "MediaRouter,"
    "OptimizationHints",
    "--disable-notifications",
    "--disable-popup-blocking",
    "--disable-prompt-on-repost",
    "--disable-save-password-bubble",
    "--disable-translate",
    "--password-store=basic",
    "--no-sandbox",
    "--no-first-run",
    "--no-default-browser-check",
]

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Preferences seeded into the persistent profile on first launch. Once
# Chromium has rewritten Preferences itself (every clean shutdown), we don't
# touch it again — except to fix `exit_type` after a crash (see below).
SEED_PREFERENCES: dict = {
    "credentials_enable_service": False,
    "credentials_enable_autosignin": False,
    "profile": {
        "password_manager_enabled": False,
        "default_content_setting_values": {
            "notifications": 2,  # 2 = block
        },
    },
    # 5 = never restore. Session restore MUST stay off: with a previous
    # session in the profile, restore_on_startup=1 makes Chromium open a
    # SECOND window at launch — the restored window sits on top on the Xvfb
    # display while Playwright drives the original underneath, so ffmpeg/VNC
    # record a blank white window (hit live 2026-07-09). Sign-in persistence
    # is handled by the cookie snapshot (inject_cookies), not by Chromium.
    "session": {"restore_on_startup": 5},
    "translate": {"enabled": False},
    "browser": {
        "show_home_button": False,
        "has_seen_welcome_page": True,
    },
    "autofill": {
        "credit_card_enabled": False,
        "profile_enabled": False,
    },
}


def _clear_stale_singleton(profile_dir: Path) -> None:
    """Remove Chromium Singleton* leftovers when their owning pid is dead.

    A crashed/killed Chromium leaves SingletonLock (symlink -> <host>-<pid>)
    behind; the next launch then fails with 'profile is already in use'.
    Only removes them when the recorded pid no longer exists.
    """
    lock = profile_dir / "SingletonLock"
    if not lock.is_symlink():
        return
    try:
        pid = int(os.readlink(lock).rsplit("-", 1)[-1])
        alive = (Path("/proc") / str(pid)).exists()
    except (ValueError, OSError):
        alive = False
    if alive:
        return
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (profile_dir / name).unlink(missing_ok=True)
        except OSError:
            pass
    log.info("browser.stale_singleton_cleared", profile=str(profile_dir))


def prepare_profile_dir(profile_dir: Path) -> None:
    """Make sure the persistent profile has sane defaults and won't show the
    'Restore tabs?' recovery prompt after a previous crash."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    _clear_stale_singleton(profile_dir)
    default_dir = profile_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    # Belt and braces vs the second-window bug: even with restore off, drop
    # any recorded previous session so there is nothing to restore.
    for name in ("Sessions", "Session Storage"):
        shutil.rmtree(default_dir / name, ignore_errors=True)
    prefs_file = default_dir / "Preferences"
    if not prefs_file.exists():
        prefs_file.write_text(json.dumps(SEED_PREFERENCES))
        return
    # Patch existing Preferences: clear the crash-recovery exit_type so the
    # next launch starts clean. Chromium rewrites this on every clean exit.
    try:
        prefs = json.loads(prefs_file.read_text())
    except json.JSONDecodeError:
        log.warning("browser.preferences_corrupt; resetting")
        prefs_file.write_text(json.dumps(SEED_PREFERENCES))
        return
    changed = False
    profile = prefs.setdefault("profile", {})
    if profile.get("exit_type") not in (None, "Normal"):
        profile["exit_type"] = "Normal"
        changed = True
    if profile.get("exited_cleanly") is False:
        profile["exited_cleanly"] = True
        changed = True
    # Session restore must stay OFF (see SEED_PREFERENCES) — force it every
    # launch and drop any recorded previous session so Chromium never opens
    # a second "restored" window over the one Playwright drives.
    session = prefs.setdefault("session", {})
    if session.get("restore_on_startup") != 5:
        session["restore_on_startup"] = 5
        changed = True
    if changed:
        try:
            prefs_file.write_text(json.dumps(prefs))
        except OSError:
            pass


@asynccontextmanager
async def teams_browser_context(
    *,
    env: dict[str, str],
    width: int,
    height: int,
    profile_dir: Optional[Path] = None,
    user_agent: Optional[str] = None,
    inject_cookies: Optional[Path] = None,
) -> AsyncIterator[BrowserContext]:
    """Launch Chromium in app-window mode with an optional persistent profile.

    `inject_cookies` points at a JSON snapshot (list of Playwright cookie
    dicts) captured at sign-in time. Injecting them at launch makes the login
    independent of Chromium's on-disk cookie store — Chromium purges session
    cookies (ESTSAUTH) at startup, which silently killed saved sign-ins.
    """
    async with async_playwright() as pw:
        context_kwargs = dict(
            viewport={"width": width, "height": height},
            permissions=["microphone", "camera"],
            locale="en-US",
            user_agent=user_agent or DEFAULT_USER_AGENT,
        )
        if profile_dir is not None:
            prepare_profile_dir(profile_dir)
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=False,
                args=LAUNCH_ARGS,
                env=env,
                **context_kwargs,
            )
            await _inject_cookies(context, inject_cookies)
            try:
                yield context
            finally:
                await context.close()
        else:
            browser = await pw.chromium.launch(
                headless=False, args=LAUNCH_ARGS, env=env,
            )
            context = await browser.new_context(**context_kwargs)
            await _inject_cookies(context, inject_cookies)
            try:
                yield context
            finally:
                await context.close()
                await browser.close()


async def _inject_cookies(context: BrowserContext, path: Optional[Path]) -> None:
    if path is None or not path.exists():
        return
    try:
        cookies = json.loads(path.read_text())
        if cookies:
            await context.add_cookies(cookies)
            log.info("browser.cookies_injected", count=len(cookies), src=str(path))
    except Exception:
        log.exception("browser.cookie_inject_failed", src=str(path))


async def export_cookies(context: BrowserContext, path: Path) -> int:
    """Snapshot the context's live cookies to `path` (atomic write).

    Called right after a confirmed sign-in / successful identity warmup so
    the snapshot always holds fresh Entra tokens.
    """
    cookies = await context.cookies()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cookies))
    tmp.replace(path)
    log.info("browser.cookies_exported", count=len(cookies), dst=str(path))
    return len(cookies)


async def first_app_page(context: BrowserContext, timeout_ms: int = 10_000) -> Page:
    """Return the app-mode window's initial page.

    Chromium with `--app=about:blank` opens a single chromeless window on
    launch. Use *that* page (not new_page()) — opening a fresh page would
    spawn a second window with a regular address bar.
    """
    if context.pages:
        return context.pages[0]
    try:
        return await context.wait_for_event("page", timeout=timeout_ms)
    except Exception:
        # Last-resort fallback if Chromium didn't spawn the app window.
        return await context.new_page()


# Back-compat shim for any older imports.
@asynccontextmanager
async def teams_browser(
    *,
    env: dict[str, str],
    width: int,
    height: int,
    user_agent: Optional[str] = None,
    storage_state: Optional[Path] = None,  # ignored
    profile_dir: Optional[Path] = None,
    inject_cookies: Optional[Path] = None,
):
    async with teams_browser_context(
        env=env, width=width, height=height,
        profile_dir=profile_dir, user_agent=user_agent,
        inject_cookies=inject_cookies,
    ) as context:
        yield None, context
