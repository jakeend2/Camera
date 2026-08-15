#!/usr/bin/env python3
"""
Camera control and recording service for the Bosch MIC 612.

The whole media path lives in a supervised ffmpeg subprocess:

    /dev/video0 (MJPEG)
        -> drawtext timestamp
        -> split
             |-> format=yuv420p -> h264_v4l2m2m -> clock-aligned daily segments
             `-> fps/scale      -> mjpeg        -> stdout pipe -> browser preview

Python never touches pixel data. It relays already-encoded JPEG frames to
browsers and owns the RS-485 link for Pelco-D PTZ commands.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import ssl
import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import paho.mqtt.client as mqtt
import serial
from flask import (Flask, Response, jsonify, make_response, redirect,
                   render_template, request, url_for)
from flask_login import (LoginManager, UserMixin, current_user, login_required,
                         login_user, logout_user)
from werkzeug.security import check_password_hash

from pelcoD import pelcoD

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent

def _env(name: str, default: str) -> str:
    """Read a setting from the environment, falling back to the default.

    Host-specific values - which capture dongle, which serial adapter - are
    written to /etc/camera-service.env by deploy/install.sh, which detects
    them. The defaults below are this deployment's hardware, so the service
    still runs unconfigured, but nothing here needs editing to move machines.
    """
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


# Addressed by the stable by-id link, not by index. /dev/videoN numbering is
# assigned in probe order, so a kernel upgrade or an extra capture device can
# move it - and the bcm2835 codec nodes already occupy video10-video23.
VIDEO_DEVICE = _env(
    "VIDEO_DEVICE", "/dev/v4l/by-id/usb-MACROSILICON_usb_video-video-index0"
)
VIDEO_DEVICE_FALLBACK = "/dev/video0"
CAPTURE_SIZE = _env("CAPTURE_SIZE", "1280x720")
# The capture dongle only offers discrete rates (5/10/20/25/30). Asking for an
# unsupported rate silently gets you the next one up - 15 yields 20.
CAPTURE_FPS = _env_int("CAPTURE_FPS", 20)
# Recording is decimated below the capture rate on purpose: the live preview
# benefits from 20 Hz, stored footage does not, and H.264 encoding is the
# single most expensive thing on this box. Set equal to CAPTURE_FPS for
# smoother recordings at roughly double the encoder cost.
RECORD_FPS = _env_int("RECORD_FPS", 10)
RECORD_BITRATE = _env("RECORD_BITRATE", "2500k")
GOP_FRAMES = RECORD_FPS * 2  # keyframe every 2s: seeking, clean segment cuts

# libx264 rather than the Pi's h264_v4l2m2m hardware encoder. v4l2m2m is
# roughly 3x cheaper, but when the filter graph feeds a second output (our
# browser preview) it emits a stream with no usable SPS/PPS: ffprobe reports
# 0x0 and no frame can be extracted. Every bsf workaround was tried. Correct
# files beat cheap ones. 'superfast' costs the same CPU as 'ultrafast' here
# but compresses noticeably better.
RECORD_ENCODER = ["-c:v", "libx264", "-preset", "superfast"]

# MPEG-TS, deliberately: it needs no trailer, so a recording stays playable
# even if the service is SIGKILLed or the power drops mid-write.
RECORD_EXT = "ts"
RECORD_FORMAT = "mpegts"

PREVIEW_WIDTH = 640
PREVIEW_FPS = 20             # matches CAPTURE_FPS; higher would only duplicate
PREVIEW_QUALITY = 7          # mjpeg -q:v, 2 (best) .. 31 (worst)

VIDEO_DIR = BASE_DIR / "videos"
LOG_DIR = BASE_DIR / "logs"
RETENTION_DAYS = _env_int("RETENTION_DAYS", 14)
MIN_FREE_GB = _env_int("MIN_FREE_GB", 50)   # floor; oldest whole days go first

# Addressed by the adapter's own serial number, not by enumeration order.
# /dev/ttyUSB0 is first-come-first-served: plug in any other USB serial device
# (most Z-Wave sticks use CP210x chips and claim ttyUSB too) and a reboot can
# hand that name to the wrong device, silently sending Pelco-D into the radio.
SERIAL_PORT = _env(
    "SERIAL_PORT",
    "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AM00KHR1-if00-port0",
)
SERIAL_PORT_FALLBACK = "/dev/ttyUSB0"
SERIAL_BAUD = _env_int("SERIAL_BAUD", 9600)
PTZ_SPEED = 25               # Pelco-D pan/tilt speed, 0-63

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 5000
SERVER_THREADS = 16          # concurrent requests; MJPEG viewers hold one each

# TLS. Self-signed and local-only by design - the certificate carries the Pi's
# hostnames and LAN address as SANs. Missing files degrade to plain HTTP with a
# loud warning rather than refusing to start, so a cert problem never costs you
# the recording.
TLS_CERT = os.environ.get("TLS_CERT", "/etc/camera-tls/server.crt")
TLS_KEY = os.environ.get("TLS_KEY", "/etc/camera-tls/server.key")
TLS_AVAILABLE = Path(TLS_CERT).exists() and Path(TLS_KEY).exists()

# Web login. Credentials arrive from /etc/camera-service.env, never the repo.
# Without them the service still runs but logs a loud warning - losing the
# recording because a password file went missing would be a worse failure.
WEB_USERNAME = os.environ.get("WEB_USERNAME", "admin")
WEB_PASSWORD_HASH = os.environ.get("WEB_PASSWORD_HASH") or None
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY") or None
AUTH_ENABLED = bool(WEB_PASSWORD_HASH and SECRET_KEY)
SESSION_HOURS = 12
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300

# MQTT. Credentials arrive from /etc/camera-service.env through systemd's
# EnvironmentFile, deliberately outside the repo so they are never committed.
# With no credentials the bridge simply stays off and the camera is unaffected.
MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME") or None
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD") or None
MQTT_PREFIX = os.environ.get("MQTT_PREFIX", "camera")
MQTT_ENABLED = bool(MQTT_USERNAME and MQTT_PASSWORD)
MQTT_STATE_INTERVAL = 30     # seconds between retained state publishes

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
FONT = next((p for p in _FONT_CANDIDATES if Path(p).exists()), None)

# 2026-08-15.ts, and 2026-08-15.part01.ts for same-day restarts
DAY_FILE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})(?:\.part\d+)?\." + RECORD_EXT + r"$"
)

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"

# High-volume, harmless chatter from the v4l2m2m encoder and swscaler.
FFMPEG_NOISE = (
    "deprecated pixel format used",
    "EOI missing, emulating",
)

log = logging.getLogger("camera")


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "camera_service.log", maxBytes=5 * 2**20, backupCount=5
    )
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    handler.setFormatter(fmt)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    log.addHandler(stream)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Preview frame distribution
# ---------------------------------------------------------------------------
class FrameBuffer:
    """Latest-frame-wins buffer. Readers block until a newer frame arrives."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._frame: bytes | None = None
        self._seq = 0

    def publish(self, jpeg: bytes) -> None:
        with self._cond:
            self._frame = jpeg
            self._seq += 1
            self._cond.notify_all()

    def wait_for(self, last_seq: int, timeout: float = 5.0):
        """Return (seq, jpeg) once seq > last_seq, else None on timeout."""
        with self._cond:
            if self._seq <= last_seq:
                self._cond.wait(timeout)
            if self._frame is None or self._seq <= last_seq:
                return None
            return self._seq, self._frame

    @property
    def seq(self) -> int:
        with self._cond:
            return self._seq


