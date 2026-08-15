#!/bin/bash
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
#   The failure mode: the interface stays associated and still receives router
#   multicast, but stops carrying unicast traffic. Only a power cycle clears it.
#
# WHAT THIS DOES
#   Pins the profile to 2.4 GHz - same SSID, 40 signal units stronger, off DFS
#   entirely - and disables WiFi power save. 2.4 GHz is slower, which does not
#   matter here: the preview stream is about 3.6 Mbps and the rest is SSH.
#
# HOW IT PROVES ITSELF
#   Applying the change drops the SSH session that launched this, so the work
#   is written to a temporary script and detached with setsid. It must be a
#   file, not a heredoc: `setsid bash <<EOF ... </dev/null` silently does
#   nothing, because the /dev/null redirect replaces the heredoc on stdin.
#
#   The detached job then verifies connectivity FROM THE PI - gateway
#   reachable and DNS resolving - and reverts on its own if either fails.
#   Nothing to confirm, no timer to beat, no way to strand the machine.
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

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }

nmcli -t -f NAME connection show | grep -qx "$PROFILE" \
  || { echo "No NetworkManager profile named '$PROFILE'."; exit 1; }

if [ "${1:-}" = "--revert" ]; then
    echo "Reverting to automatic band selection and default power save."
    nmcli connection modify "$PROFILE" 802-11-wireless.band "" 802-11-wireless.powersave 0
    nmcli connection up "$PROFILE" >/dev/null 2>&1 || true
    logger -t "$TAG" "manually reverted"
    echo "Done."
    exit 0
fi

GATEWAY="$(ip route show default | awk '/default/{print $3; exit}')"
[ -n "$GATEWAY" ] || { echo "No default gateway found; refusing to change WiFi."; exit 1; }

echo "== before =="
nmcli -f 802-11-wireless.band,802-11-wireless.powersave connection show "$PROFILE" | sed 's/^/   /'
nmcli -f ACTIVE,SSID,BSSID,CHAN,FREQ,SIGNAL device wifi list | grep '^yes' | sed 's/^/   /' || true
echo "   gateway: $GATEWAY"

# The payload has to be a real file. Feeding it to `setsid bash` on stdin and
# also redirecting stdin from /dev/null means bash reads nothing and exits
# without running a line of it - which is exactly what happened the first time.
PAYLOAD="$(mktemp /run/wifi-stabilise.XXXXXX)"
chmod 700 "$PAYLOAD"

cat > "$PAYLOAD" <<EOF
#!/bin/bash
PROFILE='${PROFILE}'
GATEWAY='${GATEWAY}'
TAG='${TAG}'

logger -t "\$TAG" "applying: band=bg (2.4GHz), powersave=disabled"

# band=bg restricts to 2.4 GHz without pinning one BSSID, so the radio can
# still move between 2.4 GHz APs. powersave=2 is 'disabled'; 0 means 'use the
# default', which is not the same thing.
nmcli connection modify "\$PROFILE" 802-11-wireless.band bg 802-11-wireless.powersave 2
nmcli connection up "\$PROFILE" >/dev/null 2>&1

ok=0
for i in \$(seq 1 15); do
    sleep 4
    if ping -c1 -W2 "\$GATEWAY" >/dev/null 2>&1 \\
       && getent hosts deb.debian.org >/dev/null 2>&1; then
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

setsid "$PAYLOAD" </dev/null >/dev/null 2>&1 &
disown 2>/dev/null || true

cat <<EOF

Applying now. Your SSH session will drop for a few seconds - that is expected.

The Pi verifies the new settings itself: gateway reachable and DNS resolving,
retried for up to 60 seconds. It reverts on its own if either fails. There is
nothing you need to type in time.

Wait about 90 seconds, then:

    ssh pi@192.168.1.125 "/opt/camera/deploy/wifi-stabilise.sh --status"

Look for a line containing VERIFIED. If it reverted instead, the log says so
and nothing is left half-applied.
EOF
