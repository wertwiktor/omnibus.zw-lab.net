from __future__ import annotations

import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


@dataclass
class VncHandle:
    vnc_port: int
    novnc_port: int
    novnc_url_path: str
    _x11vnc: subprocess.Popen
    _websockify: subprocess.Popen

    def stop(self) -> None:
        for proc in (self._websockify, self._x11vnc):
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()


def start_vnc(
    *,
    display: str,
    vnc_port: int,
    novnc_port: int,
    novnc_dir: Path,
    env: dict[str, str],
) -> VncHandle:
    for cmd in ("x11vnc", "websockify"):
        if shutil.which(cmd) is None:
            raise RuntimeError(
                f"{cmd} not on PATH. Run scripts/install-system-deps.sh."
            )
    if not (novnc_dir / "vnc.html").exists():
        raise RuntimeError(
            f"noVNC bundle missing at {novnc_dir}. "
            "Run scripts/install-system-deps.sh to fetch it."
        )

    # -shared so the page can take over without booting an existing viewer.
    # -forever so it doesn't die after the first disconnect.
    # -nopw is fine because we only bind to localhost via websockify -> public.
    x11vnc = subprocess.Popen(
        [
            "x11vnc",
            "-display", display,
            "-localhost",
            "-rfbport", str(vnc_port),
            "-nopw",
            "-shared",
            "-forever",
            "-quiet",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )

    # websockify bridges TCP VNC -> WebSocket and serves the noVNC static files.
    websockify = subprocess.Popen(
        [
            "websockify",
            "--web", str(novnc_dir),
            str(novnc_port),
            f"localhost:{vnc_port}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    log.info("vnc.started", vnc_port=vnc_port, novnc_port=novnc_port)

    return VncHandle(
        vnc_port=vnc_port,
        novnc_port=novnc_port,
        novnc_url_path=f"/vnc.html?autoconnect=true&resize=scale&path=websockify",
        _x11vnc=x11vnc,
        _websockify=websockify,
    )
