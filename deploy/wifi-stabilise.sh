#!/bin/bash
#
# SUPERSEDED - this Pi now runs on eth0 through a PoE switch and its WiFi is
# disabled. Kept because the diagnosis is worth having if WiFi ever comes
# back: the dropouts were 802.11v BSS Transition Management frames that the
# brcmfmac firmware answers 'Unknown Frame', causing 11 re-associations in
# 16 minutes between two weak DFS APs. Pinning the BSSID took signal 50->87.
#
# Stop wlan0 ping-ponging between two weak 5 GHz DFS access points.
#
# WHAT THE LOGS SHOWED
#   The SSID is broadcast by three radios:
#     CC:AB:2C:83:2B:04  ch  11  2462 MHz  signal 90   <- 2.4 GHz, non-DFS
#     CC:AB:2C:83:2B:0C  ch 100  5500 MHz  signal 50   <- 5 GHz, DFS
#     CC:AB:2C:83:2B:08  ch  64  5320 MHz  signal 49   <- 5 GHz, DFS
#
#   The profile pinned neither band nor BSSID, so the supplicant chose 5 GHz
#   and oscillated between two radios one signal unit apart: 11 re-associations
#   in the 16 minutes before the interface wedged. Channels 52-144 are DFS in
#   the US, so radar avoidance can force channel changes on top of that.
#
#   The kernel log also shows the AP driving it: repeated
#     brcmf_p2p_send_action_frame: Unknown Frame: category 0xa, action 0x8
#   at exactly each re-association. Category 0x0a action 0x08 is an 802.11v
#   BSS Transition Management request - the gateway steering the client - and
#   the Broadcom firmware answers "Unknown Frame". Power save was on too.
#
#   The failure mode: the interface stays associated and still receives router
#   multicast, but stops carrying unicast traffic. Only a power cycle clears it.
#
# WHAT THIS DOES
#   Pins the profile to 2.4 GHz - same SSID, 40 signal units stronger, off DFS
#   entirely, and nowhere for the AP to steer it to - and disables WiFi power
#   save. 2.4 GHz is slower, which does not matter here: the preview stream is
#   about 3.6 Mbps and the rest is SSH.
#
# HOW IT PROVES ITSELF
#   Applying the change drops the SSH session that launched this, so the work
#   is written to a temporary script and handed to systemd-run.
#
#   Two earlier mechanisms failed silently, which is why it is done this way.
#   `setsid bash <<EOF ... </dev/null` runs nothing at all - the /dev/null
#   redirect replaces the heredoc on stdin. And a backgrounded `setsid payload &`
#   is killed the moment sudo returns, because this host has use_pty in its
#   sudoers Defaults: sudo runs the command in a pty and tears that session
#   down on exit, taking detached children with it. systemd-run hands the job
#   to PID 1, outside all of that.
#
#   The job then verifies connectivity FROM THE PI - gateway reachable and DNS
#   resolving - and reverts on its own if either fails. Nothing to confirm, no
#   timer to beat, no way to strand the machine.
#
#   Outcome is recorded in the journal under the tag 'wifi-stabilise'.
#
#   sudo ./wifi-stabilise.sh            apply, self-verifying
#   sudo ./wifi-stabilise.sh --revert   undo immediately
#   ./wifi-stabilise.sh --status        show what happened (no privileges)
set -euo pipefail

PROFILE="${WIFI_PROFILE:-wulffgar}"
TAG=wifi-stabilise

if [ "${1:-}" = "--status" ]; then
    echo "== profile =="
    nmcli -f 802-11-wireless.band,802-11-wireless.powersave connection show "$PROFILE"
    echo
    echo "== associated with =="
    nmcli -f ACTIVE,SSID,BSSID,CHAN,FREQ,SIGNAL device wifi list | head -1
    nmcli -f ACTIVE,SSID,BSSID,CHAN,FREQ,SIGNAL device wifi list | grep '^yes' || true
    echo
    echo "== outcome =="
    if journalctl -t "$TAG" --no-pager -n 20 2>/dev/null | grep -q .; then
        journalctl -t "$TAG" --no-pager -n 20
    else
        echo "(nothing logged yet - the run may still be in progress)"
    fi
    exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo."
    exit 1
