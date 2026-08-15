# MIC 612 Camera Control

A Bosch MIC 612 PTZ camera driven from a Raspberry Pi 4: continuous recording,
a live view in the browser, and pan/tilt/zoom over RS-485.

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
                                              │ wlan0    (2.4 GHz, pinned)   │
                                              └──────────────────────────────┘
```

Both USB devices are addressed by `/dev/serial/by-id/…` and `/dev/v4l/by-id/…`,
never by `ttyUSB0` or `video0`. Those numbers are assigned in probe order, so a
reboot or an extra USB device can reassign them — and sending Pelco-D frames to
the wrong adapter is a bad failure to debug.

---

## What runs, as independent units

Five things start on their own and keep running. Only the first is code from
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

   garage/#            reserved for a ratgdo controller  (not yet installed)
   hvac/#              reserved for a Z-Wave thermostat  (not yet installed)
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

   templates/             base.html, index.html, login.html,
                          recordings.html (the archive browser)
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
