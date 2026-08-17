# Deployment

Everything needed to stand this system up on a fresh Raspberry Pi, plus the
router changes that cannot be scripted from the Pi itself.

Scripts here are meant to be **read before they are run**. They all need root,
which the camera service account deliberately does not have, so each one is
something you invoke with `sudo` rather than something that happens by itself.

---

## What is running

```
        OUTSIDE                    THE PI (192.168.1.77)
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
| Web UI | `https://camera.<domain>:5000` (LAN `192.168.1.77`), user `admin` |
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
sudo /opt/camera/deploy/make-cert.sh            # or: make-cert.sh 192.168.1.77
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
ssh pi@192.168.1.77 "curl -4 -s https://api.ipify.org; echo; getent hosts yourname.dedyn.io"
```

Both lines must show the same address.

## When the Pi's address changes

Moving from WiFi to the PoE switch changed the Pi from `192.168.1.125` to
`192.168.1.77`, and **five separate things were pinned to the old address**.
The tunnel itself never broke; everything downstream of it did. In the order
they bite:

| What | Where | Symptom when stale |
|---|---|---|
| dnsmasq `host-record` | `/etc/dnsmasq.d/camera.conf` | LAN clients reach the wrong address |
| dnsmasq `interface=` | same file | dnsmasq stops listening on the LAN entirely |
| ufw forward rule | `ufw route ... out on <if>` | VPN clients cannot reach other LAN devices |
| NAT masquerade | `/etc/ufw/before.rules` | VPN traffic is not translated |
| **public A record** | deSEC zone for `camera.<domain>` | phones off-network get the dead address |

The public DNS record is the one that hurts, because it is the only item not on
the Pi, and its TTL (3600) means the stale answer survives in phone and resolver
caches for an hour after you fix it. `ERR_ADDRESS_UNREACHABLE` on a phone whose
tunnel is handshaking normally is this, essentially every time.

To re-sync everything after an address change:

```bash
sudo /opt/camera/deploy/setup-dnsmasq.sh        # rewrites host-record + interface
sudo /opt/camera/deploy/setup-wireguard.sh      # rewrites the ufw route + masquerade
sudo /opt/camera/deploy/make-cert.sh            # only if using the self-signed cert
```

Then update the `camera` A record in the deSEC zone by hand, and re-do the
router's DHCP reservation and port forward for the **new interface's MAC**.

To confirm it took, from the Pi:

```bash
dig +short camera.<domain> @1.1.1.1      # public answer
dig +short camera.<domain> @127.0.0.1    # what dnsmasq tells the LAN
sudo nft list ruleset | grep -E 'masquerade|iifname "wg0"'
```

A better long-term shape is to point the public record at **`10.8.0.1`**, the
Pi's WireGuard address. It is stable, means nothing outside the tunnel, keeps
your internal addressing out of public resolvers, and never needs updating when
the LAN address moves. dnsmasq keeps answering the LAN address locally.

---

### 8. Router — the part that cannot be scripted

Two changes on the router's admin page:

**a) DHCP reservation** — pin the Pi to `192.168.1.77`. Its **eth0** MAC is
`dc:a6:32:68:af:ba` (the wlan0 MAC `…:bb` is one lower — reserve the one that
is actually carrying traffic). Without this, a lease change breaks the port
forward and every address baked into config at the same time.

**b) Port forward** — **UDP 51820 → 192.168.1.77**.

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

The LAN interface is detected from the default route rather than assumed, and
that detection has now been proved twice over: the Pi ran on `wlan0` for most
of this project and moved to `eth0` when the PoE switch arrived. Guides that
hardcode either one silently break access to everything except the Pi itself.

Detection only helps at the moment a script runs, though. These scripts are
one-shot generators, so config written while the Pi was on `wlan0` kept naming
`wlan0` afterwards — see *When the Pi's address changes* below.

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
cellular, connect, then browse to the camera hostname — you are inside the
network now, so it resolves to the Pi's LAN address.

Set **`DNS = 10.8.0.1`** in the client's `[Interface]` section. Without it the
phone resolves the camera name over cellular DNS and caches whatever public
DNS said, which means an address change takes a full TTL to reach it. With it,
the phone asks the Pi's own dnsmasq and always gets the current answer.

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

Health, once logged in: `https://192.168.1.77:5000/health`

MQTT, watching everything the camera publishes:

```bash
mosquitto_sub -h 127.0.0.1 -u camera -P '<password>' -t 'camera/#' -v
```

---

## More than one camera

Each camera is an object that owns its own directory, archive index, probe
cache, frame buffer, recorder and retention window. Nothing mutable is shared,
and that is structural rather than tidy: the three ways two cameras could
corrupt each other are all *silent wrong-data* failures, which for a security
recorder is the worst class there is.

| If storage were shared | What you would see |
|---|---|
| One index over both | the overlap trim clamps camera A's segment to camera B's start; a timeline collapses |
| One probe cache | both produce `2026-08-16.ts`; birth times and durations cross over |
| One window cache | `/play` serves the wrong camera's video, with HTTP 200 and a self-consistent `X-Segment` |

