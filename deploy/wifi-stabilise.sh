#!/bin/bash
#
# Stop wlan0 ping-ponging between two weak 5 GHz DFS access points.
#
# WHAT THE LOGS SHOWED
#   The SSID 'wulffgar' is broadcast by three radios:
#     CC:AB:2C:83:2B:04  ch  11  2462 MHz  signal 90   <- 2.4 GHz, non-DFS
#     CC:AB:2C:83:2B:0C  ch 100  5500 MHz  signal 50   <- 5 GHz, DFS
#     CC:AB:2C:83:2B:08  ch  64  5320 MHz  signal 49   <- 5 GHz, DFS
#
#   The profile pinned neither band nor BSSID, so the supplicant chose 5 GHz
#   and then oscillated between two radios sitting one signal unit apart:
#   11 re-associations in the 16 minutes before the interface wedged, and 3
#   more within 7 minutes of the following boot.
#
#   Channels 52-144 are DFS in the US, so both 5 GHz radios must also vacate
#   on radar detection - channel switching layered on top of the roaming.
#
#   The failure mode is consistent: the interface stays associated in software
#   and keeps receiving router multicast, but stops carrying unicast traffic.
#   Only a power cycle clears it.
#
# WHAT THIS DOES
#   Pins the profile to 2.4 GHz, where the same SSID is 40 signal units
#   stronger and off the DFS channels entirely, and disables WiFi power save.
#
#   2.4 GHz is slower, which does not matter here: the preview stream is about
#   3.6 Mbps and everything else is SSH. Reliability is the scarce resource.
#
#   Ethernet remains the real answer. This is the fix that needs no cable.
#
# SAFETY
#   Reconnecting drops your SSH session for a few seconds. If the new settings
#   fail to associate, they are reverted automatically after 3 minutes unless
#   you confirm. Nothing here can strand the machine permanently.
#
#   sudo ./wifi-stabilise.sh            apply, with auto-revert armed
#   sudo ./wifi-stabilise.sh --revert   undo immediately
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }

PROFILE="${WIFI_PROFILE:-wulffgar}"
SENTINEL=/tmp/wifi-stabilise-confirmed
REVERT_AFTER=180

nmcli -t -f NAME connection show | grep -qx "$PROFILE" \
  || { echo "No NetworkManager profile named '$PROFILE'."; exit 1; }

if [ "${1:-}" = "--revert" ]; then
    echo "Reverting to automatic band selection and default power save."
    nmcli connection modify "$PROFILE" \
        802-11-wireless.band "" \
        802-11-wireless.powersave 0
    nmcli connection up "$PROFILE" >/dev/null
    rm -f "$SENTINEL"
    echo "Done."
    exit 0
fi

echo "== before =="
nmcli -f 802-11-wireless.band,802-11-wireless.bssid,802-11-wireless.powersave \
    connection show "$PROFILE" | sed 's/^/   /'
echo "   currently associated with:"
nmcli -f ACTIVE,SSID,BSSID,CHAN,SIGNAL device wifi list \
    | awk 'NR==1 || $1=="yes"' | sed 's/^/     /'

echo
echo "== arming a 3 minute auto-revert =="
rm -f "$SENTINEL"
setsid bash -c "
    sleep ${REVERT_AFTER}
    if [ ! -f '${SENTINEL}' ]; then
        logger -t wifi-stabilise 'not confirmed, reverting band and powersave'
        nmcli connection modify '${PROFILE}' 802-11-wireless.band '' 802-11-wireless.powersave 0
        nmcli connection up '${PROFILE}'
    fi
" </dev/null >/dev/null 2>&1 &
echo "   armed"

echo
echo "== applying: 2.4 GHz only, power save off =="
# band=bg restricts to 2.4 GHz without pinning a single BSSID, so the radio can
# still move between 2.4 GHz APs if the router ever gains another.
# powersave=2 is 'disabled'; 0 means 'use the default', which is not the same.
nmcli connection modify "$PROFILE" \
    802-11-wireless.band bg \
    802-11-wireless.powersave 2

# Reconnect detached: this drops the SSH session that started the script, and
# the reconnect must survive that.
setsid bash -c "sleep 2; nmcli connection up '${PROFILE}'" \
    </dev/null >/dev/null 2>&1 &

cat <<EOF

Reconnecting now - your SSH session will drop for a few seconds.

Wait about 20 seconds, then reconnect and CONFIRM, or everything reverts
automatically in ${REVERT_AFTER} seconds:

    ssh pi@192.168.1.125 "sudo -n touch ${SENTINEL} 2>/dev/null || sudo touch ${SENTINEL}"

Then check what it settled on:

    ssh pi@192.168.1.125 "nmcli -f ACTIVE,SSID,BSSID,CHAN,FREQ,SIGNAL device wifi list | awk 'NR==1 || \\\$1==\\\"yes\\\"'"

You want to see CHAN 11 at 2462 MHz with a signal near 90.
EOF