def pump_preview(stdout, buf: FrameBuffer, stop: threading.Event) -> None:
    """Split ffmpeg's raw MJPEG stdout into frames and publish each one.

    0xFFD9 only appears as a real end-of-image marker: inside entropy-coded
    data a literal 0xFF is byte-stuffed as 0xFF 0x00.
    """
    data = bytearray()
    while not stop.is_set():
        chunk = stdout.read(65536)
        if not chunk:
            return
        data.extend(chunk)
        while True:
            start = data.find(SOI)
            if start < 0:
                data.clear()
                break
            end = data.find(EOI, start + 2)
            if end < 0:
                del data[:start]      # drop garbage, keep the partial frame
                break
            buf.publish(bytes(data[start:end + 2]))
            del data[:end + 2]


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------
def day_of(path: Path):
    """Parse the calendar day a recording belongs to from its filename."""
    m = DAY_FILE_RE.match(path.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


class Recorder:
    """Owns the ffmpeg subprocess and keeps it running."""

    def __init__(self, buf: FrameBuffer) -> None:
        self._buf = buf
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self.restarts = 0
        self.started_at: datetime | None = None

    # -- command ----------------------------------------------------------
    def _filter_complex(self) -> str:
        if FONT:
            stamp = (
                f"drawtext=fontfile={FONT}:text=%{{localtime}}:"
                "x=10:y=h-30:fontsize=22:fontcolor=white:"
                "box=1:boxcolor=black@0.5,"
            )
        else:
            stamp = ""
        return (
            f"[0:v]{stamp}split=2[rec][prv];"
            f"[rec]fps={RECORD_FPS},format=yuv420p[recout];"
            f"[prv]fps={PREVIEW_FPS},scale={PREVIEW_WIDTH}:-2[prvout]"
        )

    @staticmethod
    def _video_device() -> str:
        """Prefer the stable by-id link; fall back to the raw node loudly."""
        if Path(VIDEO_DEVICE).exists():
            return VIDEO_DEVICE
        log.warning("%s missing - falling back to %s. Confirm this is the "
                    "capture dongle and not another video node.",
                    VIDEO_DEVICE, VIDEO_DEVICE_FALLBACK)
        return VIDEO_DEVICE_FALLBACK

    def _cmd(self) -> list[str]:
        # stdin stays open on purpose: writing 'q' is the only way to make
        # ffmpeg close its output files cleanly. SIGTERM makes it exit at once.
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", CAPTURE_SIZE, "-framerate", str(CAPTURE_FPS),
            "-i", self._video_device(),
            "-filter_complex", self._filter_complex(),
            # One file per calendar day, cut exactly at local midnight.
            "-map", "[recout]",
            *RECORD_ENCODER,
            "-b:v", RECORD_BITRATE, "-g", str(GOP_FRAMES),
            "-f", "segment",
            "-segment_time", "86400",
            "-segment_atclocktime", "1",
            "-segment_format", RECORD_FORMAT,
            "-strftime", "1",
            "-reset_timestamps", "1",
            str(VIDEO_DIR / f"%Y-%m-%d.{RECORD_EXT}"),
            # Low-rate preview for the browser.
            "-map", "[prvout]",
            "-c:v", "mjpeg", "-q:v", str(PREVIEW_QUALITY), "-f", "mjpeg", "pipe:1",
        ]

    # -- lifecycle --------------------------------------------------------
    @staticmethod
    def _preserve_todays_file() -> None:
        """ffmpeg would truncate an existing file for today, so rename it first."""
        stamp = f"{date.today():%Y-%m-%d}"
        today = VIDEO_DIR / f"{stamp}.{RECORD_EXT}"
        if not today.exists():
            return
        for n in range(1, 100):
            alt = VIDEO_DIR / f"{stamp}.part{n:02d}.{RECORD_EXT}"
            if not alt.exists():
                today.rename(alt)
                log.info("Kept earlier recording for today as %s", alt.name)
                return
        log.error("Too many same-day parts; leaving %s alone", today.name)

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        suppressed = 0
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            if any(noise in line for noise in FFMPEG_NOISE):
                suppressed += 1
                if suppressed % 1000 == 0:
                    log.info("ffmpeg: suppressed %d benign warnings", suppressed)
                continue
            log.warning("ffmpeg: %s", line)

    def run(self) -> None:
        backoff = 2
        while not self._stop.is_set():
            VIDEO_DIR.mkdir(parents=True, exist_ok=True)
            self._preserve_todays_file()

            cmd = self._cmd()
            log.info("Starting ffmpeg (%s @ %s fps, %s)",
                     CAPTURE_SIZE, CAPTURE_FPS, RECORD_BITRATE)
            try:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    bufsize=0, cwd=str(BASE_DIR),
                )
            except Exception:
                log.exception("Could not launch ffmpeg")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue

            with self._lock:
                self._proc = proc
                self.started_at = datetime.now()

            threading.Thread(target=self._drain_stderr, args=(proc,),
                             daemon=True).start()

            began = time.monotonic()
            try:
                pump_preview(proc.stdout, self._buf, self._stop)
            except Exception:
                log.exception("Preview pump died")

            rc = self._reap(proc)
            if self._stop.is_set():
                return

            ran = time.monotonic() - began
            self.restarts += 1
            log.error("ffmpeg exited rc=%s after %.0fs - restarting", rc, ran)
            # A long run means the failure was transient; reset the backoff.
            backoff = 2 if ran > 60 else min(backoff * 2, 60)
            self._stop.wait(backoff)

    @staticmethod
    def _reap(proc: subprocess.Popen):
        """Ask ffmpeg to finish, escalating only if it will not."""
        if proc.poll() is None:
            try:
                proc.stdin.write(b"q")     # graceful: closes output files
                proc.stdin.flush()
            except Exception:
                proc.terminate()
            try:
                proc.wait(20)
            except subprocess.TimeoutExpired:
                log.warning("ffmpeg did not quit on request; terminating")
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    log.error("ffmpeg unresponsive; killing")
                    proc.kill()
        return proc.poll()

    def roll(self) -> bool:
        """Close the current file and start a new one immediately."""
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return False
        log.info("Manual file roll requested")
        self._reap(proc)              # run() sees the pipe close and restarts
        return True

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            self._reap(proc)

    @property
    def alive(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None


# ---------------------------------------------------------------------------
# Retention - whole calendar days, keyed off the filename not mtime
# ---------------------------------------------------------------------------
def purge_old_recordings() -> None:
    today = date.today()
    cutoff = today - timedelta(days=RETENTION_DAYS)
    for path in sorted(VIDEO_DIR.glob(f"*.{RECORD_EXT}")):
        day = day_of(path)
        if day is None or day >= today:
            continue                  # unrecognised names and today are safe
        if day < cutoff:
            try:
                size = path.stat().st_size
                path.unlink()
                log.info("Retention: removed %s (%.1f GB, %d days old)",
                         path.name, size / 2**30, (today - day).days)
            except OSError as exc:
                log.error("Could not remove %s: %s", path.name, exc)


def enforce_free_space() -> None:
    today = date.today()
    while True:
        free_gb = shutil.disk_usage(VIDEO_DIR).free / 2**30
        if free_gb >= MIN_FREE_GB:
            return
        candidates = []
        for path in VIDEO_DIR.glob(f"*.{RECORD_EXT}"):
            day = day_of(path)
            if day is not None and day < today:
                candidates.append((day, path))
        if not candidates:
            log.error("Only %.0f GB free but no old recordings to remove", free_gb)
            return
        _, victim = min(candidates)
        log.warning("Low disk (%.0f GB free): removing %s", free_gb, victim.name)
        try:
            victim.unlink()
        except OSError as exc:
            log.error("Could not remove %s: %s", victim.name, exc)
            return


def retention_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            purge_old_recordings()
            enforce_free_space()
        except Exception:
            log.exception("Retention pass failed")
        stop.wait(3600)


# ---------------------------------------------------------------------------
# PTZ control
# ---------------------------------------------------------------------------
class PTZ:
    """Serialises Pelco-D commands onto the RS-485 link."""

    def __init__(self, port: str, baud: int) -> None:
        self._port = port
        self._baud = baud
        self._lock = threading.Lock()
        self._ser: serial.Serial | None = None
        self._codec = pelcoD()
        self._open()

    def _open(self) -> None:
        """Open the by-id path, falling back to the raw device node.

        The fallback exists only so a replacement adapter (different serial
        number) still works; it logs loudly because the wrong device could be
        sitting on that name.
        """
        for port, is_fallback in ((self._port, False), (SERIAL_PORT_FALLBACK, True)):
            if not port or not Path(port).exists():
                continue
            try:
                self._ser = serial.Serial(port, self._baud, timeout=1)
                if is_fallback:
                    log.warning("Opened FALLBACK serial port %s - the by-id path "
                                "%s was missing. Verify this is the PTZ adapter.",
                                port, self._port)
                else:
                    log.info("Serial port %s open at %d baud", port, self._baud)
                return
            except Exception as exc:
                log.error("Serial port %s unavailable: %s", port, exc)
        self._ser = None
        log.error("No usable serial port for PTZ control")

    def send(self, command: str, *args) -> bool:
        """Emit one Pelco-D command. Reopens the port if it has dropped."""
        with self._lock:
            if self._ser is None or not self._ser.is_open:
                self._open()
            if self._ser is None:
                log.warning("Dropping %s - no serial port", command)
                return False
            try:
                self._ser.write(getattr(self._codec, command)(*args))
                return True
            except Exception as exc:
                log.error("Serial write failed for %s: %s", command, exc)
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None
                return False

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._ser is not None and self._ser.is_open

    def close(self) -> None:
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None


# ---------------------------------------------------------------------------
# PTZ command vocabulary - one source of truth for both HTTP and MQTT
# ---------------------------------------------------------------------------
# action -> (pelcoD method, fixed arguments)
PTZ_ACTIONS = {
    "pan_left":             ("panleft",    (PTZ_SPEED,)),
    "pan_right":            ("panright",   (PTZ_SPEED,)),
    "tilt_up":              ("tiltup",     (PTZ_SPEED,)),
    "tilt_down":            ("tiltdown",   (PTZ_SPEED,)),
    "stop":                 ("stop",       ()),
    "zoom_tele":            ("zoomtele",   ()),
    "zoom_wide":            ("zoomwide",   ()),
    "focus_near":           ("focusnear",  ()),
    "focus_far":            ("focusfar",   ()),
    "osd_menu":             ("auxon",      (2,)),
    "thermal_camera":       ("auxon",      (4,)),
    "visible_light_camera": ("auxoff",     (4,)),
    "windshield_wiper":     ("auxon",      (1,)),
    "tour_1":               ("gotopreset", (81,)),
    "tour_2":               ("gotopreset", (82,)),
}

# Actions that take a preset number rather than fixed arguments.
PTZ_PRESET_ACTIONS = {"set_preset": "setpreset", "goto_preset": "gotopreset"}

# The URLs the shipped web UI already calls, mapped onto the actions above.
# Keys must not change - templates/index.html posts to these exact paths.
HTTP_PTZ_ROUTES = {
    "/pan_left": "pan_left",
    "/pan_right": "pan_right",
    "/tilt_up": "tilt_up",
    "/tilt_down": "tilt_down",
    "/stop": "stop",
    "/zoom_tele": "zoom_tele",
    "/zoom_wide": "zoom_wide",
    "/focus_near": "focus_near",
    "/focus_far": "focus_far",
    "/OSD_menu": "osd_menu",
    "/Thermal_Camera": "thermal_camera",
    "/Visible_Light_Camera": "visible_light_camera",
    "/Windshield_Wiper": "windshield_wiper",
    "/Tour_1": "tour_1",
    "/Tour_2": "tour_2",
}


def execute_ptz(action: str, preset: int | None = None) -> tuple[bool, str]:
    """Run one PTZ action. Returns (ok, error message)."""
    if ptz is None:
        return False, "no serial link"
    if action in PTZ_PRESET_ACTIONS:
        if preset is None:
            return False, "preset number required"
        if not 1 <= preset <= 255:
            return False, "preset out of range"
        return ptz.send(PTZ_PRESET_ACTIONS[action], preset), ""
    entry = PTZ_ACTIONS.get(action)
    if entry is None:
        return False, f"unknown action '{action}'"
    method, args = entry
    return ptz.send(method, *args), ""


# ---------------------------------------------------------------------------
# Web application
# ---------------------------------------------------------------------------
app = Flask(__name__)
frames = FrameBuffer()
recorder = Recorder(frames)
ptz: PTZ | None = None
shutdown = threading.Event()
http_server = None

app.secret_key = SECRET_KEY or os.urandom(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Only mark the cookie HTTPS-only when we can actually serve HTTPS,
    # otherwise a cert problem would silently break login entirely.
    SESSION_COOKIE_SECURE=TLS_AVAILABLE,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS),
)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.session_protection = "strong"

