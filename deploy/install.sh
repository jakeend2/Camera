#!/bin/bash
#
# Install the camera service on a fresh Raspberry Pi OS (bookworm or later).
#
#   sudo git clone https://github.com/jakeend2/Camera.git /opt/camera
#   sudo /opt/camera/deploy/install.sh
#
# Safe to re-run: every step checks whether it is already done, and existing
# credentials are never regenerated - a second run will not invalidate a
# working install.
#
# Deliberately NOT covered, because both can lock you out of a headless
# machine and deserve a human watching:
#   * the firewall (ufw)
#   * sudo hardening
#   * dynamic DNS and WireGuard - they need an external account and router
#     access, so they are separate opt-in scripts
# All four are documented in deploy/README.md.
#
# Options:
#   --dry-run          report what would change, touch nothing
#   --video PATH       skip capture-device detection
#   --serial PATH      skip serial-device detection
#   --password PASS    set the web password non-interactively
set -euo pipefail

INSTALL_DIR=/opt/camera
SERVICE_USER=camera
ENV_FILE=/etc/camera-service.env
TLS_DIR=/etc/camera-tls
DRY_RUN=0
VIDEO_OVERRIDE=""
SERIAL_OVERRIDE=""
WEB_PASSWORD=""

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)  DRY_RUN=1; shift ;;
        --video)    VIDEO_OVERRIDE="$2"; shift 2 ;;
        --serial)   SERIAL_OVERRIDE="$2"; shift 2 ;;
        --password) WEB_PASSWORD="$2"; shift 2 ;;
        -h|--help)  sed -n '2,25p' "$0"; exit 0 ;;
        *)          echo "Unknown option: $1"; exit 1 ;;
    esac
done

say()  { printf '\n== %s ==\n' "$*"; }
info() { printf '   %s\n' "$*"; }
warn() { printf '   ! %s\n' "$*" >&2; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY_RUN" -eq 1 ]; then printf '   would: %s\n' "$*"; else "$@"; fi; }

[ "$(id -u)" -eq 0 ] || die "Run with sudo."
[ "$DRY_RUN" -eq 1 ] && echo "DRY RUN - nothing will be changed."

# ---------------------------------------------------------------------------
say "Checking the environment"
# ---------------------------------------------------------------------------
[ -d "$INSTALL_DIR" ] || die "$INSTALL_DIR does not exist. Clone the repository there first:
  sudo git clone https://github.com/jakeend2/Camera.git $INSTALL_DIR"
[ -f "$INSTALL_DIR/camera_service.py" ] || die "$INSTALL_DIR does not look like this repository."

. /etc/os-release 2>/dev/null || die "Cannot read /etc/os-release."
info "OS: ${PRETTY_NAME:-unknown}  arch: $(uname -m)  kernel: $(uname -r)"
case "${VERSION_CODENAME:-}" in
    bookworm|trixie|"") : ;;
    *) warn "Untested on '${VERSION_CODENAME}'. Continuing." ;;
esac

LAN_IF="$(ip route show default | awk '/default/{print $5; exit}')"
[ -n "$LAN_IF" ] || die "No default route - is the network up?"
LAN_CIDR="$(ip -o -f inet addr show "$LAN_IF" | awk '{print $4; exit}')"
LAN_IP="${LAN_CIDR%%/*}"
LAN_SUBNET="$(python3 -c 'import ipaddress,sys; print(ipaddress.ip_network(sys.argv[1], strict=False))' "$LAN_CIDR")"
info "Network: ${LAN_IF} ${LAN_IP} on ${LAN_SUBNET}"

TZ_NOW="$(timedatectl show -p Timezone --value 2>/dev/null || echo unknown)"
info "Timezone: ${TZ_NOW}"
case "$TZ_NOW" in
    Etc/UTC|UTC|unknown)
        warn "Daily recordings are cut at this clock's midnight. If that is not"
        warn "your local midnight, fix it: sudo timedatectl set-timezone <Area/City>" ;;
esac

