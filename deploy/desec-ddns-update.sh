#!/bin/bash
#
# Point a deSEC.io dynamic DNS record at this machine's current public IPv4.
#
# deSEC takes the source address of this request as the new value, so there is
# no need to ask a third party "what is my IP" first - one fewer service to
# trust, and one fewer thing to break.
#
# Credentials come from /etc/desec-ddns.conf, which is root-only and outside
# the repository. Installed by setup-desec-ddns.sh; run by a systemd timer.
set -euo pipefail

CONF=/etc/desec-ddns.conf
[ -r "$CONF" ] || { echo "Missing $CONF" >&2; exit 1; }
# shellcheck source=/dev/null
. "$CONF"
: "${DESEC_DOMAIN:?DESEC_DOMAIN not set}"
: "${DESEC_TOKEN:?DESEC_TOKEN not set}"

# -4 forces an IPv4 connection so the A record is what gets updated. This host
# also has a public IPv6 address, and without this curl would likely connect
# over v6 and leave the A record - the one the port forward needs - stale.
#
# myipv6=preserve leaves any AAAA record alone rather than clearing it.
response=$(curl -4 -sS --max-time 30 \
    --user "${DESEC_DOMAIN}:${DESEC_TOKEN}" \
    "https://update.dedyn.io/?myipv6=preserve" 2>&1) || {
        echo "update failed: ${response}" >&2
        exit 1
    }

case "$response" in
    good*|nochg*)
        echo "${DESEC_DOMAIN}: ${response}"
        ;;
    *)
        echo "unexpected response for ${DESEC_DOMAIN}: ${response}" >&2
        exit 1
        ;;
esac
