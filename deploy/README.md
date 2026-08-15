# Deployment

Everything needed to stand this system up on a fresh Raspberry Pi, plus the
router changes that cannot be scripted from the Pi itself.

Scripts here are meant to be **read before they are run**. They all need root,
which the camera service account deliberately does not have, so each one is
something you invoke with `sudo` rather than something that happens by itself.

---

## What is running

```
        OUTSIDE                    THE PI (192.168.1.125)
  ┌──────────────┐        ┌────────────────────────────────┐
  │  Bosch       │        │  camera.service                │
  │  MIC 612     │        │    └─ ffmpeg  capture, encode,  │
  │   ├ video ───┼──USB───┼──────  daily segments, preview  │
  │   └ RS-485 ──┼──USB───┼──────  Pelco-D over serial      │
  └──────────────┘        │    └─ Flask   web UI + PTZ      │
                          │                                │
   phone (cellular)       │  mosquitto   MQTT broker        │
        │                 │  wg0         VPN 10.8.0.0/24    │
        └─── WireGuard ───┼─ ufw         default deny in    │
             UDP 51820    │  desec-ddns  tracks public IP   │
                          └────────────────────────────────┘
```

| | |
|---|---|
| Project root | `/opt/camera` — owned `pi:camera`; **pi** edits, **camera** runs |
| Service account | `camera`, system user, `nologin`, no sudo |
| Web UI | `https://192.168.1.125:5000`, user `admin` |
| Recordings | `/opt/camera/videos/YYYY-MM-DD.ts`, 14-day retention |
| Timezone | `America/New_York` — this defines when a "day" ends |

The project lives in `/opt`, not a home directory, because `/home/pi` is mode
`700` and an unprivileged service account cannot traverse into it.

---

## Quick path: the installer

For a fresh machine, `install.sh` does steps 1-6 below in one go - packages,
service account, venv, credentials, TLS, broker, systemd - detecting the
capture dongle, serial adapter and LAN subnet rather than assuming them.

```bash
sudo git clone https://github.com/jakeend2/Camera.git /opt/camera
sudo /opt/camera/deploy/install.sh
```

It is safe to re-run: every step checks whether it is already done, and
**existing credentials are never regenerated**, so a second run cannot lock
you out of a working install. `--dry-run` reports what it would change
without touching anything.

It deliberately leaves out the firewall and sudo hardening - both can strand
you on a headless machine - and dynamic DNS and WireGuard, which need an
external account and router access. Those are steps 7-9 and the hardening
section, still done by hand.

The manual steps below remain the reference for what the installer does, and
for anything you need to redo piecemeal.

---

## Order of operations

Steps 1–6 are the base system. Steps 7–9 add remote access and are optional
if you only ever want LAN access.

Later steps depend on earlier ones — in particular the WireGuard client
configs embed the DNS name from step 7, so DDNS must work first.

---

### 1. Base packages and the service account

Not scripted; these run once on a fresh machine.

```bash
sudo apt-get update && sudo apt-get install -y ffmpeg fonts-dejavu-core mosquitto mosquitto-clients ufw
sudo useradd --system --home-dir /opt/camera --shell /usr/sbin/nologin camera
sudo usermod -aG video,plugdev,dialout camera
```

The three groups are what let the service reach `/dev/video0` (video),
`/dev/ttyUSB0` (plugdev on this image) and serial devices generally (dialout).

### 2. The application

```bash
sudo git clone https://github.com/jakeend2/Camera.git /opt/camera
cd /opt/camera
sudo -u pi python3 -m venv venv
sudo -u pi venv/bin/pip install -r requirements.txt
sudo chown -R pi:camera /opt/camera
sudo chmod -R g+rX /opt/camera
sudo chmod -R g+w videos logs
sudo chmod g+s videos logs          # new files inherit the camera group
```

`ffmpeg` is a system package, not a Python dependency — the media pipeline is
a subprocess, and nothing in the service imports OpenCV.

### 3. Credentials

Deliberately outside the repository. Create `/etc/camera-service.env`:

```
MQTT_HOST=127.0.0.1
MQTT_PORT=1883
MQTT_USERNAME=camera
MQTT_PASSWORD=<generated>
FLASK_SECRET_KEY=<32 random hex bytes>
WEB_USERNAME=admin
WEB_PASSWORD_HASH='<scrypt hash>'
```

```bash
sudo chown root:camera /etc/camera-service.env && sudo chmod 640 /etc/camera-service.env
```

Generate the pieces with:

```bash
openssl rand -hex 32                                    # FLASK_SECRET_KEY
/opt/camera/venv/bin/python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('yourpassword'))"
```

Quote the hash in the env file — it contains `$` characters.

Change the web password later with **`set-web-password.sh`**, which prompts
without echo, writes only the hash, and restarts the service.

### 4. TLS certificate

```bash
sudo /opt/camera/deploy/make-cert.sh            # or: make-cert.sh 192.168.1.125
```

