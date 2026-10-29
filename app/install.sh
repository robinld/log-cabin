#!/bin/bash
# Idempotent install/upgrade. Run as root on the box, from the unpacked bundle.
# Safe to re-run: scripts/deploy.sh calls it on every code change.
set -euo pipefail
set -a; . /etc/home-events/env; set +a

SRC=$(cd "$(dirname "$0")" && pwd)
ROOT=/opt/home-events
VENV=$ROOT/venv
PY=python3.11


# --- OS packages and service user -----------------------------------------
dnf install -y "$PY" "$PY-pip" sqlite amazon-ssm-agent wireguard-tools
id homeevents >/dev/null 2>&1 || useradd --system --home-dir "$ROOT" --shell /sbin/nologin homeevents
mountpoint -q /data
chown homeevents:homeevents /data

# --- Python ---------------------------------------------------------------
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$SRC/requirements.txt"

rm -rf "$ROOT/app"
mkdir -p "$ROOT/app"
cp -r "$SRC/home_events" "$SRC/datasette" "$ROOT/app/"
chown -R root:root "$ROOT/app"

# Stop the writers first, so a schema migration never races old code.
systemctl stop home-events-yolink.service home-events-evl.service home-events-unifi.service home-events-lutron.service home-events-lennox.service 2>/dev/null || true
sudo -u homeevents env DB_PATH="$DB_PATH" PYTHONPATH="$ROOT/app" "$VENV/bin/python" -m home_events.db

# --- WireGuard to the home network (skipped until its secret is set) ------
PYTHONPATH="$ROOT/app" "$VENV/bin/python" -m home_events.wireguard

# --- systemd --------------------------------------------------------------
install -m 644 "$SRC"/systemd/* /etc/systemd/system/
systemctl daemon-reload
for unit in home-events-yolink home-events-evl home-events-unifi home-events-lutron home-events-lennox home-events-datasette; do
  systemctl enable "$unit.service"
  systemctl restart "$unit.service"
done

# --- SSM agent on its IPv6 endpoints --------------------------------------
# Shell access and deploys then work with the WireGuard tunnel down, even when
# the tunnel carries all of the box's IPv4. The restart waits until this
# script, itself usually run by the agent, has finished.
if python3 - <<'PY_EOF'
import json, os, sys
d = "/etc/amazon/ssm/"
path = d + "amazon-ssm-agent.json"
cfg = json.load(open(path if os.path.exists(path) else d + "amazon-ssm-agent.json.template"))
if cfg["Agent"].get("UseDualStackEndpoint") is True:
    sys.exit(1)
cfg["Agent"]["UseDualStackEndpoint"] = True
json.dump(cfg, open(path, "w"), indent=4)
PY_EOF
then
  systemd-run --on-active=30 systemctl restart amazon-ssm-agent.service
fi

echo "install complete"
