# MIC 612 Camera Control

A Bosch MIC 612 PTZ camera driven from a Raspberry Pi 4: continuous recording,
a live view in the browser, and pan/tilt/zoom over RS-485. A second camera, a
garage door and a thermostat have since joined it, all on the same page and all
on the same rule - open-source libraries, nothing in the cloud, no phone app.

The camera itself is not on the network. Video arrives as analogue signal
through a USB capture dongle, and control leaves as Pelco-D over a USB serial
adapter. Everything IP-facing is the Pi.

---

## Physical layout

```
        OUTSIDE                                    INSIDE (by the front door)
  ┌──────────────────┐                        ┌──────────────────────────────┐
  │   Bosch MIC 612  │                        │      Raspberry Pi 4          │
  │                  │  analogue video        │                              │
  │  video out ──────┼───────────────────────►│ USB capture dongle           │
  │                  │                        │   MACROSILICON               │
  │  RS-485 in  ◄────┼────────────────────────┤ USB serial adapter           │
  │                  │  Pelco-D, 9600 baud    │   FTDI FT232R                │
  └──────────────────┘                        │                              │
                                              │ USB SSD  (931 GB, boots here)│
  ┌──────────────────┐                        │ eth0     (PoE switch, wired) │
  │  Reolink 510A    │   H.264 over RTSP      │                              │
  │  "backyard"      ├───── PoE / Cat5e ─────►│ (no capture hardware: it      │
  │  2560x1920, PoE  │   already encoded      │  arrives ready to store)     │
  └──────────────────┘                        └──────────────────────────────┘
```

The two cameras cost wildly different amounts to record. The MIC arrives as
analogue video, so every frame must be encoded on this Pi: about 150% of a
core. The Reolink arrives already H.264, so ffmpeg copies the bitstream and
rewrites only the container - about 4% of a core, for a stream at twice the
bitrate. That asymmetry is why nothing is allowed to put a filter in the
network camera's path, not even a timestamp overlay: any filter forces a full
decode of 2560x1920 and the saving evaporates.

Two more devices sit on the LAN rather than on the Pi: a **ratgdo** board
wired into the garage opener, which the service polls over HTTP and republishes
to MQTT, and a **SONOFF Z-Wave** stick in the Pi's own USB, which `zwave-js-ui`
owns and through which the thermostat is reached.

USB devices are addressed by `/dev/v4l/by-id/…` and by udev symlinks bound to
each adapter's serial number — `/dev/pelco-d` and `/dev/zwave-stick`, from
`deploy/70-serial-adapters.rules`. Never `ttyUSB0` or `video0`: those numbers
are assigned in probe order, so a reboot or an extra USB device can reassign
them. With two serial adapters now on one bus that stopped being a theoretical
risk — sending Pelco-D frames into a Z-Wave radio is a bad failure to debug,
so neither service gets blanket `dialout`.

---

## What runs, as independent units

These five start on their own and keep running. Only the first is code from
this repository.

```
                          systemd (PID 1)
                                │
   ┌──────────────┬─────────────┼──────────────┬─────────────────┐
   │              │             │              │                 │
┌──▼───────────┐ ┌▼───────────┐ ┌▼───────────┐ ┌▼──────────────┐ ┌▼─────────────┐
│camera.service│ │ mosquitto  │ │wg-quick@wg0│ │ desec-ddns    │ │    ufw       │
│              │ │  .service  │ │  .service  │ │   .timer      │ │  .service    │
│ ours         │ │            │ │            │ │               │ │              │
│ Python +     │ │ MQTT broker│ │ VPN, sets  │ │ every 5 min,  │ │ netfilter    │
│ ffmpeg       │ │ :1883      │ │ up wg0 then│ │ oneshot       │ │ rules, then  │
│ :5000        │ │ auth + ACL │ │ exits      │ │               │ │ exits        │
│ user: camera │ │            │ │ :51820/udp │ │ updates the   │ │              │
│              │ │            │ │            │ │ DNS record    │ │ default deny │
└──────────────┘ └────────────┘ └────────────┘ └───────────────┘ └──────────────┘
   long-running     long-running    one-shot        periodic         one-shot
                                    (interface       one-shot        (rules
                                     persists)                        persist)
```

`wg-quick@wg0` and `ufw` show as `active (exited)` — they configure the kernel
and stop. The interface and the firewall rules outlive the process.

