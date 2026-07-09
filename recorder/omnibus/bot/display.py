from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

import structlog

log = structlog.get_logger(__name__)


@dataclass
class DisplayHandle:
    display: str
    width: int
    height: int
    pulse_sink: str
    pulse_monitor: str
    env: dict[str, str]
    _xvfb: subprocess.Popen
    _pulse_module_ids: list[str]
    _wm: subprocess.Popen | None = None

    def stop(self) -> None:
        for module_id in self._pulse_module_ids:
            try:
                subprocess.run(["pactl", "unload-module", module_id], check=False)
            except FileNotFoundError:
                pass
        if self._wm is not None and self._wm.poll() is None:
            self._wm.send_signal(signal.SIGTERM)
            try:
                self._wm.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._wm.kill()
        if self._xvfb.poll() is None:
            self._xvfb.send_signal(signal.SIGTERM)
            try:
                self._xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._xvfb.kill()


def _require(cmd: str) -> None:
    if shutil.which(cmd) is None:
        raise RuntimeError(
            f"Required executable not found on PATH: {cmd}. "
            "Run scripts/install-system-deps.sh."
        )


def _wait_for_xvfb(display: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        probe = subprocess.run(
            ["xdpyinfo", "-display", display],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if probe.returncode == 0:
            return
        time.sleep(0.1)
    raise RuntimeError(f"Xvfb did not become ready on {display} within {timeout}s")


def _ensure_pulse(env: dict[str, str]) -> None:
    # Start a per-user pulseaudio if one isn't already reachable.
    probe = subprocess.run(
        ["pactl", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env
    )
    if probe.returncode == 0:
        return
    subprocess.run(
        ["pulseaudio", "--start", "--exit-idle-time=-1"],
        check=True,
        env=env,
    )
    deadline = time.time() + 5
    while time.time() < deadline:
        probe = subprocess.run(
            ["pactl", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        if probe.returncode == 0:
            return
        time.sleep(0.1)
    raise RuntimeError("PulseAudio failed to start")


def start_display(
    display_number: int,
    width: int,
    height: int,
    depth: int,
    pulse_sink_name: str,
) -> DisplayHandle:
    for cmd in ("Xvfb", "xdpyinfo", "pactl", "pulseaudio"):
        _require(cmd)

    display = f":{display_number}"
    env = os.environ.copy()
    env["DISPLAY"] = display
    # Route Chromium's audio output into our private sink so ffmpeg can capture it
    # without picking up anything else on the host.
    env["PULSE_SINK"] = pulse_sink_name

    xvfb = subprocess.Popen(
        [
            "Xvfb",
            display,
            "-screen", "0", f"{width}x{height}x{depth}",
            "-nolisten", "tcp",
            "-ac",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_xvfb(display)
    except Exception:
        xvfb.kill()
        raise

    # A window manager is required for --kiosk / --start-fullscreen to take
    # effect. matchbox-window-manager is ~30KB and does exactly what we need:
    # fullscreens the active window, removes WM decorations. Fall back to no
    # WM if matchbox isn't installed — sign-in still works without fullscreen.
    wm: subprocess.Popen | None = None
    if shutil.which("matchbox-window-manager") is not None:
        wm = subprocess.Popen(
            ["matchbox-window-manager", "-use_titlebar", "no"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    _ensure_pulse(env)

    # Clean up sinks left over from previous crashed sessions so we don't end
    # up with `omnibus_sink`, `omnibus_sink.2`, `omnibus_sink.3`, ... — Pulse
    # renames duplicates on collision and Chromium binds to the first one.
    try:
        modules = subprocess.check_output(
            ["pactl", "list", "short", "modules"], env=env, text=True
        )
        for line in modules.splitlines():
            if "module-null-sink" in line and pulse_sink_name in line:
                mod_id = line.split("\t", 1)[0]
                subprocess.run(
                    ["pactl", "unload-module", mod_id],
                    check=False, env=env,
                )
    except subprocess.CalledProcessError:
        pass

    module_ids: list[str] = []
    sink_id = subprocess.check_output(
        [
            "pactl", "load-module", "module-null-sink",
            f"sink_name={pulse_sink_name}",
            f"sink_properties=device.description=ZW-Omnibus-Sink",
        ],
        env=env,
        text=True,
    ).strip()
    module_ids.append(sink_id)

    log.info(
        "display.started",
        display=display,
        size=f"{width}x{height}x{depth}",
        pulse_sink=pulse_sink_name,
    )

    return DisplayHandle(
        display=display,
        width=width,
        height=height,
        pulse_sink=pulse_sink_name,
        pulse_monitor=f"{pulse_sink_name}.monitor",
        env=env,
        _xvfb=xvfb,
        _pulse_module_ids=module_ids,
        _wm=wm,
    )
