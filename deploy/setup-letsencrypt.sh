#!/bin/bash
#
# Replace the self-signed certificate with a real one from Let's Encrypt.
#
#   sudo ./setup-letsencrypt.sh [subdomain] [email]
#     subdomain defaults to 'camera'  ->  camera.<your dedyn.io domain>
#
# HOW IT WORKS
#   Uses the DNS-01 challenge, so nothing inbound is opened. certbot proves
#   ownership by writing a TXT record through deSEC's API - the same token the
#   dynamic DNS updater already uses, which has full API access.
#
#   An A record for the subdomain points at this Pi's LAN address. That is a
#   private address in public DNS, which is deliberate: the name resolves for
#   you on the LAN and over WireGuard, and to somewhere unreachable for anyone
#   else. Nothing is exposed - the firewall and the absence of a port forward
#   are unchanged. What does become public is the hostname itself, via DNS and
#   Certificate Transparency logs.
#
#   Renewal is unattended via certbot's own timer. A deploy hook copies the
#   new certificate into /etc/camera-tls with the ownership the service
#   expects and restarts it, so camera_service.py needs no change at all.
#
# NOTE ON THE deSEC API
#   Creating and replacing a record are different calls: POST to the rrsets
#   collection creates and returns 400 if the record already exists, while PUT
#   to the specific rrset path replaces and returns 404 if it does not exist.
#   Everything below tries PUT then falls back to POST, which is idempotent in
#   both directions - important, because renewals reuse the same record name.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }

SUB="${1:-camera}"
EMAIL="${2:-}"
CONF=/etc/desec-ddns.conf
TLS_DIR=/etc/camera-tls

[ -r "$CONF" ] || { echo "Missing $CONF - run setup-desec-ddns.sh first."; exit 1; }
# shellcheck source=/dev/null
. "$CONF"
: "${DESEC_DOMAIN:?not set}"
: "${DESEC_TOKEN:?not set}"

FQDN="${SUB}.${DESEC_DOMAIN}"
LAN_IF="$(ip route show default | awk '/default/{print $5; exit}')"
LAN_IP="$(ip -o -f inet addr show "$LAN_IF" | awk '{print $4; exit}' | cut -d/ -f1)"
[ -n "$LAN_IP" ] || { echo "Could not determine this machine's LAN address."; exit 1; }

echo "== plan =="
echo "   certificate for : $FQDN"
echo "   A record        : $FQDN -> $LAN_IP"
echo "   challenge       : DNS-01 via the deSEC API (no inbound ports)"
echo

echo "== packages =="
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq certbot dnsutils

echo "== A record =="
BASE="https://desec.io/api/v1/domains/${DESEC_DOMAIN}/rrsets"
BODY="{\"subname\":\"${SUB}\",\"type\":\"A\",\"ttl\":3600,\"records\":[\"${LAN_IP}\"]}"

# mktemp, never a fixed name in /tmp. fs.protected_regular=2 stops even root
# opening another user's file for write in a world-writable sticky directory,
# so a predictable path breaks the moment anyone else has used the same name.
RESP="$(mktemp)"
trap 'rm -f "$RESP"' EXIT

code=$(curl -4 -sS -o "$RESP" -w '%{http_code}' -X PUT "${BASE}/${SUB}/A/" \
  -H "Authorization: Token ${DESEC_TOKEN}" -H "Content-Type: application/json" -d "$BODY")
if [ "$code" = "404" ]; then
    code=$(curl -4 -sS -o "$RESP" -w '%{http_code}' -X POST "${BASE}/" \
      -H "Authorization: Token ${DESEC_TOKEN}" -H "Content-Type: application/json" -d "$BODY")
fi
case "$code" in
  200|201) echo "   $FQDN -> $LAN_IP (HTTP $code)" ;;
  *) echo "   FAILED (HTTP $code):"; cat "$RESP"; echo; exit 1 ;;
esac

echo "== challenge hooks =="
cat > /usr/local/sbin/desec-dns-auth <<'AUTH_EOF'
#!/bin/bash
# certbot manual auth hook: publish the DNS-01 TXT record through deSEC.
set -euo pipefail
. /etc/desec-ddns.conf
FULL="_acme-challenge.${CERTBOT_DOMAIN}"
SUBNAME="${FULL%.${DESEC_DOMAIN}}"
BASE="https://desec.io/api/v1/domains/${DESEC_DOMAIN}/rrsets"
BODY="{\"subname\":\"${SUBNAME}\",\"type\":\"TXT\",\"ttl\":3600,\"records\":[\"\\\"${CERTBOT_VALIDATION}\\\"\"]}"

# PUT replaces an existing record, POST creates a new one. Try both so this
# works on a first issue and on every renewal thereafter.
code=$(curl -4 -sS -o /dev/null -w '%{http_code}' -X PUT "${BASE}/${SUBNAME}/TXT/" \
  -H "Authorization: Token ${DESEC_TOKEN}" -H "Content-Type: application/json" -d "$BODY")