None of those throw. Giving each camera its own everything makes them
impossible to express.

```
/opt/camera/videos/mic612/2026-08-16.ts
/opt/camera/videos/backyard/2026-08-16.ts
/opt/camera/logs/archive-index-mic612.json
/var/cache/camera/windows/<cid>/...
```

Subdirectories rather than filename prefixes, so within one camera the day
genuinely *is* the whole filename and `DAY_FILE_RE`, `day_of()` and the
`.partNN` convention need no changes at all.

### Selecting a camera

Every route takes `?cam=<id>`, defaulting to the primary. Omitting it means
what it always meant, so bookmarks and existing MQTT automations keep working.
Naming a camera that does not exist is refused with 404 and the list of real
ones - falling back to a different camera is the failure this design exists to
prevent, so the default applies only when nothing was named.

PTZ is a capability, not an assumption. A camera without motors refuses with
"<name> has no pan/tilt/zoom" over both HTTP and MQTT, and the web UI hides
the motion, preset and imager sections entirely rather than showing controls
that do nothing.

### Adding one

Append a `CameraConfig` to `camera_configs()` in `camera_service.py`. Anything
host-specific is overridable per camera as `CAM_<ID>_<KEY>`, and the old flat
keys still work for the original camera so `/etc/camera-service.env` needed no
edit when this arrived.

```
CAM_BACKYARD_USER=admin
CAM_BACKYARD_PASS="((*Why))*452"      # quote anything a shell would parse
CAM_BACKYARD_RETENTION_DAYS=7
CAMERAS=mic612,backyard               # optional: narrow what this host runs
```

**Quote values containing shell metacharacters.** systemd parses this file
itself and does not care, but every script that `source`s it does - adding an
unquoted password with parentheses in it broke `verify-live.sh` while the
service carried on perfectly, which took a while to notice.

A camera whose password is missing is skipped with a reason rather than
starting a recorder that fails in a loop.

### Recording a network camera

```
ffmpeg -hide_banner -loglevel warning
  -rtsp_transport tcp -timeout 5000000
  -i rtsp://user:pass@host:554/h264Preview_01_main
  -map 0:v:0 -c:v copy -an
  -f segment -segment_time 86400 -segment_atclocktime 1
  -segment_format mpegts -strftime 1 -reset_timestamps 1
  videos/backyard/%Y-%m-%d.ts
```

- **`-timeout`, not `-stimeout`** on this ffmpeg (5.1.9). Verify with
  `ffmpeg -h demuxer=rtsp | grep -i timeout` before assuming: the wrong
  spelling either refuses to start, or leaves a black-holed socket hanging
  forever while the supervisor never sees an exit.
- **TCP.** A dropped UDP packet under `-c copy` is corruption written straight
  into the archive.
- **No filter of any kind.** See the note in the root README.
- Credentials are inserted percent-encoded when the argument list is built,
  and scrubbed from ffmpeg's stderr before it reaches the journal. They are
  still visible in `ps`, because ffmpeg accepts RTSP credentials only in the
  URL. `hidepid` on /proc is the only real mitigation.

### Live preview

The analog camera's preview is nearly free - its ffmpeg already decodes the
capture, so a second output costs only the MJPEG encode. A network camera has
no such spare decode, so its preview comes from the camera's own low-res
substream in a separate process: measured at 20% of a core against 4% for the
recording it must not disturb.

That process runs only while somebody is watching, plus `PREVIEW_LINGER`
seconds. The saving matters less for CPU than for the camera: every RTSP
session is a resource on the device itself. Cold start to first frame is about
6.5 seconds, which `/health` reports as `preview_running` so the page can say
so instead of showing a dead frame.

### Retention

Per camera, and deliberately equal here. A day that has one angle but not the
other is worse than a shorter archive, because you go looking for the second
view of an incident and it is simply gone.

| | Measured | 7 days |
|---|---|---|
| mic612 | 28 GB/day | 198 GB |
| backyard | 57 GB/day | 396 GB |
| | | **594 GB** of ~820 GB free |

`enforce_free_space()` picks the oldest day across *all* cameras, so the
emergency path cannot favour whichever is listed first, and
`stray_recording_dirs()` warns about directories no camera owns - retention
cannot delete what it cannot see, but the disk still charges for it.

### MQTT

```
camera/status              online | offline (retained, LWT)
camera/state               whole service (retained)
camera/<cid>/state         one camera (retained)
camera/<cid>/ptz/set       commands for that camera
camera/<cid>/ptz/result    outcome
camera/ptz/set             the primary camera - kept for existing automations
```

The unqualified topics still address the primary camera. Silently retargeting
an existing automation at a different device would be the worst possible way
to introduce a second one.

---

## Watching and clipping the archive

The recordings are MPEG-TS, which no browser plays. Rather than ship a
media-source library to decode it in the page, the server hands the browser
an ordinary MP4 cut on demand: ffmpeg copies the H.264 bitstream and rewrites
only the container, so nothing is re-encoded, the picture is bit-identical to
what was recorded, and a cut costs about a second.

