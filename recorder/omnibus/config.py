from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OMNIBUS_",
        env_file="/etc/omnibus/recorder.env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "ZW-Omnibus"
    app_description: str = "Meeting Recorder"
    display_name: str = "ZW-Omnibus Recorder"

    # --- storage ---------------------------------------------------------
    # Recordings are written to fast local disk first (ffmpeg-to-CIFS is
    # corruption-prone), then moved to the share's TEMP inbox on session end.
    local_rec_dir: Path = Field(default=Path("/var/lib/omnibus/rec"))
    share_root: Path = Field(default=Path("/mnt/omnibus"))
    temp_dirname: str = "TEMP"
    # Subfolder created inside a project folder when a recording is assigned.
    project_meetings_dirname: str = "Meetings"

    # --- Supabase (korfu) --------------------------------------------------
    supabase_url: str = "https://korfu.zw-lab.net"
    supabase_schema: str = "zw_omnibus"
    # service_role key — full DB access, NEVER exposed to the browser.
    supabase_service_key: str = ""
    # JWT secret used to verify user tokens sent by the SPA.
    supabase_jwt_secret: str = ""

    # --- multiuser session slots -------------------------------------------
    # Each concurrent session gets its own Xvfb display, pulse sink and
    # VNC/noVNC port pair, allocated from these bases by slot index.
    max_concurrent_sessions: int = 2
    display_number_base: int = 100
    vnc_port_base: int = 5900
    novnc_port_base: int = 6080

    display_width: int = 1920
    display_height: int = 1080
    display_depth: int = 24

    video_framerate: int = 25
    video_crf: int = 23
    video_preset: str = "veryfast"
    audio_bitrate: str = "160k"

    # --- Teams identity profiles --------------------------------------------
    # Per-user persistent Chromium profiles: auth_profiles_dir/<user_id>/.
    auth_profiles_dir: Path = Field(default=Path("/var/lib/omnibus/profiles"))
    # Sign-in sessions use a smaller display so the noVNC view scales up
    # larger/readable; recordings keep full 1920x1080.
    auth_display_width: int = 1280
    auth_display_height: int = 800
    # Abandoned sign-in sessions are torn down after this long.
    auth_session_timeout_seconds: int = 900

    solo_grace_seconds: int = 60
    participant_poll_seconds: int = 5
    chat_poll_seconds: int = 10
    join_timeout_seconds: int = 90
    debug_dump_seconds: int = 0  # noisy diagnostics off by default in prod

    # --- active-speaker tracking -----------------------------------------
    speaker_tracking_enabled: bool = True
    # How often to sample the Teams "speaking" indicator. Speaking toggles
    # sub-second; ~1s sampling gives a faithful timeline without hammering the
    # DOM. Segments shorter than one interval may be missed.
    speaker_poll_seconds: float = 1.0
    # When >0, the speaker sampler also dumps the raw tile/roster DOM every N
    # seconds (dump_speaker_diagnostics) so the "speaking" selector can be
    # calibrated against a real multi-party meeting. Off in prod.
    speaker_debug_dump_seconds: int = 0

    # --- AI summarization ----------------------------------------------------
    summary_enabled: bool = True
    summary_auto_on_end: bool = True
    summary_claude_binary: str = "claude"
    summary_model: str = "haiku"
    summary_max_budget_usd: float = 0.50
    summary_timeout_seconds: int = 180
    summary_max_input_chars: int = 60000

    vnc_always_on: bool = True

    web_host: str = "127.0.0.1"
    web_port: int = 8088

    novnc_dir: Path = Field(default=Path("/opt/omnibus/vendor/noVNC"))

    # --- Calendar (ICS subscriptions) -----------------------------------
    calendar_enabled: bool = True
    calendar_lookahead_minutes: int = 1440
    calendar_join_lead_seconds: int = 60
    calendar_poll_seconds: int = 60


settings = Settings()