Writes a self-signed cert to `/etc/camera-tls/` with the Pi's hostnames and
LAN address as SANs. **Re-run it if the Pi's address changes** — browsers
reject a certificate whose SAN does not match.

Import `/etc/camera-tls/server.crt` into your OS trust store to stop the
browser warning.

There is deliberately **no HSTS header**. With a self-signed certificate, a
pinned HSTS policy removes the browser's "proceed anyway" option and would
lock you out of your own camera.

### 5. MQTT broker

Copy the two config files from here:

```bash
sudo cp /opt/camera/deploy/mosquitto-local.conf /etc/mosquitto/conf.d/local.conf
sudo cp /opt/camera/deploy/mosquitto-aclfile   /etc/mosquitto/aclfile
sudo chown root:mosquitto /etc/mosquitto/aclfile && sudo chmod 640 /etc/mosquitto/aclfile
sudo mosquitto_passwd -c /etc/mosquitto/passwd camera
sudo systemctl restart mosquitto
```

`local.conf` sets `allow_anonymous false` and binds `0.0.0.0` so devices
elsewhere in the house can reach it. `aclfile` confines each account to its
own topic prefix, so a compromised garage controller cannot publish camera
commands.

Do not repeat `persistence`, `persistence_location` or `log_dest` in
`local.conf` — they are already set in `/etc/mosquitto/mosquitto.conf`, and
Mosquitto treats a duplicate as a fatal error.

Add accounts for new hardware as it arrives:

```bash
sudo mosquitto_passwd -b /etc/mosquitto/passwd ratgdo '<password>'
sudo systemctl restart mosquitto
```

The ACL entries for `ratgdo` and `zwave` are already written and activate as
soon as those accounts exist.

### 6. The service

```bash
sudo cp /opt/camera/deploy/camera.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now camera.service
```

Runs as `camera` with `ProtectSystem=strict`; only `videos/` and `logs/` are
writable. `Restart=always` covers crashes, and `enable` covers reboots.

Verify:

```bash
curl -k https://127.0.0.1:5000/health      # 401 until you log in - that is correct
journalctl -u camera.service -n 30
```

### 7. Dynamic DNS

Needed because the ISP address is dynamic and the router has no DDNS client.
The updater runs on the Pi instead — it is always on, which is the only
requirement.

Create a free account and a `*.dedyn.io` hostname at **https://desec.io/**,
then note the token it issues.

```bash
sudo /opt/camera/deploy/setup-desec-ddns.sh
```

Prompts for the hostname and token (hidden), installs the updater to
`/usr/local/sbin/`, writes credentials root-only to `/etc/desec-ddns.conf`,
and schedules a systemd timer every 5 minutes. **It tests the update before
scheduling anything**, so a bad token fails immediately.

deSEC takes the source address of the request as the new record value, so no
"what is my IP" service is involved. The request is forced over IPv4 with
`curl -4` — this Pi also has a public IPv6 address, and left alone curl would
update only the AAAA record while the A record the port forward needs went
stale.

Check it:

```bash
ssh pi@192.168.1.125 "curl -4 -s https://api.ipify.org; echo; getent hosts yourname.dedyn.io"
```

Both lines must show the same address.

### 8. Router — the part that cannot be scripted

Two changes on the router's admin page:

**a) DHCP reservation** — pin the Pi to `192.168.1.125`. Its MAC is
`dc:a6:32:68:af:bb`. Without this, a lease change breaks the TLS certificate
and the port forward at the same time.

**b) Port forward** — **UDP 51820 → 192.168.1.125**.

> UDP, not TCP. WireGuard is UDP-only and a TCP rule does nothing at all.
> This is the single most common reason the tunnel never handshakes.

On AT&T gateways this lives under **Firewall → NAT/Gaming** rather than a menu
called "port forwarding", and may require defining a custom service for
UDP 51820 before assigning it to the Pi.

Nothing else is forwarded. The web UI is never exposed to the internet.

### 9. WireGuard

```bash
sudo /opt/camera/deploy/setup-wireguard.sh yourname.dedyn.io
```

Installs WireGuard, generates the server key, writes `/etc/wireguard/wg0.conf`,
enables IP forwarding, opens the one UDP port, extends the existing service
rules to the VPN subnet, adds a masquerade rule, and starts the tunnel.

The LAN interface is detected from the default route rather than assumed to be
`eth0` — **this Pi is on `wlan0`**, and most guides hardcode `eth0`, which
silently breaks access to everything except the Pi itself.

Then add a device:

```bash
sudo /opt/camera/deploy/wireguard-add-client.sh phone
```

Prints the config and a QR code. Scan it with the official WireGuard app.
Each client gets its own keypair plus a preshared key. **The private key is
shown once and stored nowhere** — if you lose it, issue a new client.

Clients are **split tunnel**: only `192.168.1.0/24` and `10.8.0.0/24` route
through the VPN, so ordinary browsing is untouched.