# Endpoints reachable without a session: the login form and static assets.
PUBLIC_ENDPOINTS = {"login", "static"}


class User(UserMixin):
    """The single operator account. There is no user database by design."""

    def get_id(self) -> str:
        return WEB_USERNAME


@login_manager.user_loader
def load_user(user_id: str):
    return User() if user_id == WEB_USERNAME else None


# Failed-login throttling, keyed by client address. In-memory on purpose:
# one process, one operator, and a restart clearing it is acceptable.
_login_failures: dict[str, tuple[int, float]] = {}
_login_lock = threading.Lock()


def _lockout_remaining(addr: str) -> int:
    with _login_lock:
        _, until = _login_failures.get(addr, (0, 0.0))
    return max(0, int(until - time.time()))


def _note_failure(addr: str) -> None:
    with _login_lock:
        count, _ = _login_failures.get(addr, (0, 0.0))
        count += 1
        until = time.time() + LOGIN_LOCKOUT_SECONDS if count >= LOGIN_MAX_ATTEMPTS else 0.0
        _login_failures[addr] = (count, until)
    if until:
        log.warning("Locking out %s for %ds after %d failed logins",
                    addr, LOGIN_LOCKOUT_SECONDS, count)


def _clear_failures(addr: str) -> None:
    with _login_lock:
        _login_failures.pop(addr, None)


