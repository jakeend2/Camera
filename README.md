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

Three processes, sometimes four. The Python process owns the hardware and the
web interface; one supervised ffmpeg per camera owns every pixel; a fourth
appears only while somebody is watching the network camera's preview.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  camera.service                 runs as the unprivileged 'camera' account│
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  camera_service.py                                     43 threads  │  │
│  │                                                                    │  │
│  │   recorder     one supervisor per camera: spawn ffmpeg, restart it │  │
│  │                with backoff, read its preview pipe                 │  │
│  │   retention    hourly: drop each camera's days past ITS OWN window │  │
│  │                (7 here), then enforce the 50 GB floor oldest-first │  │
│  │   mqtt-state   every 30s: publish a health snapshot per camera     │  │
│  │   mqtt client  subscribes camera/<cid>/ptz/set  (paho loop)        │  │
│  │   ratgdo       every 2s: poll the garage over HTTP digest auth,    │  │
│  │                republish to MQTT as its own broker identity        │  │
│  │   hvac         listens to the Z-Wave gateway's topics; writes only │  │
│  │                through the gateway's API, never device state       │  │
│  │   cheroot      32 workers, TLS 1.2+, :5000                         │  │
│  │                                                                    │  │
│  │   FrameBuffer  one per camera. Condition variable, latest wins -   │  │
│  │                a slow viewer drops frames instead of stalling      │  │
│  │   PTZ          pelcoD.py -> /dev/pelco-d, one lock per serial port │  │
│  │                so two 7-byte frames cannot interleave              │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  ffmpeg: mic612          analog in, so every frame is encoded here │  │
│  │                                                                    │  │
│  │   /dev/v4l/by-id/…  MJPEG 1280x720 @20fps                          │  │
│  │        -> drawtext (burn in the timestamp)                         │  │
│  │        -> split ─┬─ fps=10, libx264 ─► videos/mic612/DATE.ts       │  │
│  │                  │                     cut at local midnight       │  │
│  │                  └─ fps=20, scale 640, mjpeg ─► stdout pipe        │  │
│  │                                          ~150% of a core           │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  ffmpeg: backyard        already H.264, so the bitstream is copied │  │
│  │                                                                    │  │
│  │   rtsp main 2560x1920 @20fps ─ c:v copy ─► videos/backyard/DATE.ts │  │
│  │        no filter, not even a timestamp: any filter would force a   │  │
│  │        full decode and the ~4%-of-a-core saving would evaporate    │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  ffmpeg: backyard preview      ON DEMAND, plus PREVIEW_LINGER (60s)│  │
│  │                                                                    │  │
│  │   rtsp sub 640x480 @15fps ─ mjpeg ─► stdout pipe   23% of a core   │  │
│  │        a separate process on the substream, because the main       │  │
│  │        stream must reach the disk untouched                        │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```

The two cameras cost very different amounts, and that asymmetry is the reason
for most of the design above. Recording the analog camera means encoding it;
recording the network camera means copying bytes. Nothing is allowed to put a
filter in the network camera's path.

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
   camera ──► dongle ──► ffmpeg ──┬──► videos/<cid>/2026-08-15.ts  (7 days)
                                  │
                                  └──► FrameBuffer ──► GET /camera  (multipart
                                                        MJPEG, one encode
                                                        shared by all viewers)
```

---

## HTTP surface

Forty-two routes. Every one requires a session except `/login` and `/static`
(`PUBLIC_ENDPOINTS`), and anything scripted gets a 401 in JSON rather than a
redirect to the login form.

Routes marked **cam** accept `?cam=<id>` and act on that camera; without it
they act on `PRIMARY_CAMERA`.

### Pages

| Route | | |
|---|---|---|
| `GET /` | | the live page |
| `GET /watch`, `/watch/<day>` | cam | the archive player |
| `GET /recordings` | cam | the file list |
| `GET,POST /login` | | the only public route besides static files |
| `GET /logout` | | |

### Live video

| Route | | |
|---|---|---|
| `GET /camera` | cam | MJPEG stream, `multipart/x-mixed-replace` |
| `GET /snapshot` | cam | the latest frame as a downloadable JPEG |
| `POST /preview/warm` | cam | start a substream preview without streaming it |
| `GET /health` | cam | whole-service health, or one camera's with `?cam=` |

### Archive

| Route | | |
|---|---|---|
| `GET /timeline` | cam | what footage exists for a day, and where the holes are |
| `GET /play` | cam | a bounded, seekable MP4 window |
| `GET /clip` | cam | cut `[from, to]` out and hand back a real MP4 |
| `GET /recordings/<name>` | cam | one recording, by filename |

### PTZ — all POST, all refused for a camera with no motors

Eighteen action routes are registered from `HTTP_PTZ_ROUTES` rather than
written out as decorators, because `templates/index.html` already posted to
these exact paths and the keys must not change:

```
/pan_left  /pan_right  /tilt_up  /tilt_down  /stop
/zoom_tele /zoom_wide  /focus_near /focus_far  /iris_open /iris_close
/OSD_menu  /Thermal_Camera /Visible_Light_Camera
/Windshield_Wiper /Wiper_Off  /Tour_1 /Tour_2
```

Plus:

| Route | | |
|---|---|---|
| `POST /move` | cam | combined-axis motion for the D-pad |
| `POST /Set_preset`, `/Goto_preset`, `/Clear_preset` | cam | 1-79 only; 33, 34, 62 and 80-99 are camera functions and are refused |
| `POST /Start_New_File` | cam | close today's file early, open a fresh one |
| `POST /Exit_program` | | ask systemd to restart the service |

### The rest of the house

| Route | |
|---|---|
| `GET /garage` | door, light, remote lock, obstruction |
| `POST /garage/<what>` | `door` / `light` / `lock` - each states the state it wants |
| `GET /hvac` | temperature, mode, setpoints, liveness |
| `POST /hvac/<what>` | `heat` / `cool` / `mode` / `fan`, clamped server-side |

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