To test properly, **turn WiFi off on the phone** so you are genuinely on
cellular, connect, then browse to `https://192.168.1.125:5000` — the LAN
address, because you are now inside the network.

Revoke a client by deleting its `# client: <name>` block from
`/etc/wireguard/wg0.conf` and running:

```bash
sudo wg syncconf wg0 <(wg-quick strip wg0)
```

---

## Host hardening

Applied once, not scripted:

```bash
# security updates only, no surprise reboots on a machine that is recording
sudo apt-get install -y unattended-upgrades
sudo cp /opt/camera/deploy/apt-20auto-upgrades.conf /etc/apt/apt.conf.d/20auto-upgrades
sudo cp /opt/camera/deploy/unattended-upgrades-camera.conf /etc/apt/apt.conf.d/52unattended-upgrades-camera

# firewall: default deny inbound, LAN-only services
sudo ufw default deny incoming && sudo ufw default allow outgoing
sudo ufw allow from 192.168.1.0/24 to any port 22 proto tcp
sudo ufw allow from 192.168.1.0/24 to any port 5000 proto tcp
sudo ufw allow from 192.168.1.0/24 to any port 1883 proto tcp
sudo ufw allow from 192.168.1.0/24 to any port 5353 proto udp
sudo ufw enable
```

> Add the SSH rule **before** enabling ufw, or you will lock yourself out of a
> headless machine.

`pi` requires a password for sudo. `/etc/sudoers.d/010_pi-nopasswd` keeps a
narrow passwordless exception for starting and stopping `camera.service` and
`mosquitto.service` — exact paths, no wildcards, and `systemctl` offers no
shell escape. `journalctl` is deliberately absent: `pi` is in the `adm` group
and can already read the journal unprivileged, and its pager can spawn a shell.

---

## Where the secrets live

None of these are in the repository, by design.

| File | Contents | Mode |
|---|---|---|
| `/etc/camera-service.env` | MQTT + web credentials, Flask secret | `640 root:camera` |
| `/etc/camera-tls/server.key` | TLS private key | `640 root:camera` |
| `/etc/mosquitto/passwd` | MQTT password hashes | `640 root:mosquitto` |
| `/etc/desec-ddns.conf` | deSEC token | `600 root:root` |
| `/etc/wireguard/wg0.conf` | server key and peers | `600 root:root` |

**Back these up off the Pi.** The code is safe in git; these are not, and
losing them means rebuilding every credential from scratch.

```bash
sudo tar czf ~/camera-config-backup-$(date +%F).tar.gz \
  /etc/camera-service.env /etc/camera-tls /etc/mosquitto/passwd \
  /etc/mosquitto/aclfile /etc/mosquitto/conf.d/local.conf \
  /etc/desec-ddns.conf /etc/wireguard /etc/systemd/system/camera.service
```

---

## Checking on things

```bash
systemctl status camera.service mosquitto wg-quick@wg0
journalctl -u camera.service -f
sudo wg show                                   # peers and handshakes
sudo ufw status verbose
systemctl list-timers desec-ddns.timer
```

Health, once logged in: `https://192.168.1.125:5000/health`

MQTT, watching everything the camera publishes:

```bash
mosquitto_sub -h 127.0.0.1 -u camera -P '<password>' -t 'camera/#' -v
```

---

## Things that cost time to work out

Recorded so they do not have to be rediscovered.

**ffmpeg reads stdin.** Running it inside a script fed from a heredoc lets it
eat the rest of the script. Pass `-nostdin`, except where stdin is
deliberately a pipe — the recorder keeps it open so `q` can request a clean
shutdown, which SIGTERM does not give you.

**The hardware H.264 encoder is unusable here.** `h264_v4l2m2m` is roughly 3×
cheaper than libx264, but when the filter graph feeds a second output — the
browser preview — it emits a stream with no usable SPS/PPS. `ffprobe` reports
`0x0` and no frame can be extracted. Every bitstream-filter workaround was
tried. Recording uses `libx264 -preset superfast`.

**MPEG-TS, not MP4 or MKV.** TS needs no trailer, so a recording stays
playable if the service is killed or the power drops mid-write. MKV produced
undecodable files with this encoder.

**Devices are addressed by stable path, never by index.** `/dev/ttyUSB0` and
`/dev/video0` are assigned in probe order. A Z-Wave stick — most use CP210x
chips and claim `ttyUSB` — could take `ttyUSB0` after a reboot and receive
Pelco-D frames meant for the camera. The service uses
`/dev/serial/by-id/…` and `/dev/v4l/by-id/…`.

**Timezone defines the day.** Daily files are cut with
`-segment_atclocktime`, which follows the system clock. The Pi shipped set to
`Europe/London`; recordings rolled at 7pm local until it was corrected.

**Windows users:** in PowerShell, `curl` is an alias for `Invoke-WebRequest`
and will prompt for a `Uri` instead of behaving like curl. Use `curl.exe`, or
run the command on the Pi over ssh.