@app.after_request
def security_headers(response):
    """Baseline hardening.

    Deliberately no HSTS: the certificate is self-signed, and once a browser
    pins HSTS it refuses to let you click through the trust warning - that
    would lock you out of your own camera.

    'unsafe-inline' is needed because base.html and index.html carry inline
    <style> and <script> blocks. Everything else is 'self' now that Bootstrap,
    jQuery and the font are served locally.
    """
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'",
    )
    return response


@app.before_request
def require_login():
    if not AUTH_ENABLED or request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if current_user.is_authenticated:
        return None
    # Anything scripted gets a clean 401; a browser navigation gets the form.
    if request.method == "POST" or request.path in ("/camera", "/health"):
        return make_response(
            jsonify({"ok": False, "error": "authentication required"}), 401
        )
    return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_ENABLED:
        return redirect(url_for("index"))
    addr = request.remote_addr or "unknown"
    error = ""

    if request.method == "POST":
        wait = _lockout_remaining(addr)
        if wait:
            error = f"Too many attempts. Try again in {wait} seconds."
        else:
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if (username == WEB_USERNAME
                    and check_password_hash(WEB_PASSWORD_HASH, password)):
                _clear_failures(addr)
                login_user(User(), remember=False)
                log.info("Login succeeded for '%s' from %s", username, addr)
                nxt = request.args.get("next", "")
                # Only ever redirect within this site.
                if not nxt.startswith("/") or nxt.startswith("//"):
                    nxt = url_for("index")
                return redirect(nxt)
            _note_failure(addr)
            log.warning("Failed login for '%s' from %s", username, addr)
            error = "Incorrect username or password."

    return make_response(render_template("login.html", error=error), 401 if error else 200)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/camera")