**Where the times come from.** Nothing inside a .ts file records wall-clock
time, so each file's span is derived from the filesystem. The anchor is the
file's *birth* time - ext4 keeps it, `stat -c %W` reads it, and it survives
the rename that turns today's file into a `.partNN` on restart. Checked
against the timestamp burned into the picture: a clip asked for at 13:20:50
started on the frame stamped 13:20:50.

mtime was tried first and is wrong. It is when the file was last written,
which trails the final recorded frame by anything from 2s to 30s depending on
how that ffmpeg ended - a clip asked for at 13:17:47 came back at 13:17:18.
That is also why a segment's end is start + duration and never mtime:
claiming footage that was never written makes playback seek into nothing.
Where ffmpeg's duration overruns the next file's birth time, the duration is
trimmed, because the birth time is a fact and the duration is an estimate.

**Gaps are real and are drawn.** Every restart ends one file and begins
another, and whatever happened in between was not recorded. The timeline
shows the holes instead of closing them up, clicking into one skips to the
next footage, and a clip spanning a hole reports the missing seconds in an
`X-Missing-Seconds` header - the burned-in clock jumping is the proof.

**Playback is windowed.** `/play` serves a complete, seekable MP4 covering
PLAY_WINDOW_SECONDS (120 by default), aligned to the recording's start so
that scrubbing around one moment keeps hitting the same cut. Finished windows
stay in `clips/windows` up to WINDOW_CACHE_MB (1024) so a scrub backwards or
a range request costs nothing; the page reaches the same boundary arithmetic
from the segment list, so its clock stays exact without asking.

A fragmented stream was tried first and is worse: with no duration in the
moov, the browser reported a 299s window as 6s, its scrubber did nothing, and
it could not seek inside what it already held.

**Clips** come from `/clip?day=&from=&to=`, capped at CLIP_MAX_SECONDS (1800).
A range crossing a restart is stitched: each file is cut with its own fast
seek and the pieces are concatenated. Seeking *through* a concatenation
instead was measured at 28.8s against 2.3s, which is why it is done this way.

Archive work runs `nice -n 10 ionice -c 3` and no more than MEDIA_JOBS (3) at
once, with requests refused rather than queued after MEDIA_WAIT seconds. The
live recording always wins.

**Where the cache lives, and why it is not in the install tree.** The unit
runs `ProtectSystem=strict`, which makes everything outside `ReadWritePaths`
read-only *to the service* no matter who owns it. The first version of this
feature put its scratch in `/opt/camera/clips`, and every playback request
died on `Errno 30: Read-only file system` - while every test passed, because
the tests drove the app in-process as the `pi` user, outside the sandbox
entirely. A feature that works when you test it as yourself and fails as the
service is the failure mode this sandbox is built to produce.

So the cache directory is now chosen at startup by trying candidates and
keeping the first that can hold a file: `$CACHE_DIR`, then systemd's
`$CACHE_DIRECTORY`, then `logs/media-cache`, which the unit already grants.
`CacheDirectory=camera` in the unit makes systemd provide `/var/cache/camera`
- outside the tree, correct ownership, removable with `systemctl clean` - and
the service moves there by itself once the unit is reinstalled:

    sudo cp /opt/camera/deploy/camera.service /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl restart camera.service

Startup logs which directory it settled on, and says loudly if none worked.
Anything touching the sandbox should be checked against the running service
over HTTPS, not through the test client - `deploy/verify-live.sh` does that,
minting a session from the same `EnvironmentFile` the service reads.

---

## The camera itself: verified, confirmed, gated

The MIC 612 is driven blind - RS-485 is one-way, nothing is ever read back -
so what we "know" about it comes in three grades, and the code enforces the
difference.

**Verified live on this unit** (never renumber): wiper = aux 1, OSD open =
aux 2, thermal = aux 4 on, visible = aux 4 off, tour 1/2 = presets 81/82.
Aux numbers are remappable in the camera's own Pelco AUX menu and two of
ours are local remaps (factory docs put the imager toggle on aux 5 and the
menu on Set-Preset 95) - a factory reset of the camera would move them.

**Reserved preset bands the server refuses** (HTTP and MQTT alike): 33/34
(flip/home actions), 62 (washer nozzle), 80-99 (Bosch special band - 92/93
WRITE the AutoScan limits, 95 opens the menu, 97 re-addresses the camera on
the bus). User scenes live in 1-32, 35-61, 63-79.

**Gated pending a bench test** - each is one watched command with the camera
on the bench:

1. One diagonal `/move` (e.g. up-right) -> then set `DIAGONALS_ENABLED=1`
   in /etc/camera-service.env to turn the D-pad from 4-way into 8-way.
2. `goto preset 34` then `33` -> then enable the HOME and FLIP buttons in
   templates/index.html (they ship disabled).
3. A single-direction hold longer than 20 s -> confirms whether the Pelco
   runaway-protect timeout exists here (the client already re-sends every
   4 s, harmless either way).

**Never send**: preset 97 (FastAddress), presets 92/93 (limit writes),
opcode 0x49 (azimuth-zero calibration write), opcode 0x29 (factory reset),
turbo speed 0x40. pelcoD.py deliberately cannot build the last three.

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
