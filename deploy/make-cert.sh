#!/bin/bash
# Regenerate the self-signed TLS certificate for the web UI.
#
# Run this if the Pi's LAN address changes or the certificate expires - the
# address is baked into the cert as a SAN, and browsers reject a mismatch.
set -euo pipefail

IP="${1:-$(hostname -I | awk '{print $1}')}"
DIR=/etc/camera-tls
echo "Issuing certificate for ${IP} and $(hostname)"

CNF=$(mktemp)
cat > "$CNF" <<EOF
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = $(hostname)
O = Camera Service
[v3]
basicConstraints = critical, CA:TRUE
keyUsage = critical, digitalSignature, keyEncipherment, keyCertSign
extendedKeyUsage = serverAuth
subjectAltName = @alt
[alt]
DNS.1 = $(hostname)
DNS.2 = $(hostname).local
DNS.3 = localhost
IP.1  = ${IP}
IP.2  = 127.0.0.1
EOF

sudo mkdir -p "$DIR"
sudo openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout "$DIR/server.key" -out "$DIR/server.crt" -config "$CNF"
rm -f "$CNF"

sudo chown root:pi "$DIR/server.key" "$DIR/server.crt"
sudo chmod 640 "$DIR/server.key"
sudo chmod 644 "$DIR/server.crt"

openssl x509 -in "$DIR/server.crt" -noout -subject -dates -ext subjectAltName
echo "Restart the service to pick it up:  sudo systemctl restart camera.service"