A sixth, `zwave-js-ui.service`, runs wherever a Z-Wave controller is present.
It owns the radio and republishes every device value to MQTT under `hvac/`,
and it is the one place this project runs somebody else's application rather
than a library: Z-Wave is a certification-bound protocol whose security layer
is not something to hand-roll next to a furnace. `deploy/README.md` covers what
that costs and how it is confined.

---

## Inside `camera.service`

Two processes. The Python process owns the hardware and the web interface; a
supervised ffmpeg owns every pixel.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  camera.service            runs as the unprivileged 'camera' account    │
│                                                                         │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  camera_service.py                                    23 threads  │  │
│  │                                                                   │  │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌──────────┐  │  │
│  │  │  recorder   │  │  retention  │  │ mqtt-state  │  │  cheroot │  │  │
│  │  │             │  │             │  │             │  │  server  │  │  │
│  │  │ spawns and  │  │ hourly:     │  │ every 30s:  │  │          │  │  │
│  │  │ restarts    │  │ delete days │  │ publish     │  │ 16 worker│  │  │
│  │  │ ffmpeg,     │  │ older than  │  │ health to   │  │ threads, │  │  │
│  │  │ reads its   │  │ 14, enforce │  │ MQTT        │  │ TLS 1.2+ │  │  │
│  │  │ preview     │  │ 50 GB floor │  │             │  │ :5000    │  │  │
│  │  │ pipe        │  │             │  │             │  │          │  │  │
│  │  └──────┬──────┘  └─────────────┘  └─────────────┘  └────┬─────┘  │  │
│  │         │                                                │        │  │
│  │         │ JPEG frames                     latest frame   │        │  │
│  │         └──────────────► FrameBuffer ◄─────────────────  ┘        │  │
│  │                          (condition variable, latest wins)        │  │
│  │                                                                   │  │
│  │  ┌─────────────┐  ┌─────────────┐                                 │  │
│  │  │ mqtt client │  │     PTZ     │  one lock per serial port, so   │  │
│  │  │ (paho loop) │  │  pelcoD.py  │  overlapping requests cannot    │  │
│  │  │ subscribes  │──►             │  interleave two 7-byte frames   │  │
│  │  │ ptz/set     │  │ /dev/serial │                                 │  │
│  │  └─────────────┘  └──────┬──────┘                                 │  │
│  └────────────────────────────┼──────────────────────────────────────┘  │
│                               │                                         │
│  ┌────────────────────────────┼──────────────────────────────────────┐  │
│  │  ffmpeg (child process)    │                                      │  │
│  │                            ▼  RS-485 out to the camera            │  │
│  │   /dev/v4l/by-id/…  MJPEG 1280x720 @20fps                         │  │
│  │        │                                                          │  │
│  │        ▼                                                          │  │
│  │   drawtext  ── burn in the timestamp                              │  │
│  │        │                                                          │  │
│  │      split ──┬─── fps=10 ─ libx264 ─ segment ──► videos/DATE.ts   │  │
│  │              │                       cut at local midnight        │  │
│  │              └─── fps=20 ─ scale 640 ─ mjpeg ──► stdout pipe      │  │
│  └───────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
```

Python never touches pixel data. It shuttles already-encoded JPEG bytes from
ffmpeg's pipe to browsers, which is why it sits at about 1.5% CPU while ffmpeg
uses roughly 1.25 cores.

The recorder thread is a supervisor: if ffmpeg dies it restarts it with
backoff, and preserves any existing recording for the current day as
`DATE.partNN.ts` rather than letting the new run truncate it.

---

## The two request paths

Control can arrive over HTTP or MQTT. Both land on the same command table, so
they cannot drift apart.

```
   browser ─── HTTPS ──► cheroot ──► Flask route ──┐
   (session cookie)                                │
                                                   ├──► execute_ptz()
   automation ── MQTT ──► paho ──► camera/ptz/set ─┘         │
   (broker auth + ACL)                                       ▼
                                                     PTZ.send() ── lock
                                                          │
                                                     pelcoD.py
                                                          │
                                                  FF 01 00 04 19 1E
                                                          │
                                                    RS-485 ──► camera
```

Video only ever flows one way:

```
   camera ──► dongle ──► ffmpeg ──┬──► videos/2026-08-15.ts   (on disk, 14 days)
                                  │
                                  └──► FrameBuffer ──► GET /camera  (multipart
                                                        MJPEG, one encode
                                                        shared by all viewers)
