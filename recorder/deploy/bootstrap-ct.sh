#!/bin/bash
# ZW-Omnibus recorder — CT230 deploy script (idempotent, plug-and-play).
set -euo pipefail

echo "== system packages =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  python3 python3-venv python3-pip \
  ffmpeg xvfb xdotool x11vnc websockify x11-utils \
  pulseaudio matchbox-window-manager \
  fonts-liberation fonts-noto-color-emoji \
  cifs-utils \
  curl unzip >/dev/null

echo "== app layout =="
mkdir -p /opt/omnibus /etc/omnibus /var/lib/omnibus/rec /var/lib/omnibus/profiles /opt/omnibus/vendor

echo "== python venv + package =="
if [ ! -x /opt/omnibus/venv/bin/python ]; then
  python3 -m venv /opt/omnibus/venv
fi
/opt/omnibus/venv/bin/pip install -q --upgrade pip
/opt/omnibus/venv/bin/pip install -q -e /opt/omnibus/recorder

echo "== playwright chromium =="
export PLAYWRIGHT_BROWSERS_PATH=/opt/omnibus/pw-browsers
/opt/omnibus/venv/bin/python -m playwright install --with-deps chromium >/dev/null 2>&1 || \
  /opt/omnibus/venv/bin/python -m playwright install chromium

echo "== noVNC bundle =="
if [ ! -f /opt/omnibus/vendor/noVNC/vnc.html ]; then
  curl -fsSL https://github.com/novnc/noVNC/archive/refs/tags/v1.5.0.tar.gz -o /tmp/novnc.tgz
  mkdir -p /opt/omnibus/vendor/noVNC
  tar xzf /tmp/novnc.tgz -C /opt/omnibus/vendor/noVNC --strip-components=1
  rm /tmp/novnc.tgz
fi

echo "== CIFS share mount =="
# The recordings share is a plain _netdev mount, NOT x-systemd.automount:
# autofs is unavailable inside an LXC container, so the automount unit silently
# never fires. nofail keeps a share outage from blocking boot. The credentials
# file is a secret and must be placed manually at the path below (600 perms).
if ! grep -q '[[:space:]]/mnt/omnibus[[:space:]]' /etc/fstab; then
  echo '//anubis.zw-lab.net/ZW-OMNIBUS /mnt/omnibus cifs credentials=/root/.smb/anubis-omnibus.cred,iocharset=utf8,vers=3.0,nosharesock,_netdev,nofail 0 0' >> /etc/fstab
fi
mkdir -p /mnt/omnibus /root/.smb
systemctl daemon-reload
if [ -f /root/.smb/anubis-omnibus.cred ]; then
  mount /mnt/omnibus 2>/dev/null || echo "  (share mount deferred — check creds/network)"
else
  echo "  (place /root/.smb/anubis-omnibus.cred [chmod 600], then: mount /mnt/omnibus)"
fi

echo "== systemd unit =="
cat > /etc/systemd/system/omnibus-recorder.service <<'UNIT'
[Unit]
Description=ZW-Omnibus meeting recorder API
After=network-online.target mnt-omnibus.mount
Wants=network-online.target mnt-omnibus.mount

[Service]
Type=simple
ExecStart=/opt/omnibus/venv/bin/python -m omnibus serve
WorkingDirectory=/opt/omnibus
Restart=on-failure
RestartSec=5
# Chromium + ffmpeg need a writable HOME for pulse etc.
Environment=HOME=/var/lib/omnibus
Environment=XDG_RUNTIME_DIR=/run/omnibus
Environment=PLAYWRIGHT_BROWSERS_PATH=/opt/omnibus/pw-browsers
RuntimeDirectory=omnibus

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable omnibus-recorder >/dev/null 2>&1
echo "== done (service NOT started — start after /etc/omnibus/recorder.env is in place) =="
