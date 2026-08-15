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
#   Reconnecting drops the SSH session that launched it, so everything after
#   the change runs detached. The script then verifies connectivity FROM THE
#   PI - gateway reachable and DNS resolving - and reverts on its own if that
#   fails. No confirmation step, nothing to type within a time limit, and no
#   way to strand the machine by losing your shell at the wrong moment.
#
#   Outcome is written to the journal under the tag 'wifi-stabilise'.
#
#   sudo ./wifi-stabilise.sh            apply, self-verifying
#   sudo ./wifi-stabilise.sh --revert   undo immediately
#   sudo ./wifi-stabilise.sh --status   show what happened
set -euo pipefail

PROFILE="${WIFI_PROFILE:-wulffgar}"
TAG=wifi-stabilise

case "${1:-}" in
  --status)
      echo "== profile =="
      nmcli -f 802-11-wireless.band,802-11-wireless.powersave connection show "$PROFILE"
      echo
      echo "== associated with =="
      nmcli -f ACTIVE,SSID,BSSID,CHAN,FREQ,SIGNAL device wifi list | head -1
      nmcli -f ACTIVE,SSID,BSSID,CHAN,FREQ,SIGNAL device wifi list | grep '^yes' || true
      echo
      echo "== recent outcome =="
      journalctl -t "$TAG" -n 15 --no-pager
      exit 0
      ;;
esac

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }

nmcli -t -f NAME connection show | grep -qx "$PROFILE" \
  || { echo "No NetworkManager profile named '$PROFILE'."; exit 1; }

revert_now() {
    nmcli connection modify "$PROFILE" \
        802-11-wireless.band "" \
        802-11-wireless.powersave 0
    nmcli connection up "$PROFILE" >/dev/null 2>&1 || true
}

if [ "${1:-}" = "--revert" ]; then
    echo "Reverting to automatic band selection and default power save."
    revert_now
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

# Everything below runs detached, because applying the change kills the SSH
# session this script was started from.
setsid bash <<EOF </dev/null >/dev/null 2>&1 &
exec 2>/dev/null
logger -t $TAG "applying: band=bg (2.4GHz), powersave=disabled"

# band=bg restricts to 2.4 GHz without pinning one BSSID, so the radio can
# still move between 2.4 GHz APs. powersave=2 is 'disabled'; 0 means 'use the
# default', which is not the same thing.
nmcli connection modify '$PROFILE' 802-11-wireless.band bg 802-11-wireless.powersave 2
nmcli connection up '$PROFILE'

# Verify from here rather than asking a human to confirm in time.
ok=0
for i in \$(seq 1 15); do
    sleep 4
    if ping -c1 -W2 '$GATEWAY' >/dev/null 2>&1 && getent hosts deb.debian.org >/dev/null 2>&1; then
        ok=1
        break
    fi
done

if [ "\$ok" = "1" ]; then
    band=\$(nmcli -g GENERAL.STATE device show wlan0 2>/dev/null)
    info=\$(nmcli -f ACTIVE,SSID,CHAN,FREQ,SIGNAL device wifi list | grep '^yes' | tr -s ' ')
    logger -t $TAG "VERIFIED on 2.4GHz - gateway reachable, DNS resolving - \$info"
else
    logger -t $TAG "FAILED to reach the network on 2.4GHz after 60s - reverting"
    nmcli connection modify '$PROFILE' 802-11-wireless.band '' 802-11-wireless.powersave 0
    nmcli connection up '$PROFILE'
    logger -t $TAG "reverted to automatic band selection"
fi
EOF

cat <<EOF

Applying now. Your SSH session will drop for a few seconds - that is expected.

The Pi verifies the new settings itself: it checks the gateway responds and
DNS resolves, and reverts on its own if either fails. There is nothing you
need to type in time.

Wait about a minute, then check what it decided:

    ssh pi@192.168.1.125 "/opt/camera/deploy/wifi-stabilise.sh --status"

A successful run logs a line containing VERIFIED. If it reverted, the log
says why, and nothing is left half-applied.
EOF
