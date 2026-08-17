#!/bin/bash
#
# Serve the camera's hostname locally, because the AT&T gateway will not.
#
#   sudo ./setup-dnsmasq.sh
#
# WHY THIS EXISTS
#   camera.<domain> has a public A record pointing at this Pi's LAN address,
#   which is what the Let's Encrypt certificate is issued for. Every device on
#   the LAN resolves through the BGW320-500, and that gateway drops any public
#   DNS answer containing an RFC1918 address - DNS rebinding protection, not
#   configurable in its firewall settings. Verified: deSEC's own nameservers,
#   1.1.1.1 and 8.8.8.8 all return the Pi's LAN address; the gateway returns
#   nothing.
#
#   dnsmasq answers for that one name locally and forwards everything else,
#   so the name resolves and the certificate validates.
#
# WHAT IT DOES NOT DO
#   No DHCP - the gateway keeps doing that. This only answers queries.
#
#   It also does not change what the Pi itself uses for DNS. The Pi resolves
#   fine through the gateway and does not need the local name; leaving that
#   alone avoids a NetworkManager reconnect and a way to lose DNS entirely if
#   dnsmasq is ever down.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }

CONF=/etc/desec-ddns.conf
[ -r "$CONF" ] || { echo "Missing $CONF - run setup-desec-ddns.sh first."; exit 1; }
# shellcheck source=/dev/null
. "$CONF"
: "${DESEC_DOMAIN:?not set}"

SUB="${1:-camera}"
FQDN="${SUB}.${DESEC_DOMAIN}"

LAN_IF="$(ip route show default | awk '/default/{print $5; exit}')"
LAN_IP="$(ip -o -f inet addr show "$LAN_IF" | awk '{print $4; exit}' | cut -d/ -f1)"
LAN_CIDR="$(ip -o -f inet addr show "$LAN_IF" | awk '{print $4; exit}')"
LAN_SUBNET="$(python3 -c 'import ipaddress,sys; print(ipaddress.ip_network(sys.argv[1], strict=False))' "$LAN_CIDR")"
GATEWAY="$(ip route show default | awk '/default/{print $3; exit}')"
VPN_SUBNET=10.8.0.0/24

echo "== plan =="
echo "   answer locally : $FQDN -> $LAN_IP"
echo "   listen on      : lo, $LAN_IF, wg0"
echo "   forward to     : 1.1.1.1, 9.9.9.9   (not the gateway - it filters)"
echo "   except         : *.attlocal.net -> $GATEWAY, so LAN hostnames still work"
echo

if ss -tulnp 2>/dev/null | grep -qE ':53\b'; then
    echo "Something is already listening on port 53. Refusing to continue:"
    ss -tulnp | grep -E ':53\b' | sed 's/^/   /'
    exit 1
fi

echo "== packages =="
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq dnsmasq

echo "== configuration =="
cat > /etc/dnsmasq.d/camera.conf <<EOF
# Answer for the camera's certificate hostname. The gateway refuses to return
# this because it is a public name resolving to a private address.
host-record=${FQDN},${LAN_IP}

# bind-dynamic rather than bind-interfaces: wg0 may not exist when dnsmasq
# starts, and bind-interfaces would fail outright in that case.
bind-dynamic
interface=lo
interface=${LAN_IF}
interface=wg0

# Do not use /etc/resolv.conf - that points at the gateway, which is the thing
# filtering answers. Go straight to public resolvers instead.
no-resolv
server=1.1.1.1
server=9.9.9.9

# ...except the gateway's own local zone, so other LAN devices still resolve
# by name.
server=/attlocal.net/${GATEWAY}

cache-size=1000
domain-needed
bogus-priv

# Deliberately NOT setting stop-dns-rebind: this box exists precisely to serve
# a private address for a public name.

# No DHCP here. The gateway does that.
no-dhcp-interface=${LAN_IF}
no-dhcp-interface=wg0
EOF
echo "   /etc/dnsmasq.d/camera.conf written"

echo "== enabling the drop-in directory =="
# Debian ships /etc/dnsmasq.conf with every conf-dir line commented out, so a
# drop-in in /etc/dnsmasq.d is never read - and "dnsmasq --test" passes anyway,
# because it does not read the drop-in either. Silent, and easy to miss.
if grep -qE "^conf-dir=" /etc/dnsmasq.conf; then
    echo "   already enabled"
else
    printf '
# Load drop-ins.
conf-dir=/etc/dnsmasq.d/,*.dpkg-dist,*.dpkg-old,*.dpkg-new
' >> /etc/dnsmasq.conf
    echo "   conf-dir appended to /etc/dnsmasq.conf"
fi

echo "== syntax check =="
dnsmasq --test 2>&1 | sed 's/^/   /'

echo "== firewall =="
ufw allow from "$LAN_SUBNET" to any port 53 proto udp comment 'DNS from LAN' >/dev/null
ufw allow from "$LAN_SUBNET" to any port 53 proto tcp comment 'DNS from LAN' >/dev/null
ufw allow from "$VPN_SUBNET" to any port 53 proto udp comment 'DNS from VPN' >/dev/null
ufw allow from "$VPN_SUBNET" to any port 53 proto tcp comment 'DNS from VPN' >/dev/null
echo "   port 53 opened to ${LAN_SUBNET} and ${VPN_SUBNET} only"

echo "== starting =="
systemctl enable --now dnsmasq
sleep 2
systemctl is-active dnsmasq | sed 's/^/   dnsmasq: /'

echo
echo "== verifying =="
ANS="$(dig +short "@127.0.0.1" A "$FQDN" | head -1)"
if [ "$ANS" = "$LAN_IP" ]; then
    echo "   $FQDN -> $ANS   correct"
else
    echo "   $FQDN -> ${ANS:-NO ANSWER}   expected $LAN_IP"
    echo "   The drop-in is not being read. Check for an uncommented conf-dir"
    echo "   line in /etc/dnsmasq.conf, then restart dnsmasq."
    exit 1
fi
echo "   public forwarding       : $(dig +short "@127.0.0.1" A one.one.one.one | head -1)"
echo "   attlocal.net -> gateway : $(dig +short "@127.0.0.1" A dsldevice.attlocal.net | head -1)"

cat <<EOF

-----------------------------------------------------------------
dnsmasq is answering on ${LAN_IP}:53.

To use it, point clients at this Pi for DNS:

  WireGuard clients   already handled - new configs from
                      wireguard-add-client.sh carry DNS = 10.8.0.1.
                      Existing clients need that line added, or
                      re-issue them.

  LAN devices         either set the router's DHCP DNS server to
                      ${LAN_IP}, or set it per device.

Until a device is pointed here, it will keep asking the gateway and
${FQDN} will not resolve for it.
-----------------------------------------------------------------
EOF
