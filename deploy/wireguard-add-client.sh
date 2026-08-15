#!/bin/bash
#
# Add a WireGuard client and print its config, plus a QR code for phones.
#
#   sudo ./wireguard-add-client.sh <name>
#
# Each client gets its own keypair. The private key is generated here and
# shown once - it is never needed again on this machine, so delete the file
# afterwards if you saved it.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
[ $# -eq 1 ] || { echo "Usage: sudo $0 <name>   e.g. phone, laptop"; exit 1; }

NAME="$1"
WG_DIR=/etc/wireguard
CONF="$WG_DIR/wg0.conf"
LAN_SUBNET="192.168.1.0/24"
VPN_SUBNET="10.8.0.0/24"

[ -f "$CONF" ] || { echo "Run setup-wireguard.sh first."; exit 1; }
ENDPOINT="$(cat "$WG_DIR/endpoint")"

grep -q "^# client: ${NAME}\$" "$CONF" && { echo "Client '${NAME}' already exists."; exit 1; }

# Next free address in the VPN subnet: .1 is the server, clients start at .2.
LAST=$(grep -oE 'AllowedIPs = 10\.8\.0\.([0-9]+)/32' "$CONF" | grep -oE '[0-9]+/32' | cut -d/ -f1 | sort -n | tail -1)
NEXT=$(( ${LAST:-1} + 1 ))
[ "$NEXT" -le 254 ] || { echo "VPN subnet full."; exit 1; }
CLIENT_IP="10.8.0.${NEXT}"

umask 077
CLIENT_KEY=$(wg genkey)
CLIENT_PUB=$(printf '%s' "$CLIENT_KEY" | wg pubkey)
# A preshared key adds a symmetric layer on top, cheap post-quantum insurance.
PSK=$(wg genpsk)

cat >> "$CONF" <<EOF

# client: ${NAME}
[Peer]
PublicKey = ${CLIENT_PUB}
PresharedKey = ${PSK}
AllowedIPs = ${CLIENT_IP}/32
EOF

# Apply without dropping existing connections.
wg syncconf wg0 <(wg-quick strip wg0)

CLIENT_CONF=$(cat <<EOF
[Interface]
PrivateKey = ${CLIENT_KEY}
Address = ${CLIENT_IP}/32

[Peer]
PublicKey = $(cat "$WG_DIR/server.pub")
PresharedKey = ${PSK}
Endpoint = ${ENDPOINT}
# Split tunnel: only home traffic goes through the VPN. Everything else uses
# the normal connection, so browsing is unaffected and battery lasts longer.
AllowedIPs = ${LAN_SUBNET}, ${VPN_SUBNET}
PersistentKeepalive = 25
EOF
)

echo
echo "===================== ${NAME} (${CLIENT_IP}) ====================="
echo "$CLIENT_CONF"
echo "=============================================================="
echo
if command -v qrencode >/dev/null; then
    echo "Scan this in the WireGuard app:"
    printf '%s\n' "$CLIENT_CONF" | qrencode -t ansiutf8
fi
echo
echo "The private key above is not stored anywhere. Capture it now or"
echo "re-run this script with a different name to issue another client."
echo
echo "To revoke later: delete the '# client: ${NAME}' block from ${CONF}"
echo "then run: wg syncconf wg0 <(wg-quick strip wg0)"