# ---------------------------------------------------------------------------
say "Installing packages"
# ---------------------------------------------------------------------------
PKGS=(ffmpeg fonts-dejavu-core mosquitto mosquitto-clients python3-venv v4l-utils openssl curl)
MISSING=()
for p in "${PKGS[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || MISSING+=("$p")
done
if [ ${#MISSING[@]} -eq 0 ]; then
    info "All present."
else
    info "Installing: ${MISSING[*]}"
    run apt-get update -qq
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${MISSING[@]}"
fi

# ---------------------------------------------------------------------------
say "Detecting hardware"
# ---------------------------------------------------------------------------
# /dev/v4l/by-id contains only USB capture devices; the Pi's own codec nodes
# appear under by-path, so they cannot be picked up by mistake here.
if [ -n "$VIDEO_OVERRIDE" ]; then
    VIDEO_DEVICE="$VIDEO_OVERRIDE"
    info "Capture device (given): $VIDEO_DEVICE"
else
    mapfile -t VIDEOS < <(ls /dev/v4l/by-id/*-video-index0 2>/dev/null || true)
    case ${#VIDEOS[@]} in
        0) die "No USB capture device found under /dev/v4l/by-id.
   Plug the dongle in, or pass --video /dev/videoN" ;;
        1) VIDEO_DEVICE="${VIDEOS[0]}"; info "Capture device: $VIDEO_DEVICE" ;;
        *) printf '   %s\n' "${VIDEOS[@]}" >&2
           die "More than one capture device. Choose with --video <path>" ;;
    esac
fi

# A Z-Wave stick or any other USB-serial adapter shows up here too, so an
# ambiguous match must stop rather than guess - picking wrong would send
# Pelco-D frames into the wrong radio.
if [ -n "$SERIAL_OVERRIDE" ]; then
    SERIAL_PORT="$SERIAL_OVERRIDE"
    info "Serial adapter (given): $SERIAL_PORT"
else
    mapfile -t SERIALS < <(ls /dev/serial/by-id/* 2>/dev/null || true)
    case ${#SERIALS[@]} in
        0) warn "No USB-serial adapter found. PTZ will be unavailable until one"
           warn "is present; the service will still record. Pass --serial later."
           SERIAL_PORT="" ;;
        1) SERIAL_PORT="${SERIALS[0]}"; info "Serial adapter: $SERIAL_PORT" ;;
        *) printf '   %s\n' "${SERIALS[@]}" >&2
           die "More than one USB-serial adapter. Choose with --serial <path>" ;;
    esac
fi

# ---------------------------------------------------------------------------
say "Service account"
# ---------------------------------------------------------------------------
if id "$SERVICE_USER" >/dev/null 2>&1; then
    info "User '$SERVICE_USER' exists."
else
    info "Creating system user '$SERVICE_USER' (no shell, no sudo)."
    run useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
run usermod -aG video,plugdev,dialout "$SERVICE_USER"

# ---------------------------------------------------------------------------
say "Python environment"
# ---------------------------------------------------------------------------
if [ -x "$INSTALL_DIR/venv/bin/python" ]; then
    info "venv present."
else
    info "Creating venv."
    run python3 -m venv "$INSTALL_DIR/venv"
fi
info "Installing requirements."
run "$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
run "$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

# ---------------------------------------------------------------------------
say "Directories and ownership"
# ---------------------------------------------------------------------------
# pi (or whoever owns the checkout) edits; the service account only writes
# recordings and logs.
OWNER="$(stat -c %U "$INSTALL_DIR")"
[ "$OWNER" = "root" ] && OWNER="pi"
run install -d -o "$OWNER" -g "$SERVICE_USER" -m 2775 "$INSTALL_DIR/videos" "$INSTALL_DIR/logs"
run chown -R "$OWNER:$SERVICE_USER" "$INSTALL_DIR"
run chmod -R g+rX "$INSTALL_DIR"
run chmod -R g+w "$INSTALL_DIR/videos" "$INSTALL_DIR/logs"
info "Owner ${OWNER}, group ${SERVICE_USER}; videos/ and logs/ are setgid."

# ---------------------------------------------------------------------------
say "Credentials"
# ---------------------------------------------------------------------------
# Written once. Re-running never regenerates a value that already exists,
# so an accidental second run cannot lock you out of a working install.
env_has() { [ -f "$ENV_FILE" ] && grep -qE "^$1=" "$ENV_FILE"; }
env_add() {
    if env_has "$1"; then
        info "$1 already set, keeping it."
    elif [ "$DRY_RUN" -eq 1 ]; then
        info "would add $1"
    else
        printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
        info "$1 written."
    fi
}

if [ "$DRY_RUN" -eq 0 ]; then
    touch "$ENV_FILE"; chmod 640 "$ENV_FILE"; chown root:"$SERVICE_USER" "$ENV_FILE"
fi

MQTT_PW="$(openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | head -c 28)"
env_add MQTT_HOST 127.0.0.1
env_add MQTT_PORT 1883
env_add MQTT_USERNAME camera
env_add MQTT_PASSWORD "$MQTT_PW"
env_add FLASK_SECRET_KEY "$(openssl rand -hex 32)"
env_add WEB_USERNAME admin

# Detected hardware, so nothing host-specific stays in the source.
env_add VIDEO_DEVICE "$VIDEO_DEVICE"
[ -n "$SERIAL_PORT" ] && env_add SERIAL_PORT "$SERIAL_PORT"

GENERATED_PW=""
if ! env_has WEB_PASSWORD_HASH; then
    if [ -z "$WEB_PASSWORD" ]; then
        GENERATED_PW="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 20)"
        WEB_PASSWORD="$GENERATED_PW"
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        info "would generate WEB_PASSWORD_HASH"
    else
        HASH="$(P="$WEB_PASSWORD" "$INSTALL_DIR/venv/bin/python" -c \
            'import os; from werkzeug.security import generate_password_hash; print(generate_password_hash(os.environ["P"]))')"
        printf "WEB_PASSWORD_HASH='%s'\n" "$HASH" >> "$ENV_FILE"
        info "WEB_PASSWORD_HASH written."
    fi
else
    info "WEB_PASSWORD_HASH already set, keeping it."
fi

# ---------------------------------------------------------------------------
say "TLS certificate"
# ---------------------------------------------------------------------------
if [ -f "$TLS_DIR/server.crt" ]; then
    info "Certificate exists; leaving it. Regenerate with deploy/make-cert.sh"
    info "if this machine's address has changed."
else
    run "$INSTALL_DIR/deploy/make-cert.sh" "$LAN_IP"
fi
[ "$DRY_RUN" -eq 0 ] && [ -f "$TLS_DIR/server.key" ] && \
    { chown root:"$SERVICE_USER" "$TLS_DIR/server.key"; chmod 640 "$TLS_DIR/server.key"; }

# ---------------------------------------------------------------------------
say "MQTT broker"
# ---------------------------------------------------------------------------
run install -m 644 "$INSTALL_DIR/deploy/mosquitto-local.conf" /etc/mosquitto/conf.d/local.conf
run install -m 640 -o root -g mosquitto "$INSTALL_DIR/deploy/mosquitto-aclfile" /etc/mosquitto/aclfile
if [ -f /etc/mosquitto/passwd ] && grep -q '^camera:' /etc/mosquitto/passwd 2>/dev/null; then
    info "MQTT user 'camera' exists; password left as-is."
    warn "If MQTT_PASSWORD was regenerated the two will not match - they were"
    warn "both created on the first run, so this only matters if you edited one."
elif [ "$DRY_RUN" -eq 1 ]; then
    info "would create MQTT user 'camera'"
else
    ACTUAL_PW="$(grep -E '^MQTT_PASSWORD=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
    mosquitto_passwd -c -b /etc/mosquitto/passwd camera "$ACTUAL_PW"
    chown root:mosquitto /etc/mosquitto/passwd; chmod 640 /etc/mosquitto/passwd
    info "MQTT user 'camera' created."
fi
run systemctl enable --now mosquitto
run systemctl restart mosquitto

# ---------------------------------------------------------------------------
say "Service"
# ---------------------------------------------------------------------------
run install -m 644 "$INSTALL_DIR/deploy/camera.service" /etc/systemd/system/camera.service
run systemctl daemon-reload
run systemctl enable camera.service
run systemctl restart camera.service

# ---------------------------------------------------------------------------
say "Verifying"
# ---------------------------------------------------------------------------
if [ "$DRY_RUN" -eq 1 ]; then
    info "skipped in dry run"
else
    for _ in $(seq 1 20); do
        systemctl is-active --quiet camera.service && break
        sleep 2
    done
    systemctl is-active --quiet camera.service \
        || die "Service did not start. Look at: journalctl -u camera.service -n 50"
    info "camera.service active"

    code="$(curl -k -s -o /dev/null -w '%{http_code}' "https://127.0.0.1:5000/health" || echo 000)"
    case "$code" in
        401) info "HTTPS responding (401 = login required, correct)" ;;
        200) info "HTTPS responding" ;;
        *)   warn "Unexpected status from /health: $code" ;;
    esac

    sleep 12
    TODAY="$INSTALL_DIR/videos/$(date +%F).ts"
    if [ -s "$TODAY" ]; then
        info "Recording to $(basename "$TODAY") ($(stat -c%s "$TODAY") bytes so far)"
    else
        warn "No recording yet - check journalctl -u camera.service"
    fi
fi

# ---------------------------------------------------------------------------
cat <<EOF

------------------------------------------------------------------
Done.

  Web UI    https://${LAN_IP}:5000
  Username  admin
EOF
[ -n "$GENERATED_PW" ] && cat <<EOF
  Password  ${GENERATED_PW}

  ^ generated, shown once. Change it with:
      sudo ${INSTALL_DIR}/deploy/set-web-password.sh
EOF
cat <<EOF

Your browser will warn about the self-signed certificate. Import
${TLS_DIR}/server.crt to silence it.

Not done by this script, on purpose - see deploy/README.md:
  * firewall (ufw)          - can lock you out of a headless box
  * sudo hardening          - same
  * dynamic DNS + WireGuard - need an account and router access:
      sudo ${INSTALL_DIR}/deploy/setup-desec-ddns.sh
      sudo ${INSTALL_DIR}/deploy/setup-wireguard.sh <hostname>
------------------------------------------------------------------
EOF