if [ "$code" = "404" ]; then
    code=$(curl -4 -sS -o /dev/null -w '%{http_code}' -X POST "${BASE}/" \
      -H "Authorization: Token ${DESEC_TOKEN}" -H "Content-Type: application/json" -d "$BODY")
fi
case "$code" in
  200|201) : ;;
  *) echo "deSEC rejected the challenge record (HTTP ${code})" >&2; exit 1 ;;
esac

# Wait until deSEC's own nameserver serves it, rather than guessing a delay.
for _ in $(seq 1 30); do
    if dig +short "@ns1.desec.io" TXT "${FULL}" 2>/dev/null | grep -qF "${CERTBOT_VALIDATION}"; then
        sleep 5      # small buffer for the secondary nameserver
        exit 0
    fi
    sleep 5
done
echo "TXT record for ${FULL} did not appear on deSEC within 150s" >&2
exit 1
AUTH_EOF

cat > /usr/local/sbin/desec-dns-cleanup <<'CLEANUP_EOF'
#!/bin/bash
# certbot manual cleanup hook: remove the DNS-01 TXT record.
set -euo pipefail
. /etc/desec-ddns.conf
FULL="_acme-challenge.${CERTBOT_DOMAIN}"
SUBNAME="${FULL%.${DESEC_DOMAIN}}"
curl -4 -sS -o /dev/null -X DELETE \
  "https://desec.io/api/v1/domains/${DESEC_DOMAIN}/rrsets/${SUBNAME}/TXT/" \
  -H "Authorization: Token ${DESEC_TOKEN}" || true
CLEANUP_EOF

chmod 700 /usr/local/sbin/desec-dns-auth /usr/local/sbin/desec-dns-cleanup
echo "   installed"

echo "== deploy hook (runs after every issue and renewal) =="
mkdir -p /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/camera-tls <<'DEPLOY_EOF'
#!/bin/bash
# Copy the renewed certificate to where camera.service expects it, with the
# ownership it needs, then restart. Copying rather than symlinking keeps the
# private key readable by the 'camera' group without loosening /etc/letsencrypt.
set -euo pipefail
[ -n "${RENEWED_LINEAGE:-}" ] || exit 0
install -m 644 -o root -g camera "${RENEWED_LINEAGE}/fullchain.pem" /etc/camera-tls/server.crt
install -m 640 -o root -g camera "${RENEWED_LINEAGE}/privkey.pem"   /etc/camera-tls/server.key
systemctl restart camera.service
logger -t camera-tls "installed renewed certificate from ${RENEWED_LINEAGE}"
DEPLOY_EOF
chmod 700 /etc/letsencrypt/renewal-hooks/deploy/camera-tls
echo "   installed"

echo "== keeping the current certificate, in case this fails =="
cp -a "$TLS_DIR/server.crt" "$TLS_DIR/server.crt.selfsigned.bak"
cp -a "$TLS_DIR/server.key" "$TLS_DIR/server.key.selfsigned.bak"
echo "   backed up to ${TLS_DIR}/*.selfsigned.bak"

echo "== requesting the certificate =="
if [ -n "$EMAIL" ]; then
    EMAIL_ARGS=(--email "$EMAIL" --no-eff-email)
else
    EMAIL_ARGS=(--register-unsafely-without-email)
fi

if certbot certonly \
    --manual \
    --preferred-challenges dns \
    --manual-auth-hook /usr/local/sbin/desec-dns-auth \
    --manual-cleanup-hook /usr/local/sbin/desec-dns-cleanup \
    --agree-tos "${EMAIL_ARGS[@]}" \
    --non-interactive \
    --keep-until-expiring \
    -d "$FQDN"; then
    echo "   issued"
else
    echo
    echo "   certbot failed. The self-signed certificate is untouched and the"
    echo "   service is still running on it. Nothing to undo."
    exit 1
fi

echo "== installing it =="
RENEWED_LINEAGE="/etc/letsencrypt/live/${FQDN}" \
  /etc/letsencrypt/renewal-hooks/deploy/camera-tls

echo "== renewal timer =="
systemctl enable --now certbot.timer >/dev/null 2>&1 || true
systemctl list-timers certbot.timer --no-pager 2>/dev/null | sed -n '2p' | sed 's/^/   /'

echo
echo "== verifying =="
sleep 6
openssl x509 -in "${TLS_DIR}/server.crt" -noout -issuer -subject -dates | sed 's/^/   /'
echo
echo "-----------------------------------------------------------------"
echo "Done. Use this address from now on:"
echo
echo "    https://${FQDN}:5000"
echo
echo "It resolves to ${LAN_IP}, so it works on the LAN and over WireGuard."
echo "The old https://${LAN_IP}:5000 still works but shows the warning,"
echo "because the certificate is issued for the name, not the address."
echo "-----------------------------------------------------------------"
