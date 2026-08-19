#!/bin/bash
#
# Set up WireGuard so the camera (and later the garage and thermostat) can be
# reached from outside without exposing any of them to the internet.
#
# The design: exactly one UDP port is forwarded, and WireGuard never answers a
# packet that is not cryptographically valid - so a port scan of it returns
# nothing at all. Once a client is connected it is logically on the LAN, and
# every service keeps its existing LAN-only firewall rules unchanged.
#
#   sudo ./setup-wireguard.sh <public-hostname-or-ip> [port]
#
# Run once. Add clients afterwards with:  sudo $0 add-client <name>
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
# ---------------------------------------------------------------- add-client --
# Formerly its own script (wireguard-add-client.sh). Issuing a device config
# is maintenance rather than setup, so it lives here as a subcommand now.
if [ "${1:-}" = "add-client" ]; then
    shift
    [ $# -eq 1 ] || { echo "Usage: sudo $0 add-client <name>   e.g. phone, laptop"; exit 1; }


NAME="$1"
WG_DIR=/etc/wireguard
CONF="$WG_DIR/wg0.conf"
VPN_SUBNET="10.8.0.0/24"

# Derived from this machine's own address rather than assumed. A hardcoded
# 192.168.1.0/24 produces clients that connect happily and then route nothing,
# which is a confusing failure to diagnose.
LAN_IF="$(ip route show default | awk '/default/{print $5; exit}')"
LAN_CIDR="$(ip -o -f inet addr show "$LAN_IF" | awk '{print $4; exit}')"
LAN_SUBNET="$(python3 -c 'import ipaddress,sys; print(ipaddress.ip_network(sys.argv[1], strict=False))' "$LAN_CIDR")"

[ -f "$CONF" ] || { echo "Run: sudo $0 <public-hostname-or-ip>   first."; exit 1; }
ENDPOINT="$(cat "$WG_DIR/endpoint")"

if grep -q "^# client: ${NAME}\$" "$CONF"; then
    echo "Client '${NAME}' already exists. Use a different name, or remove"
    echo "its block from ${CONF} first."
    exit 1
fi

# Next free address in the VPN subnet: .1 is the server, clients start at .2.
#
# Guarded by an explicit test rather than letting the pipeline run dry: with
# no peers yet, grep exits non-zero, and under 'set -eo pipefail' that aborts
# the script before it writes anything - which broke the very first client.
LAST=1
if grep -q 'AllowedIPs = 10\.8\.0\.' "$CONF"; then
    LAST=$(grep -oE 'AllowedIPs = 10\.8\.0\.[0-9]+/32' "$CONF" \
           | sed -E 's|.*10\.8\.0\.([0-9]+)/32|\1|' | sort -n | tail -1)
fi
NEXT=$(( LAST + 1 ))
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
# Resolve through the Pi. The router drops public DNS answers pointing at
# private addresses, so the name on the TLS certificate will not resolve
# through it.
DNS = 10.8.0.1

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

    exit 0
fi

[ $# -ge 1 ] || { echo "Usage: sudo $0 <public-hostname-or-ip> [port]"; exit 1; }

ENDPOINT_HOST="$1"
PORT="${2:-51820}"
VPN_SUBNET="10.8.0.0/24"
VPN_SERVER_IP="10.8.0.1/24"
WG_DIR=/etc/wireguard

# The LAN interface is whatever carries the default route. Do not hardcode
# it: this Pi ran on wlan0 for months and moved to eth0 when a PoE switch
# arrived. Naming the wrong one silently breaks NAT for every VPN client
# while the tunnel itself keeps handshaking, which is a miserable debug.
LAN_IF="$(ip route show default | awk '/default/{print $5; exit}')"
[ -n "$LAN_IF" ] || { echo "Could not determine the LAN interface."; exit 1; }
LAN_IP="$(ip -o -f inet addr show "$LAN_IF" | awk '{print $4; exit}' | cut -d/ -f1)"
echo "LAN interface: $LAN_IF ($LAN_IP)"

echo "== installing packages =="
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq wireguard wireguard-tools qrencode

echo "== server keypair =="
install -d -m 700 "$WG_DIR"
if [ ! -f "$WG_DIR/server.key" ]; then
    (umask 077; wg genkey > "$WG_DIR/server.key")
    wg pubkey < "$WG_DIR/server.key" > "$WG_DIR/server.pub"
    echo "  generated"
else
    echo "  already present, keeping it"
fi
chmod 600 "$WG_DIR/server.key"

# Remembered so add-client can build client configs without asking again.
printf '%s:%s\n' "$ENDPOINT_HOST" "$PORT" > "$WG_DIR/endpoint"

echo "== /etc/wireguard/wg0.conf =="
if [ ! -f "$WG_DIR/wg0.conf" ]; then
    cat > "$WG_DIR/wg0.conf" <<EOF
# Server interface. Peers are appended below by the add-client subcommand.
[Interface]
Address = ${VPN_SERVER_IP}
ListenPort = ${PORT}
PrivateKey = $(cat "$WG_DIR/server.key")
EOF
    chmod 600 "$WG_DIR/wg0.conf"
    echo "  written"
else
    echo "  already exists, leaving it alone"
fi

echo "== IP forwarding =="
# Needed so VPN clients can reach the rest of the LAN, not just the Pi.
cat > /etc/sysctl.d/99-wireguard.conf <<'EOF'
net.ipv4.ip_forward = 1
EOF
sysctl -q --system
echo "  net.ipv4.ip_forward = $(cat /proc/sys/net/ipv4/ip_forward)"

echo "== firewall =="
# The single inbound port, open to the internet.
ufw allow "${PORT}/udp" comment 'WireGuard' >/dev/null

# VPN clients get the same service access LAN clients already have.
for p in 22 5000 1883; do
    ufw allow from "$VPN_SUBNET" to any port "$p" proto tcp comment 'from VPN' >/dev/null
done

# Forwarding stays default-deny; this permits only VPN -> LAN specifically.
ufw route allow in on wg0 out on "$LAN_IF" >/dev/null

# Masquerade so LAN devices reply to the Pi rather than needing a route to
# 10.8.0.0/24 configured on the router.
if ! grep -q "WIREGUARD NAT" /etc/ufw/before.rules; then
    cp /etc/ufw/before.rules "/etc/ufw/before.rules.bak-$(date +%Y%m%d%H%M%S)"
    cat > /tmp/wg-nat.$$ <<EOF
# WIREGUARD NAT - added by setup-wireguard.sh
*nat
:POSTROUTING ACCEPT [0:0]
-A POSTROUTING -s ${VPN_SUBNET} -o ${LAN_IF} -j MASQUERADE
COMMIT

EOF
    cat /tmp/wg-nat.$$ /etc/ufw/before.rules > /tmp/wg-rules.$$
    mv /tmp/wg-rules.$$ /etc/ufw/before.rules
    rm -f /tmp/wg-nat.$$
    echo "  NAT rule added"
else
    echo "  NAT rule already present"
fi
ufw reload >/dev/null

echo "== starting =="
systemctl enable --now wg-quick@wg0
sleep 2
wg show

cat <<EOF

------------------------------------------------------------------
Server is up. Two things remain, and neither can be done from here:

1. On your router, forward UDP ${PORT} to ${LAN_IP}.
   Give this Pi a DHCP reservation for ${LAN_IF} at the same time -
   several things bake this address in, so a lease change breaks
   them all at once. See 'When the Pi's address changes' in
   deploy/README.md for the full list.

2. Add a client:
     sudo /opt/camera/deploy/setup-wireguard.sh add-client phone

Server public key: $(cat "$WG_DIR/server.pub")
Clients will connect to: ${ENDPOINT_HOST}:${PORT}
------------------------------------------------------------------
EOF
