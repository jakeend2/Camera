#!/bin/bash
#
# Install the deSEC.io dynamic DNS updater and its timer.
#
#   sudo ./setup-desec-ddns.sh
#
# Prompts for your deSEC hostname and token. The token is read without echo
# and stored root-only in /etc/desec-ddns.conf - never on the command line,
# where it would end up in shell history and in ps output.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF=/etc/desec-ddns.conf
BIN=/usr/local/sbin/desec-ddns-update

read -rp  "deSEC hostname (e.g. yourname.dedyn.io): " DOMAIN
read -rsp "deSEC token (input hidden): " TOKEN; echo
[ -n "$DOMAIN" ] && [ -n "$TOKEN" ] || { echo "Both values are required."; exit 1; }

echo "== installing the updater =="
install -m 750 -o root -g root "${SRC_DIR}/desec-ddns-update.sh" "$BIN"

umask 077
cat > "$CONF" <<EOF
# deSEC.io dynamic DNS credentials. Root-only, deliberately outside the repo.
DESEC_DOMAIN="${DOMAIN}"
DESEC_TOKEN="${TOKEN}"
EOF
chmod 600 "$CONF"; chown root:root "$CONF"
echo "  credentials written to $CONF"

echo "== testing the update before installing the timer =="
if ! "$BIN"; then
    echo
    echo "The update failed. Check the hostname and token, then re-run."
    echo "Nothing has been scheduled."
    exit 1
fi

echo "== systemd units =="
cat > /etc/systemd/system/desec-ddns.service <<EOF
[Unit]
Description=Update deSEC.io dynamic DNS record
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=${BIN}
# Nothing here needs privileges beyond reading the credential file.
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
EOF

cat > /etc/systemd/system/desec-ddns.timer <<'EOF'
[Unit]
Description=Refresh deSEC.io dynamic DNS every 5 minutes

[Timer]
# Shortly after boot, then steadily. A residential address rarely changes,
# but when it does you want the record to catch up in minutes, not hours.
OnBootSec=1min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now desec-ddns.timer
echo

echo "== result =="
systemctl list-timers desec-ddns.timer --no-pager | head -3
echo
echo "Record now points at: $(getent hosts "$DOMAIN" | awk '{print $1}' | head -1)"
echo
echo "Next: forward UDP 51820 to this Pi on your router, then run"
echo "  sudo ${SRC_DIR}/setup-wireguard.sh ${DOMAIN}"
