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

import logging
import logging.handlers
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import serial
from flask import Flask, Response, jsonify, make_response, render_template, request

from pelcoD import pelcoD

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent

VIDEO_DEVICE = "/dev/video0"
CAPTURE_SIZE = "1280x720"
# The capture dongle only offers discrete rates (5/10/20/25/30). Asking for an
# unsupported rate silently gets you the next one up - 15 yields 20.
CAPTURE_FPS = 10
RECORD_BITRATE = "2500k"
GOP_FRAMES = 20              # keyframe every 2s: seeking, and clean segment cuts

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
PREVIEW_FPS = 5
PREVIEW_QUALITY = 7          # mjpeg -q:v, 2 (best) .. 31 (worst)

VIDEO_DIR = BASE_DIR / "videos"
LOG_DIR = BASE_DIR / "logs"
RETENTION_DAYS = 14
MIN_FREE_GB = 50             # hard floor; oldest whole days go first

SERIAL_PORT = "/dev/ttyUSB0"
SERIAL_BAUD = 9600

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 5000

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
            f"[rec]format=yuv420p[recout];"
            f"[prv]fps={PREVIEW_FPS},scale={PREVIEW_WIDTH}:-2[prvout]"
        )

    def _cmd(self) -> list[str]:
        # stdin stays open on purpose: writing 'q' is the only way to make
        # ffmpeg close its output files cleanly. SIGTERM makes it exit at once.
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", CAPTURE_SIZE, "-framerate", str(CAPTURE_FPS),
            "-i", VIDEO_DEVICE,
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
        try:
            self._ser = serial.Serial(self._port, self._baud, timeout=1)
            log.info("Serial port %s open at %d baud", self._port, self._baud)
        except Exception as exc:
            self._ser = None
            log.error("Serial port %s unavailable: %s", self._port, exc)

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

    def close(self) -> None:
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None


# ---------------------------------------------------------------------------
# Web application
# ---------------------------------------------------------------------------
app = Flask(__name__)
frames = FrameBuffer()
recorder = Recorder(frames)
ptz: PTZ | None = None
shutdown = threading.Event()


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


def ptz_route(rule: str, command: str, *args) -> None:
    """Register a POST endpoint that emits one fixed Pelco-D command."""
    def view():
        ok = ptz.send(command, *args) if ptz else False
        return make_response(jsonify({"ok": ok, "command": command}), 200)
    view.__name__ = f"ptz_{rule.strip('/')}"
    app.route(rule, methods=["POST"])(view)


# Movement
ptz_route("/pan_left", "panleft", 25)
ptz_route("/pan_right", "panright", 25)
ptz_route("/tilt_up", "tiltup", 25)
ptz_route("/tilt_down", "tiltdown", 25)
ptz_route("/stop", "stop")
# Lens
ptz_route("/zoom_tele", "zoomtele")
ptz_route("/zoom_wide", "zoomwide")
ptz_route("/focus_near", "focusnear")
ptz_route("/focus_far", "focusfar")
# MIC 612 auxiliaries
ptz_route("/OSD_menu", "auxon", 2)
ptz_route("/Thermal_Camera", "auxon", 4)
ptz_route("/Visible_Light_Camera", "auxoff", 4)
ptz_route("/Windshield_Wiper", "auxon", 1)
# Tours live on presets 81 and 82
ptz_route("/Tour_1", "gotopreset", 81)
ptz_route("/Tour_2", "gotopreset", 82)


@app.route("/Set_preset", methods=["POST"])
def set_preset():
    return _preset("setpreset")


@app.route("/Goto_preset", methods=["POST"])
def goto_preset():
    return _preset("gotopreset")


def _preset(command: str):
    payload = request.get_json(silent=True) or {}
    try:
        number = int(payload.get("flabber"))
    except (TypeError, ValueError):
        return make_response(jsonify({"ok": False, "error": "bad preset"}), 400)
    if not 1 <= number <= 255:
        return make_response(jsonify({"ok": False, "error": "out of range"}), 400)
    ok = ptz.send(command, number) if ptz else False
    return make_response(jsonify({"ok": ok, "preset": number}), 200)


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


@app.route("/health")
def health():
    current = VIDEO_DIR / f"{date.today():%Y-%m-%d}.{RECORD_EXT}"
    return jsonify({
        "recording": recorder.alive,
        "ffmpeg_restarts": recorder.restarts,
        "ffmpeg_started": recorder.started_at.isoformat() if recorder.started_at else None,
        "preview_frames": frames.seq,
        "serial": ptz is not None and ptz._ser is not None,
        "current_file": current.name if current.exists() else None,
        "current_size_gb": round(current.stat().st_size / 2**30, 2) if current.exists() else 0,
        "free_gb": round(shutil.disk_usage(VIDEO_DIR).free / 2**30, 1),
        "retention_days": RETENTION_DAYS,
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def graceful_stop() -> None:
    if shutdown.is_set():
        return
    shutdown.set()
    log.info("Shutting down")
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

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    ptz = PTZ(SERIAL_PORT, SERIAL_BAUD)

    threading.Thread(target=recorder.run, name="recorder", daemon=True).start()
    threading.Thread(target=retention_loop, args=(shutdown,),
                     name="retention", daemon=True).start()

    log.info("Serving on http://%s:%d", LISTEN_HOST, LISTEN_PORT)
    app.run(host=LISTEN_HOST, port=LISTEN_PORT,
            debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