def camera():
    def stream():
        last = frames.seq - 1
        while not shutdown.is_set():
            got = frames.wait_for(last, timeout=5.0)
            if got is None:
                continue              # no new frame yet; keep the socket open
            last, jpeg = got
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n"
                   b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                   + jpeg + b"\r\n")
    return Response(stream(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


def _register_ptz_routes() -> None:
    """Expose every PTZ action at the URL the existing web UI already uses."""
    for rule, action in HTTP_PTZ_ROUTES.items():
        def view(action=action):
            ok, err = execute_ptz(action)
            return make_response(
                jsonify({"ok": ok, "action": action, "error": err}), 200
            )
        view.__name__ = f"ptz_{action}"
        app.route(rule, methods=["POST"])(view)


_register_ptz_routes()


@app.route("/Set_preset", methods=["POST"])
def set_preset():
    return _preset("set_preset")


@app.route("/Goto_preset", methods=["POST"])
def goto_preset():
    return _preset("goto_preset")


def _preset(action: str):
    payload = request.get_json(silent=True) or {}
    try:
        number = int(payload.get("flabber"))
    except (TypeError, ValueError):
        return make_response(jsonify({"ok": False, "error": "bad preset"}), 400)
    ok, err = execute_ptz(action, number)
    status = 200 if ok or not err else 400
    return make_response(
        jsonify({"ok": ok, "preset": number, "error": err}), status
    )


@app.route("/Start_New_File", methods=["POST"])
def start_new_file():
    """Close today's file early and open a fresh one (kept as .partNN)."""
    ok = recorder.roll()
    return make_response(jsonify({"ok": ok}), 200)


@app.route("/Exit_program", methods=["POST"])
def exit_program():
    log.info("Restart requested from the web interface")
    threading.Thread(target=_deferred_exit, daemon=True).start()
    return make_response(
        jsonify({"ok": True, "message": "Restarting; systemd will bring it back"}),
        200,
    )


def _deferred_exit() -> None:
    time.sleep(1)                     # let the response flush
    graceful_stop()
    os._exit(0)


def current_state() -> dict:
    """One snapshot of service health, shared by /health and MQTT."""
    current = VIDEO_DIR / f"{date.today():%Y-%m-%d}.{RECORD_EXT}"
    exists = current.exists()
    return {
        "recording": recorder.alive,
        "ffmpeg_restarts": recorder.restarts,
        "ffmpeg_started": recorder.started_at.isoformat() if recorder.started_at else None,
        "preview_frames": frames.seq,
        "serial": ptz is not None and ptz.connected,
        "current_file": current.name if exists else None,
        "current_size_gb": round(current.stat().st_size / 2**30, 2) if exists else 0,
        "free_gb": round(shutil.disk_usage(VIDEO_DIR).free / 2**30, 1),
        "retention_days": RETENTION_DAYS,
    }


@app.route("/health")
def health():
    return jsonify(current_state())


# ---------------------------------------------------------------------------
# MQTT bridge
# ---------------------------------------------------------------------------
class MqttBridge:
    """Publishes camera state and accepts PTZ commands over MQTT.

    Strictly optional. If the broker is unreachable the camera carries on
    exactly as before; paho reconnects in the background on its own.

    Topics, all under MQTT_PREFIX:
        <prefix>/status      online | offline   (retained, offline via LWT)
        <prefix>/state       JSON health snapshot (retained)
        <prefix>/ptz/set     subscribed: {"action": "pan_left"} or "pan_left"
        <prefix>/ptz/result  outcome of the last command
    """

    def __init__(self, stop: threading.Event) -> None:
        self._stop = stop
        self._client: mqtt.Client | None = None

    def start(self) -> None:
        if not MQTT_ENABLED:
            log.info("MQTT bridge off - no credentials in the environment")
            return
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"{MQTT_PREFIX}-service"
        )
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        # The broker publishes this for us if we drop off without saying goodbye.
        client.will_set(f"{MQTT_PREFIX}/status", "offline", qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        self._client = client
        try:
            client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
            client.loop_start()
        except Exception:
            log.exception("MQTT bridge failed to start")
            self._client = None
            return
        log.info("MQTT bridge connecting to %s:%d as '%s', prefix '%s'",
                 MQTT_HOST, MQTT_PORT, MQTT_USERNAME, MQTT_PREFIX)
        threading.Thread(target=self._state_loop, name="mqtt-state",
                         daemon=True).start()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            log.error("MQTT connection refused: %s", reason_code)
            return
        log.info("MQTT connected")
        client.publish(f"{MQTT_PREFIX}/status", "online", qos=1, retain=True)
        client.subscribe(f"{MQTT_PREFIX}/ptz/set", qos=1)
        self._publish_state()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        if not self._stop.is_set():
            log.warning("MQTT disconnected (%s) - retrying", reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            raw = msg.payload.decode("utf-8", "replace").strip()
            if raw.startswith("{"):
                payload = json.loads(raw)
                action = str(payload.get("action", ""))
                preset = payload.get("preset")
                preset = int(preset) if preset is not None else None
            else:
                action, preset = raw, None      # bare action string also works
            ok, err = execute_ptz(action, preset)
            if not ok:
                log.warning("MQTT PTZ '%s' rejected: %s",
                            action, err or "serial write failed")
            client.publish(
                f"{MQTT_PREFIX}/ptz/result",
                json.dumps({"action": action, "ok": ok, "error": err}), qos=0
            )
        except Exception:
            log.exception("Unusable MQTT message on %s", msg.topic)

    def _state_loop(self) -> None:
        while not self._stop.wait(MQTT_STATE_INTERVAL):
            self._publish_state()

    def _publish_state(self) -> None:
        if self._client is None:
            return
        try:
            self._client.publish(f"{MQTT_PREFIX}/state",
                                 json.dumps(current_state()), qos=0, retain=True)
        except Exception:
            log.exception("Could not publish MQTT state")

    def stop_bridge(self) -> None:
        if self._client is None:
            return
        try:
            # Say goodbye properly so the retained status is accurate.
            self._client.publish(f"{MQTT_PREFIX}/status", "offline",
                                 qos=1, retain=True).wait_for_publish(timeout=2)
            self._client.loop_stop()
            self._client.disconnect()
            log.info("MQTT bridge stopped")
        except Exception:
            pass


mqtt_bridge = MqttBridge(shutdown)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def serve() -> None:
    """Serve the app over TLS. Blocks until the server stops."""
    global http_server

    from cheroot.wsgi import Server as WSGIServer

    server = WSGIServer((LISTEN_HOST, LISTEN_PORT), app,
                        numthreads=SERVER_THREADS, server_name="camera")
    have_cert = Path(TLS_CERT).exists() and Path(TLS_KEY).exists()
    if have_cert:
        from cheroot.ssl.builtin import BuiltinSSLAdapter

        adapter = BuiltinSSLAdapter(TLS_CERT, TLS_KEY)
        adapter.context.minimum_version = ssl.TLSVersion.TLSv1_2
        server.ssl_adapter = adapter
        log.info("Serving on https://%s:%d (TLS 1.2+)", LISTEN_HOST, LISTEN_PORT)
    else:
        log.warning("No certificate at %s - falling back to plain HTTP", TLS_CERT)
        log.info("Serving on http://%s:%d", LISTEN_HOST, LISTEN_PORT)

    http_server = server
    try:
        server.start()
    finally:
        try:
            server.stop()
        except Exception:
            pass


def graceful_stop() -> None:
    if shutdown.is_set():
        return
    shutdown.set()
    log.info("Shutting down")
    if http_server is not None:
        try:
            http_server.stop()
        except Exception:
            pass
    mqtt_bridge.stop_bridge()
    recorder.stop()
    if ptz:
        ptz.close()
    log.info("Stopped cleanly")


def handle_signal(sig, _frame) -> None:
    log.info("Signal %s received", sig)
    graceful_stop()
    os._exit(0)


def main() -> None:
    global ptz

    setup_logging()
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)

    log.info("Camera service starting in %s", BASE_DIR)
    now = datetime.now().astimezone()
    log.info("Local time is %s (%s) - daily files cut at this clock's midnight",
             now.isoformat(timespec="seconds"), now.tzname())
    if FONT is None:
        log.warning("No DejaVu font found - recording without a timestamp overlay")
    if AUTH_ENABLED:
        log.info("Web login required for user '%s'", WEB_USERNAME)
    else:
        log.warning("NO WEB AUTHENTICATION - set WEB_PASSWORD_HASH and "
                    "FLASK_SECRET_KEY in /etc/camera-service.env")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    ptz = PTZ(SERIAL_PORT, SERIAL_BAUD)

    threading.Thread(target=recorder.run, name="recorder", daemon=True).start()
    threading.Thread(target=retention_loop, args=(shutdown,),
                     name="retention", daemon=True).start()
    mqtt_bridge.start()

    serve()


if __name__ == "__main__":
    main()
