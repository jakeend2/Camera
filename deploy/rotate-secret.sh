#!/bin/bash
#
# Rotate one of the machine-generated secrets, in every place it lives.
#
#   sudo deploy/rotate-secret.sh <what> [--dry-run]
#
#   mqtt-camera   broker password for the 'camera' user  (the service itself)
#   mqtt-ratgdo   broker password for the 'ratgdo' user  (the garage bridge)
#   mqtt-zwave    broker password for the 'zwave' user   (zwave-js-ui)
#   flask         the session signing key - logs everyone out
#   web           the web UI password (prompts, stores only the hash)
#   tls           self-signed TLS key + certificate   [tls <lan-ip>]
#
#   set <NAME>    prompt for a value you set elsewhere - on the camera, on
#                 the ratgdo - and store it here without echoing it. Use this
#                 rather than editing the file by hand: several deploy scripts
#                 source it, so an unquoted value with a bracket in it breaks
#                 all of them at once, which has happened.
#
# Each MQTT password lives in TWO places: the broker's own password file and
# whatever hands it to the client. Change one and not the other and the
# subsystem goes quiet without saying why - the broker just refuses the
# connection and the client retries forever. That is the whole reason this
# script exists rather than a line in the documentation.
#
# Credentials held by a device (the camera's own admin account, the ratgdo's
# UI) cannot be rotated from this machine at all - change them on the device,
# then record the new value here with `set`.
set -euo pipefail

ENV_FILE=/etc/camera-service.env
ZW_SETTINGS=/var/lib/zwave-js-ui/store/settings.json
# --dry-run is honoured wherever it appears. Testing only $2 broke the
# moment a mode grew a positional argument: `tls <ip> --dry-run` silently
# performed a REAL key rotation, which is the exact opposite of what the
# flag promises.
DRY=0
ARGS=()
for a in "$@"; do
    if [ "$a" = "--dry-run" ]; then DRY=1; else ARGS+=("$a"); fi
done
set -- ${ARGS[@]+"${ARGS[@]}"}
WHAT="${1:-}"

say()  { printf '\n== %s ==\n' "$*"; }
info() { printf '   %s\n' "$*"; }
warn() { printf '   ! %s\n' "$*" >&2; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY" -eq 1 ]; then printf '   would: %s\n' "$*"; else "$@"; fi; }

# Usage first, so asking what this does does not require root.
case "$WHAT" in
    mqtt-camera|mqtt-ratgdo|mqtt-zwave|flask|set|web|tls) : ;;
    *) awk 'NR > 1 { if (!/^#/) exit; print }' "$0"; exit 1 ;;
esac
[ "$(id -u)" -eq 0 ] || die "Run with sudo."
[ "$DRY" -eq 1 ] && echo "DRY RUN - nothing will be changed."

# Replace a value in the env file, preserving the rest of the file exactly.
env_set() {
    local key="$1" val="$2"
    if [ "$DRY" -eq 1 ]; then info "would set $key"; return; fi
    python3 - "$ENV_FILE" "$key" "$val" <<'PY'
import re, sys
path, key, val = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f:
    text = f.read()
if "'" in val:
    sys.exit("value contains a single quote, which cannot be represented "
             "safely in this file - choose another")
if val != val.strip():
    sys.exit("value has leading or trailing whitespace, which _cam_env() "
             "strips on read - it would authenticate with a different string")
# Single quotes, not double: the deploy scripts source this file, so $, a
# backtick or a backslash inside double quotes would be expanded by the shell
# into something other than what was typed.
line = "%s='%s'" % (key, val)
if re.search(r'^%s=' % re.escape(key), text, re.M):
    text = re.sub(r'^%s=.*$' % re.escape(key), line, text, flags=re.M)
else:
    if not text.endswith("\n"):
        text += "\n"
    text += line + "\n"
with open(path, "w") as f:
    f.write(text)
PY
    # The old standalone scripts re-asserted these on every write, healing
    # any drift; keep that. Group camera matches install.sh (pi is in it).
    local grp=camera
    getent group "$grp" >/dev/null || grp=pi
    chown "root:$grp" "$ENV_FILE"
    chmod 640 "$ENV_FILE"
    info "$key updated in $ENV_FILE"
}