```

---

## Network boundary

Exactly one port is reachable from the internet, and it is not the camera.

```
   INTERNET
      │
      │  UDP 51820 only — forwarded on the router
      │  WireGuard never replies to an unauthenticated packet,
      │  so a port scan sees nothing here
      ▼
  ┌─────────────────────────────────────────────────────────┐
  │  wg0   10.8.0.1/24                                      │
  │        clients get 10.8.0.2+                            │
  └───────────────────────┬─────────────────────────────────┘
                          │  once connected, a phone is
                          │  logically on the LAN
  ┌───────────────────────▼─────────────────────────────────┐
  │  ufw — default deny inbound                             │
  │                                                         │
  │    22   ◄── LAN, VPN          ssh                       │
  │    5000 ◄── LAN, VPN          web UI, TLS + login       │
  │    1883 ◄── LAN, VPN          MQTT, auth + per-topic ACL│
  │    5353 ◄── LAN               mDNS                      │
  └─────────────────────────────────────────────────────────┘
```

The web UI is never exposed directly. Remote access means joining the network,
not publishing the camera — so the self-signed certificate stays adequate and
no reverse proxy is needed.

`desec-ddns.timer` keeps a `dedyn.io` hostname pointed at the current public
IP, because the ISP address is dynamic and the router has no DDNS client of
its own.

---

## MQTT topics

Each account is confined to its own prefix, so a compromised device cannot
drive the others.

```
   camera/status       online | offline    retained; 'offline' is published by
                                           the broker as a Last Will if the
                                           service dies without saying goodbye
   camera/state        retained JSON health snapshot, refreshed every 30s
   camera/ptz/set      subscribed: {"action": "pan_left", "speed": 1-63},
                       a bare action string, or {"action": "move", "pan": -1|0|1,
                       "tilt": -1|0|1, "panSpeed": n, "tiltSpeed": n}
   camera/ptz/result   outcome of the last command

   garage/state        retained JSON: door, light, remote lock, obstruction.
                       Volatile fields (uptime, signal) are excluded, or a
                       door that has not moved in a week would republish
                       thirty times a minute
   garage/<what>/set   subscribed: door | light | lock. Every control states
                       the state it wants rather than toggling

   hvac/nodeID_<n>/…   published by zwave-js-ui, one topic per Z-Wave value
   hvac/_CLIENTS/…/api the gateway's own request/response API

The camera service may READ `hvac/#` and WRITE only the gateway's api topic.
It can ask for a setpoint change; it cannot publish a temperature and claim it
came from the thermostat. That split is enforced by the broker ACL rather than
by convention, and it is checked - publishing a fake reading as the `camera`
user is refused.
```

---

## Repository layout

```
   camera_service.py      the service: recorder, retention, MQTT bridge,
                          web UI, PTZ. Host-specific values come from the
                          environment, so nothing here is pinned to one Pi.
   pelcoD.py              Pelco-D frame construction. Unchanged from the
                          original project; it was always correct.
   requirements.txt       flask, flask-login, pyserial, paho-mqtt, cheroot
                          ffmpeg and mosquitto are system packages

   videos/<cid>/          one directory per camera; the day is the filename
   templates/             base.html, index.html, login.html,
                          recordings.html (file list), watch.html (player)
   static/                lcars.css, main.js and the vendored Antonio font.
                          No frameworks and nothing fetched from a CDN - the
                          interface works with no internet at all, and the
                          CSP allows no inline code whatsoever

   deploy/                everything needed to stand this up on a fresh Pi,
                          plus the router steps that cannot be scripted.
                          See deploy/README.md.
```

Secrets are deliberately absent: credentials live in `/etc/camera-service.env`,
the TLS key in `/etc/camera-tls/`, MQTT passwords in `/etc/mosquitto/passwd`,
and the WireGuard keys in `/etc/wireguard/`. Back those up separately — the
code is safe in git, those are not.

---

## Getting started

```bash
sudo git clone https://github.com/jakeend2/Camera.git /opt/camera
sudo /opt/camera/deploy/install.sh
```

`deploy/README.md` covers the rest: TLS, the broker, dynamic DNS, WireGuard,
the two router changes, and the traps worth knowing about before they cost you
an afternoon.
