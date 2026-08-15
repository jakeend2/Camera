#!/bin/bash
# Change the web UI password.
#
# Prompts for the new password, writes only its hash to
# /etc/camera-service.env, and restarts the service. The plaintext is never
# stored or echoed.
set -euo pipefail

ENV=/etc/camera-service.env
VENV=/home/pi/Desktop/Camera/venv/bin/python

read -rsp "New password: " p1; echo
read -rsp "Confirm:      " p2; echo
[ "$p1" = "$p2" ] || { echo "Passwords do not match."; exit 1; }
[ ${#p1} -ge 12 ] || { echo "Use at least 12 characters."; exit 1; }

HASH=$(P="$p1" "$VENV" -c 'import os;from werkzeug.security import generate_password_hash;print(generate_password_hash(os.environ["P"]))')
unset p1 p2

sudo sed -i "/^WEB_PASSWORD_HASH=/d" "$ENV"
echo "WEB_PASSWORD_HASH='$HASH'" | sudo tee -a "$ENV" >/dev/null
sudo chmod 640 "$ENV"; sudo chown root:pi "$ENV"
sudo rm -f /etc/camera-web-initial-password

sudo systemctl restart camera.service
echo "Password updated and service restarted."