env_get() {
    sed -n "s/^$1=//p" "$ENV_FILE" | head -1 | sed 's/^"//; s/"$//; s/^'"'"'//; s/'"'"'$//'
}

if [ "$WHAT" = "web" ]; then
    say "Web UI password"
    printf '   New password (not echoed): '
    read -rs P1; echo
    printf '   Again: '
    read -rs P2; echo
    if [ "$P1" != "$P2" ]; then die "They do not match - nothing changed."; fi
    if [ ${#P1} -lt 12 ]; then die "Use at least 12 characters."; fi
    HASH=$(P="$P1" /opt/camera/venv/bin/python -c \
        'import os; from werkzeug.security import generate_password_hash; print(generate_password_hash(os.environ["P"]))')
    unset P1 P2
    env_set WEB_PASSWORD_HASH "$HASH"
    run rm -f /etc/camera-web-initial-password
    run systemctl restart camera.service
    info "Password updated. Existing sessions stay signed in - rotate 'flask'"
    info "as well to sign everyone out."
    exit 0
fi

if [ "$WHAT" = "tls" ]; then
    IP="${2:-$(hostname -I | awk '{print $1}')}"
    DIR=/etc/camera-tls
    say "TLS key and certificate for $IP"
    # setup-letsencrypt.sh may have installed a CA-issued certificate over the
    # self-signed one. Regenerating self-signed here would silently downgrade
    # it - renewal belongs to certbot's timer, not to this script.
    if [ -f "$DIR/server.crt" ]; then
        SUBJ=$(openssl x509 -in "$DIR/server.crt" -noout -subject 2>/dev/null | sed 's/^subject=//')
        ISS=$(openssl x509 -in "$DIR/server.crt" -noout -issuer 2>/dev/null | sed 's/^issuer=//')
        if [ -n "$ISS" ] && [ "$SUBJ" != "$ISS" ]; then
            die "the installed certificate is CA-issued ($ISS).
Replacing it with a self-signed one is a downgrade. certbot renews it on its
own timer; if self-signed is really wanted, remove $DIR/server.crt first."
        fi
    fi
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
    run mkdir -p "$DIR"
    run openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$DIR/server.key" -out "$DIR/server.crt" -config "$CNF"
    rm -f "$CNF"
    # The service reads the key as User=camera; root:pi only ever worked
    # because install.sh re-chowned it afterwards.
    SVC_GROUP=camera
    getent group "$SVC_GROUP" >/dev/null || SVC_GROUP=pi
    run chown "root:$SVC_GROUP" "$DIR/server.key" "$DIR/server.crt"
    run chmod 640 "$DIR/server.key"
    run chmod 644 "$DIR/server.crt"
    if [ "$DRY" -eq 0 ]; then
        openssl x509 -in "$DIR/server.crt" -noout -subject -dates -ext subjectAltName
    fi
    # try-restart no-ops for a unit that exists but is stopped - but it FAILS
    # for a unit that does not exist at all, and during a fresh install this
    # runs before install.sh has installed the unit. Unguarded, that one exit
    # code aborted the entire fresh install under set -e.
    if systemctl cat camera.service >/dev/null 2>&1; then
        run systemctl try-restart camera.service
    else
        info "camera.service not installed yet - nothing to restart"
    fi
    info "Done."
    exit 0
fi

if [ "$WHAT" = "set" ]; then
    KEY="${2:-}"
    [ -n "$KEY" ] || die "Which setting? e.g. sudo $0 set CAM_BACKYARD_PASS"
    case "$KEY" in
        CAM_BACKYARD_PASS|RATGDO_PASS|CAM_BACKYARD_USER|RATGDO_USER) : ;;
        *) die "Refusing to set '$KEY' this way. This mode is for credentials
