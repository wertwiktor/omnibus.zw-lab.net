from __future__ import annotations

import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


@dataclass
class RecorderHandle:
    output_path: Path
    _proc: subprocess.Popen

    def stop(self, timeout: float = 10.0) -> Path:
        if self._proc.poll() is None:
            # ffmpeg writes a proper moov atom only on graceful shutdown — SIGINT/q.
            try:
                self._proc.communicate(input=b"q", timeout=timeout)
            except subprocess.TimeoutExpired:
                self._proc.send_signal(signal.SIGTERM)
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        return self.output_path


def start_recorder(
    *,
    display: str,
    width: int,
    height: int,
    framerate: int,
    pulse_monitor: str,
    output_path: Path,
    crf: int,
    preset: str,
    audio_bitrate: str,
    env: dict[str, str],
) -> RecorderHandle:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-loglevel", "warning",
        "-y",
        # Video: capture the whole Xvfb screen.
        "-f", "x11grab",
        "-framerate", str(framerate),
        "-video_size", f"{width}x{height}",
        "-i", f"{display}.0+0,0",
        # Audio: monitor of the null sink that Chromium plays into.
        "-f", "pulse",
        "-i", pulse_monitor,
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", audio_bitrate,
        "-movflags", "+faststart",
        str(output_path),
    ]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
    )
    log.info("recorder.started", output=str(output_path))
    return RecorderHandle(output_path=output_path, _proc=proc)
