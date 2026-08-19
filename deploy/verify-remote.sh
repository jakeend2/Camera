#!/bin/bash
# Walk the chain that gets a phone on mobile data to the web UI, and say which
# link is broken. Getting in from outside is eight things in a row, and every
# one of them fails the same way from the sofa: "it doesn't load".
#
#   sudo deploy/verify-remote.sh [vpn-endpoint-host]
#
# Read-only: it queries, it never reconfigures. The deSEC token is not printed,
# only whether it still works.
#
# TWO NAMES, and confusing them sends you hunting the wrong fault:
#
#   jakeend2.dedyn.io          the VPN endpoint. MUST be this house's public
#                              IP. The deSEC timer keeps it current.
#   camera.jakeend2.dedyn.io   the UI's name, and the name on the certificate.
#                              It points at the Pi's LAN address ON PURPOSE -
#                              setup-letsencrypt.sh publishes it that way so
#                              the certificate's name resolves to the Pi from
#                              the LAN and through the tunnel. Seeing a private
#                              address here is correct, not a leak of a broken
#                              record, and chasing it wastes an evening.
# Deliberately NOT pipefail. The checks below are `<command> | grep -q ...`,
# and grep -q exits the instant it matches, which SIGPIPEs the writer; under
# pipefail that reads as a failed pipeline. It is a race decided by whichever
# process finishes first, and it made this script report "no desec-ddns units
# installed" about units it had confirmed working ninety seconds earlier.
set -u
cd /opt/camera 2>/dev/null || true

PASS=0; FAIL=0; SKIP=0
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
skip() { printf '  \033[33mSKIP\033[0m %s\n' "$*"; SKIP=$((SKIP+1)); }
note() { printf '       %s\n' "$*"; }
step() { printf '\n%s\n' "$*"; }

case "${1:-}" in
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
esac

UI_HOST="camera.jakeend2.dedyn.io"