fi

if ! nmcli -t -f NAME connection show | grep -qx "$PROFILE"; then
    echo "No NetworkManager profile named '$PROFILE'."
    exit 1
fi

if [ "${1:-}" = "--revert" ]; then
    echo "Reverting to automatic band selection and default power save."
    nmcli connection modify "$PROFILE" 802-11-wireless.band "" 802-11-wireless.powersave 0
    nmcli connection up "$PROFILE" >/dev/null 2>&1 || true
    logger -t "$TAG" "manually reverted"
    echo "Done."
    exit 0
fi

GATEWAY="$(ip route show default | awk '/default/{print $3; exit}')"
if [ -z "$GATEWAY" ]; then
    echo "No default gateway found; refusing to change WiFi."
    exit 1
fi

echo "== before =="
nmcli -f 802-11-wireless.band,802-11-wireless.powersave connection show "$PROFILE" | sed 's/^/   /'
nmcli -f ACTIVE,SSID,BSSID,CHAN,FREQ,SIGNAL device wifi list | grep '^yes' | sed 's/^/   /' || true
echo "   gateway: $GATEWAY"

PAYLOAD="$(mktemp /run/wifi-stabilise.XXXXXX)"
chmod 700 "$PAYLOAD"

cat > "$PAYLOAD" <<EOF
#!/bin/bash
PROFILE='${PROFILE}'
GATEWAY='${GATEWAY}'
TAG='${TAG}'

logger -t "\$TAG" "applying: band=bg (2.4GHz), powersave=disabled"

nmcli connection modify "\$PROFILE" 802-11-wireless.band bg 802-11-wireless.powersave 2
nmcli connection up "\$PROFILE" >/dev/null 2>&1

ok=0
for i in \$(seq 1 15); do
    sleep 4
    if ping -c1 -W2 "\$GATEWAY" >/dev/null 2>&1 && getent hosts deb.debian.org >/dev/null 2>&1; then
        ok=1
        break
    fi
done

if [ "\$ok" = "1" ]; then
    info="\$(nmcli -f ACTIVE,SSID,CHAN,FREQ,SIGNAL device wifi list | grep '^yes' | tr -s ' ')"
    logger -t "\$TAG" "VERIFIED on 2.4GHz - gateway reachable and DNS resolving - \$info"
else
    logger -t "\$TAG" "FAILED to reach the network on 2.4GHz after 60s - reverting"
    nmcli connection modify "\$PROFILE" 802-11-wireless.band '' 802-11-wireless.powersave 0
    nmcli connection up "\$PROFILE" >/dev/null 2>&1
    logger -t "\$TAG" "reverted to automatic band selection"
fi

rm -f "\$0"
EOF

# systemd-run, not a background job: sudo's use_pty kills detached children
# as soon as this script returns.
#
# Invoked as `/bin/bash <payload>` rather than executing the payload directly,
# because /run is mounted noexec. The file has mode 700 and is owned by root,
# but the mount forbids execve() regardless - systemd reported 203/EXEC,
# "Failed to locate executable ... Permission denied". Passing it as an
# argument to bash is an ordinary open() and is unaffected.
systemctl reset-failed wifi-stabilise-apply.service 2>/dev/null || true
systemd-run --collect --unit=wifi-stabilise-apply --description="Apply and verify 2.4 GHz WiFi settings" /bin/bash "$PAYLOAD" >/dev/null

cat <<EOF

Applying now. Your SSH session will drop for a few seconds - that is expected.

The Pi verifies the new settings itself: gateway reachable and DNS resolving,
retried for up to 60 seconds. It reverts on its own if either fails.

Wait about 90 seconds, then:

    ssh pi@192.168.1.77 "/opt/camera/deploy/wifi-stabilise.sh --status"

Look for a line containing VERIFIED.
EOF