that live on a device: CAM_BACKYARD_PASS, RATGDO_PASS and their usernames.
The generated ones have their own modes; the web login is: sudo $0 web." ;;
    esac
    say "$KEY"
    printf '   New value (not echoed): '
    read -rs VALUE; echo
    [ -n "$VALUE" ] || die "Empty - nothing changed."
    printf '   Again: '
    read -rs VALUE2; echo
    [ "$VALUE" = "$VALUE2" ] || die "They do not match - nothing changed."
    env_set "$KEY" "$VALUE"
    run systemctl restart camera.service
    if [ "$DRY" -eq 0 ]; then
        sleep 8
        systemctl is-active --quiet camera.service             && info "camera.service active"             || warn "camera.service did not come back - journalctl -u camera.service -n 30"
    fi
    info "Done. The value was never echoed and is not in your shell history."
    exit 0
fi

NEW="$(openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | head -c 28)"

if [ "$WHAT" = "flask" ]; then
    say "Session signing key"
    warn "Every logged-in browser is signed out the moment this restarts."
    env_set FLASK_SECRET_KEY "$(openssl rand -hex 32)"
    run systemctl restart camera.service
    info "Done. Log in again."
    exit 0
fi

USER="${WHAT#mqtt-}"
say "Broker password for '$USER'"

# Order matters. Update the broker LAST for the identities this service uses,
# so there is no window where the running service holds a password the broker
# has already stopped accepting. zwave-js-ui is restarted explicitly instead.
case "$USER" in
    camera) VAR=MQTT_PASSWORD ;;
    ratgdo) VAR=RATGDO_MQTT_PASS ;;
    zwave)  VAR=ZWAVE_MQTT_PASS ;;
esac

env_set "$VAR" "$NEW"

if [ "$USER" = "zwave" ]; then
    # zwave-js-ui keeps its own copy in its own store. The env var is read by
    # the deploy scripts, NOT by the gateway - so changing only the env leaves
    # the gateway using the old password and it silently stops publishing.
    if [ -f "$ZW_SETTINGS" ]; then
        run cp -a "$ZW_SETTINGS" "${ZW_SETTINGS}.bak"
        if [ "$DRY" -eq 0 ]; then
            python3 - "$ZW_SETTINGS" "$NEW" <<'PY'
import json, sys
path, pw = sys.argv[1], sys.argv[2]
with open(path) as f:
    cfg = json.load(f)
found = []
def walk(node, trail=""):
    if isinstance(node, dict):
        for k, v in node.items():
            if k.lower() == "password" and isinstance(v, str):
                node[k] = pw
                found.append(trail + "/" + k)
            else:
                walk(v, trail + "/" + k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            walk(v, "%s[%d]" % (trail, i))
walk(cfg.get("mqtt", {}), "mqtt")
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print("   zwave-js-ui settings updated: %s" % (", ".join(found) or "NO password field found"))
PY
        else
            info "would update the mqtt password inside $ZW_SETTINGS"
        fi
    else
        warn "$ZW_SETTINGS not found - set the broker password in the"
        warn "zwave-js-ui UI (Settings -> MQTT) yourself, or it will stop publishing."
    fi
fi

# Not through run(): its dry-run echo would print the candidate password,
# and a rotation tool has no business printing secrets in any mode.
if [ "$DRY" -eq 1 ]; then
    info "would set the broker password for '$USER' (value not shown)"
else
    mosquitto_passwd -b /etc/mosquitto/passwd "$USER" "$NEW"
fi
run chown root:mosquitto /etc/mosquitto/passwd
run chmod 640 /etc/mosquitto/passwd
run systemctl reload mosquitto

case "$USER" in
    camera|ratgdo) run systemctl restart camera.service ;;
    zwave)         run systemctl restart zwave-js-ui ;;
esac

if [ "$DRY" -eq 1 ]; then
    echo; info "dry run complete"; exit 0
fi

say "Verifying"
sleep 6
ERR="$(mosquitto_sub -h 127.0.0.1 -u "$USER" -P "$NEW" -t 'nothing/at/all' -W 2 2>&1 || true)"
case "$ERR" in
    *"not authorised"*|*"Connection Refused"*|*"refused"*)
        die "The broker rejected the new password. /etc/mosquitto/passwd and
$ENV_FILE may now disagree - re-run this script to set them together again." ;;
    *) info "broker accepts the new password for '$USER'" ;;
esac

for unit in camera.service zwave-js-ui; do
    systemctl is-active --quiet "$unit" 2>/dev/null && info "$unit active" || true
done
info "Done."