# --watch: sit on the wire while you try to connect from the phone, and say
# which of the three things happened. Timing a capture against somebody else
# picking up their phone does not work; this waits for them instead.
if [ "${1:-}" = "--watch" ]; then
    [ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
    SECS="${2:-180}"
    B=$(wg show wg0 latest-handshakes 2>/dev/null | awk '{print $2}' | head -1)
    RX0=$(wg show wg0 transfer 2>/dev/null | awk '{print $2}' | head -1)
    echo "Watching udp/51820 for ${SECS}s. Toggle the tunnel OFF and ON now -"
    echo "off and on, not just opening the app: that is what makes WireGuard"
    echo "re-resolve the hostname, which a stale cached address defeats."
    echo
    timeout "$SECS" tcpdump -n -i any udp port 51820 2>/dev/null | sed 's/^/  /' &
    TCPD=$!
    wait $TCPD 2>/dev/null
    A=$(wg show wg0 latest-handshakes 2>/dev/null | awk '{print $2}' | head -1)
    RX1=$(wg show wg0 transfer 2>/dev/null | awk '{print $2}' | head -1)
    echo
    if [ "${A:-0}" != "${B:-0}" ]; then
        echo "HANDSHAKE COMPLETED - the tunnel is up."
        wg show wg0 endpoints 2>/dev/null | sed 's/^/  from /'
        echo "Whatever was wrong, re-resolving the endpoint fixed it."
    elif [ "${RX1:-0}" != "${RX0:-0}" ]; then
        echo "PACKETS ARRIVED but no handshake completed."
        echo "The network path is fine; the phone's key or config does not match"
        echo "this server. Re-issue it: sudo deploy/wireguard-add-client.sh <name>"
    else
        echo "NOTHING ARRIVED on 51820."
        echo "If you really did toggle the tunnel, the packets are being stopped"
        echo "before this Pi: the router's udp/51820 forward, or the carrier."
        echo "This port has accepted $(iptables -L -v -n 2>/dev/null | awk '/51820/{print $1; exit}') packets since the rules loaded,"
        echo "so it has worked before - look at the router first."
    fi
    exit 0
fi

VPN_HOST="${1:-jakeend2.dedyn.io}"
IFACE="$(ip route show default | awk '/default/{print $5;exit}')"
LAN_IP="$(ip -o -f inet addr show "$IFACE" | awk '{print $4}' | cut -d/ -f1)"
ROOT=0; [ "$(id -u)" -eq 0 ] && ROOT=1
[ "$ROOT" -eq 0 ] && note "not root: WireGuard, ufw and the DDNS journal will be skipped"

# Ask over DNS-over-HTTPS. A plain :53 query from inside this network can be
# answered by our own dnsmasq or intercepted by the router, which makes a
# "public DNS" check quietly measure the wrong thing.
pubdns() {
    curl -s --max-time 10 -H 'accept: application/dns-json' \
        "https://cloudflare-dns.com/dns-query?name=$1&type=A" 2>/dev/null \
    | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print(''); raise SystemExit
print(next((a['data'] for a in d.get('Answer',[]) if a.get('type')==1), ''))
" 2>/dev/null
}

step "1. This network's public address"
WAN="$(curl -s --max-time 8 https://api.ipify.org 2>/dev/null)"
[ -n "$WAN" ] && ok "public IP $WAN" || bad "could not determine the public IP"

step "2. The VPN endpoint name points at it"
EP="$(pubdns "$VPN_HOST")"
if [ -z "$EP" ]; then
    bad "$VPN_HOST does not resolve publicly"
elif [ "$EP" = "$WAN" ]; then
    ok "$VPN_HOST -> $EP"
else
    bad "$VPN_HOST -> $EP but this network is $WAN"
    note "The address changed and the DDNS record is stale. See step 3."
fi

step "3. The dynamic DNS updater is alive and authenticated"
if [ "$ROOT" -eq 0 ]; then
    skip "needs root to run the updater and read its journal"
elif systemctl list-unit-files 2>/dev/null | grep -q '^desec-ddns'; then
    systemctl is-enabled --quiet desec-ddns.timer 2>/dev/null \
        && ok "desec-ddns.timer enabled" \
        || bad "desec-ddns.timer is not enabled - the record will go stale"
    note "last run: $(systemctl show desec-ddns.timer -p LastTriggerUSec --value 2>/dev/null)"
    systemctl start desec-ddns.service >/dev/null 2>&1; sleep 4
    OUT="$(journalctl -u desec-ddns.service -n 10 --no-pager 2>/dev/null)"
    case "$OUT" in
        *good*|*nochg*) ok "deSEC accepted the update" ;;
        *401*|*403*|*nauthor*) bad "deSEC rejected the credentials"
            note "Token changed at desec.io but not here."
            note "Fix: sudo /opt/camera/deploy/setup-desec-ddns.sh" ;;
        *) note "updater said:"; printf '%s\n' "$OUT" | tail -3 | sed 's/^/         /' ;;
    esac
else
    bad "no desec-ddns units installed"
fi

step "4. WireGuard is up, with peers that have actually connected"
if [ "$ROOT" -eq 0 ]; then
    skip "wg show needs root"
elif systemctl is-active --quiet wg-quick@wg0 2>/dev/null; then
    ok "wg-quick@wg0 active"
    P="$(wg show wg0 listen-port 2>/dev/null)"
    [ -n "$P" ] && ok "listening on udp/$P" || bad "wg0 has no listen port"
    N="$(wg show wg0 peers 2>/dev/null | wc -l)"
    [ "$N" -gt 0 ] && ok "$N peer(s) configured" || bad "no peers - no client can connect"
    SEEN=0
    while read -r key when; do
        [ -z "$key" ] && continue
        if [ "${when:-0}" = "0" ]; then
            note "peer ${key:0:12}...  never connected"
        else
            note "peer ${key:0:12}...  handshake $(( ($(date +%s) - when) / 60 )) min ago"
            SEEN=1
        fi
    done < <(wg show wg0 latest-handshakes 2>/dev/null)
    [ "$SEEN" -eq 1 ] && ok "at least one peer has completed a handshake" \
                      || bad "no peer has EVER completed a handshake"
    [ "$SEEN" -eq 1 ] || note "That points at step 5 or 6: the packets are not arriving."
else
    bad "wg-quick@wg0 is not active"
    note "Fix: sudo systemctl restart wg-quick@wg0"
fi

step "5. The firewall lets the VPN in, and the UI through it"
if [ "$ROOT" -eq 0 ]; then
    skip "ufw status needs root"
elif ufw status 2>/dev/null | grep -q 'Status: active'; then
    ufw status 2>/dev/null | grep -q '51820' \
        && ok "51820/udp allowed" \
        || bad "51820/udp is NOT allowed - the handshake never arrives"
    ufw status 2>/dev/null | grep -qE '5000.*10\.8\.0' \
        && ok "5000/tcp allowed from the VPN subnet" \
        || bad "5000/tcp is not open to 10.8.0.0/24"
else
    note "ufw is not active; nothing to check"
fi

step "6. Your router forwards the VPN port"
note "The one link that cannot be tested from inside the house."
note "It must forward udp/51820 to $LAN_IP."
note "If 1-5 pass and a phone on mobile data still fails, it is this."
note "Check it from outside:  nc -vzu $WAN 51820"

step "7. The UI name resolves to the Pi (a private address here is CORRECT)"
UI_PUB="$(pubdns "$UI_HOST")"
UI_LOCAL="$(dig +short @127.0.0.1 "$UI_HOST" A 2>/dev/null | tail -1)"
[ "$UI_LOCAL" = "$LAN_IP" ] && ok "locally $UI_HOST -> $LAN_IP" \
                            || bad "locally $UI_HOST -> ${UI_LOCAL:-nothing}, expected $LAN_IP"
if [ "$UI_PUB" = "$LAN_IP" ]; then
    ok "publicly $UI_HOST -> $LAN_IP, which is how setup-letsencrypt.sh sets it"
elif [ -n "$UI_PUB" ]; then
    note "publicly $UI_HOST -> $UI_PUB (differs from the LAN address; fine as long"
    note "as clients on the tunnel get $LAN_IP from this Pi's resolver)"
fi
systemctl is-active --quiet dnsmasq 2>/dev/null && ok "dnsmasq active" \
                                                || bad "dnsmasq is not running"

step "8. The service answers, with a certificate for that name"
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 \
        --cacert /etc/camera-tls/server.crt --resolve "$UI_HOST:5000:$LAN_IP" \
        "https://$UI_HOST:5000/health" 2>/dev/null)"
case "$CODE" in
    401|200) ok "https://$UI_HOST:5000/health -> $CODE" ;;
    *)       bad "no answer over TLS (got '${CODE:-nothing}')" ;;
esac
if [ -f /etc/camera-tls/server.crt ]; then
    SANS="$(openssl x509 -in /etc/camera-tls/server.crt -noout -text 2>/dev/null \
            | grep -A1 'Subject Alternative Name' | tail -1 | tr -d ' ')"
    case "$SANS" in
        *"$UI_HOST"*) ok "certificate covers $UI_HOST" ;;
        *) bad "certificate does NOT cover $UI_HOST"; note "names: ${SANS:-none}" ;;
    esac
    note "expires $(openssl x509 -in /etc/camera-tls/server.crt -noout -enddate 2>/dev/null | cut -d= -f2)"
fi

printf '\n%s\n' "-------------------------------------------------"
if [ "$FAIL" -eq 0 ]; then
    printf 'REMOTE CHAIN OK  (%d checked, %d skipped)\n' "$PASS" "$SKIP"
    [ "$SKIP" -gt 0 ] && printf 'Re-run with sudo to cover the skipped links.\n'
    printf 'If it still fails from outside, it is step 6: the router.\n'
else
    printf 'BROKEN LINKS: %d   (%d passed, %d skipped)\n' "$FAIL" "$PASS" "$SKIP"
    printf 'Fix the FIRST failure and re-run - later ones often follow from it.\n'
fi
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
