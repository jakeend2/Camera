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

import faulthandler
import fcntl
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import paho.mqtt.client as mqtt
import serial
from flask import (Flask, Response, jsonify, make_response, redirect,
                   render_template, request, send_from_directory, url_for)
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
# How long a substream preview keeps running after the last viewer left.
# Long enough that switching between cameras and back is instant, short
# enough that nothing decodes video all night for an empty room.
PREVIEW_LINGER = _env_int("PREVIEW_LINGER", 60)

# Each camera records into VIDEO_ROOT/<cid>/. Subdirectories rather than
# filename prefixes: the day then genuinely IS the whole filename within a
# camera's directory, so DAY_FILE_RE, day_of() and the .partNN convention
# all keep working untouched, and two cameras can never collide on
# "2026-08-16.ts" in a probe cache keyed by bare basename.
VIDEO_ROOT = BASE_DIR / "videos"
VIDEO_DIR = VIDEO_ROOT          # rebound to the primary camera in main()
LOG_DIR = BASE_DIR / "logs"
# Both cameras keep the same window, so a day of footage always has both
# angles or neither - a mismatch is worse than a shorter archive, because
# you go looking for the other view of an incident and it is simply gone.
# 7 days of MIC (~30 GB/day) plus 7 of the 5 MP Reolink (58 GB/day) is
# about 616 GB against a ~808 GB budget.
RETENTION_DAYS = _env_int("RETENTION_DAYS", 7)
MIN_FREE_GB = _env_int("MIN_FREE_GB", 50)   # floor; oldest whole days go first


def _cam_env(cid: str, key: str, default: str, legacy: str = "") -> str:
    """Per-camera setting, falling back to the old flat key then the default.

    The flat form is what /etc/camera-service.env already contains for the
    original camera, written by install.sh. Honouring it means the existing
    deployment keeps working with no edit to that file.
    """
    value = os.environ.get(f"CAM_{cid.upper()}_{key}", "").strip()
    if not value and legacy:
        value = os.environ.get(legacy, "").strip()
    return value or default


def _cam_env_int(cid: str, key: str, default: int, legacy: str = "") -> int:
    try:
        return int(_cam_env(cid, key, str(default), legacy))
    except ValueError:
        return default


@dataclass(frozen=True)
class CameraConfig:
    """Everything that differs between one camera and another.

    Defaults describe this deployment's hardware, matching the project's
    existing habit: the service runs correctly unconfigured, and nothing here
    needs editing to move machines - that is what the env overrides are for.
    """
    cid: str                          # short id; names its directory and URLs
    name: str                         # what the UI shows
    kind: str                         # "v4l2" (analog capture) or "rtsp"
    source: str
    source_fallback: str = ""
    # Capture geometry only means anything for a local device; a remote
    # encoder decides its own and ignores anything we ask for.
    capture_size: str = "1280x720"
    capture_fps: int = 20
    # False when the source already delivers H.264: ffmpeg copies the
    # bitstream and costs almost nothing. Note that ANY filter - including a
    # drawtext overlay - forces a full decode and throws that away.
    encode: bool = True
    record_fps: int = 10
    record_bitrate: str = "2500k"
    overlay_timestamp: bool = True
    retention_days: int = 14
    preview_width: int = 640
    preview_fps: int = 20
    preview_quality: int = 7
    play_window_seconds: int = 120
    aspect: str = "16/9"
    capabilities: frozenset = frozenset()
    backoff_max: int = 60
    noise: tuple = ()
    input_extra: tuple = ()
    # Credentials for a network source. Kept apart from `source` so the URL
    # can be logged and repr'd without them, and so they can come from the
    # environment while the URL stays in code.
    user: str = ""
    password: str = ""
    # "split"     - one ffmpeg produces recording and preview (analog capture)
    # "substream" - a second process transcodes a low-res stream for preview
    # "none"      - no live preview for this camera
    preview: str = "split"
    preview_source: str = ""

    @property
    def has_ptz(self) -> bool:
        return "ptz" in self.capabilities

    @property
    def secrets(self) -> tuple:
        """Strings that must never reach a log or an error page."""
        return tuple(s for s in (self.password,
                                 urllib.parse.quote(self.password, safe=""))
                     if s)

    def authed_url(self, url: str = "") -> str:
        """A source URL with credentials inserted, percent-encoded.

        The password here contains shell metacharacters on this deployment,
        which is exactly why it is never interpolated into a shell string -
        ffmpeg is exec'd with an argument list, so quoting never applies.
        """
        url = url or self.source
        if not self.user or "://" not in url:
            return url
        scheme, _, rest = url.partition("://")
        creds = (urllib.parse.quote(self.user, safe="") + ":"
                 + urllib.parse.quote(self.password, safe=""))
        return f"{scheme}://{creds}@{rest}"

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

# Archive playback and clipping. Both are remux-only - ffmpeg copies the
# H.264 bitstream untouched, never re-encoding - so the cost is I/O, not CPU.
# Measured on this Pi: a 60s clip out of a 1.2 GB file takes 0.8s, and the
# first bytes of a playback stream arrive in 0.6s.
# Playback is served in windows rather than as one endless stream. Each is a
# complete, seekable MP4 - moov at the front, duration known - so the
# browser's own scrubber works inside it and the page only has to fetch a new
# one when you leave it. 120s keeps the file around 40 MB and about a second
# to cut; longer windows scrub better but start slower.
PLAY_WINDOW_SECONDS = _env_int("PLAY_WINDOW_SECONDS", 120)
# Finished windows are kept so a scrub backwards, a replay, or the browser's
# range requests cost nothing. Oldest go first once the cache is over budget.
WINDOW_CACHE_MB = _env_int("WINDOW_CACHE_MB", 1024)
CLIP_MAX_SECONDS = _env_int("CLIP_MAX_SECONDS", 1800)
# Concurrent archive jobs. The live recording always wins: these run niced
# and at idle I/O priority, and a request that cannot get a slot within
# MEDIA_WAIT seconds is refused rather than queued behind a viewer.
MEDIA_JOBS = _env_int("MEDIA_JOBS", 3)
MEDIA_WAIT = 3           # a request that cannot get a slot in 3s will not
                         # get one in 8, and waiting holds a worker thread
def _cache_dir() -> Path:
    """Somewhere to put clip scratch and cached playback windows.

    The unit runs ProtectSystem=strict, so this cannot simply be a directory
    inside the install tree: that filesystem is read-only to the service no
    matter who owns the directory. Candidates are tried in order and the
    first one that can actually hold a file wins.

    /var/cache/camera, via systemd's CacheDirectory=, is the intended home -
    outside the app tree, created with the right ownership, and cleaned up by
    `systemctl clean`. A service whose unit predates this feature has no such
    directory, so the fallback is beside the logs, which the unit already
    grants write access to. Nothing here needs privilege, and the moment the
    unit is reinstalled the cache moves to /var/cache/camera by itself.
    """
    candidates = [
        os.environ.get("CACHE_DIR", "").strip(),
        os.environ.get("CACHE_DIRECTORY", "").split(":")[0].strip(),
        str(LOG_DIR / "media-cache"),
        str(BASE_DIR / "clips"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".writable"
            probe.write_bytes(b"")
            probe.unlink()
            return path
        except OSError:
            continue
    return BASE_DIR / "clips"       # nothing worked; startup will say so


CLIP_DIR = _cache_dir()
NICE = ["nice", "-n", "10", "ionice", "-c", "3"]

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 5000
SERVER_THREADS = _env_int("SERVER_THREADS", 32)
# Each MJPEG viewer holds a worker for as long as it watches, so a browser
# tab costs one per visible feed. Cheroot workers are ~32-64 KB of RSS, so
# headroom here is far cheaper than a pool exhaustion that takes out
# /health - the one page that could explain the outage.

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

# Seconds of no new frame before a preview response gives up and frees its
# worker thread. The page reconnects on its own; a pinned worker does not.
STREAM_IDLE_GIVEUP = _env_int("STREAM_IDLE_GIVEUP", 15)   # x2s = 30s

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

    def wake(self) -> None:
        """Release every waiting reader without publishing a frame.

        Used at shutdown: a preview generator parked in wait_for() holds a
        cheroot worker thread, and cheroot's stop() waits on its workers. One
        browser tab watching the live feed was enough to stall shutdown until
        systemd lost patience and SIGKILLed the service mid-write.
        """
        with self._cond:
            self._cond.notify_all()

    def latest(self) -> bytes | None:
        """The most recent frame without waiting, or None before first frame."""
        with self._cond:
            return self._frame

    @property
    def seq(self) -> int:
        with self._cond:
            return self._seq


def _widen_pipe(fileobj, target: int = 1 << 20) -> None:
    """Grow ffmpeg's stdout pipe so a slow reader cannot stall the capture.

    A pipe defaults to 64 KB. An MJPEG frame at 640 wide and q:v 7 is roughly
    25-35 KB, so the default holds about two frames - a tenth of a second at
    20 fps. ffmpeg 5.1 muxes on its main thread, so once that pipe fills, the
    write to pipe:1 blocks the whole process including the v4l2 read, the
    dongle's ring buffer overwrites, and frames are lost from the RECORDING
    with nothing logged. 1 MB is the unprivileged ceiling
    (/proc/sys/fs/pipe-max-size) and buys roughly 35 frames of slack.
    """
    try:
        fcntl.fcntl(fileobj.fileno(), 1031, target)      # 1031 = F_SETPIPE_SZ
    except (OSError, AttributeError, ValueError) as exc:
        log.debug("Could not widen the preview pipe: %s", exc)


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


class ArchiveIndex:
    """Wall-clock map of the recordings on disk.

    Each .ts file is one uninterrupted run of the recorder: a restart closes
    the current file and opens a .partNN. Nothing inside a file stores
    wall-clock time, so its span has to be derived from the filesystem.

    The anchor is the file's BIRTH time - ext4 keeps it, `stat -c %W` reads
    it, and a rename preserves it, so it survives the restart shuffle that
    turns today's file into a .partNN. Checked against the timestamp burned
    into the video at offset 0: part24 predicted 15:07:42 and read 15:07:42,
    part19 predicted 13:17:08 and read 13:17:07.

    mtime was tried first and is wrong: it is when the file was last touched,
    which trails the last recorded frame by anything from 2s to 30s depending
    on how that ffmpeg died. That is also why a segment's end is start +
    duration and never mtime - claiming footage that was never written makes
    playback seek into nothing.

    Probing costs ~0.6s per file and a day can hold dozens, so results are
    cached by (size, mtime) and persisted: a finished file is probed once
    ever, even across service restarts. Only the growing file is re-probed.
    """

    TTL = 5.0            # seconds before a refresh re-examines the directory

    def __init__(self, video_dir: Path, index_file: Path) -> None:
        # One index per camera, over one directory, persisted to its own file.
        # Sharing either across cameras silently mis-attributes footage: the
        # probe cache is keyed on bare filename and both cameras produce
        # "2026-08-16.ts", and the overlap-trim below would clamp one camera's
        # segment against the other's.
        self._video_dir = video_dir
        self._index_file = index_file
        self._lock = threading.Lock()
        self._probe_cache: dict[str, tuple] = {}
        self._segments: list[dict] = []
        self._checked = 0.0
        self._load()

    @property
    def index_file(self) -> Path:
        """Where this camera's probe cache is persisted."""
        return self._index_file

    @property
    def video_dir(self) -> Path:
        """The directory this index describes."""
        return self._video_dir

    # -- persistence --------------------------------------------------------
    def _load(self) -> None:
        try:
            raw = json.loads(self._index_file.read_text())
            self._probe_cache = {k: tuple(v) for k, v in raw.items()}
            log.info("Archive index: %d cached probes", len(self._probe_cache))
        except FileNotFoundError:
            pass
        except Exception:
            log.warning("Archive index cache unreadable - rebuilding")

    def _save(self) -> None:
        try:
            self._index_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._index_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._probe_cache))
            tmp.replace(self._index_file)
        except Exception:
            log.warning("Could not persist the archive index", exc_info=True)

    # -- probing ------------------------------------------------------------
    @staticmethod
    def _birth_times(paths: list) -> dict:
        """Birth time per file, in one stat(1) call rather than one each.

        Returns {name: epoch}, omitting anything the filesystem cannot
        answer for - callers fall back to mtime minus duration there.
        """
        if not paths:
            return {}
        try:
            out = subprocess.run(
                ["stat", "-c", "%W|%n"] + [str(p) for p in paths],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
            )
            found = {}
            for line in out.stdout.decode("utf-8", "replace").splitlines():
                birth, _, name = line.partition("|")
                try:
                    epoch = int(birth)
                except ValueError:
                    continue
                if epoch > 0:
                    found[Path(name).name] = epoch
            return found
        except Exception:
            return {}

    @staticmethod
    def _probe(path: Path):
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(path)],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
            )
            return float(out.stdout.decode().strip())
        except Exception:
            return None

    def refresh(self, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            if not force and now - self._checked < self.TTL:
                return
            self._checked = now
            paths = sorted(self._video_dir.glob("*." + RECORD_EXT))
            births = self._birth_times(paths)
            if paths and not births:
                log.warning("No birth times available - archive times will be "
                            "less accurate on this filesystem")
            segments, dirty = [], False
            for path in paths:
                day = day_of(path)
                if day is None:
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                if st.st_size == 0:
                    continue
                hit = self._probe_cache.get(path.name)
                # Re-probe whenever the file has changed at all - which is
                # exactly the file still being written to.
                if not hit or hit[0] != st.st_size or hit[1] != int(st.st_mtime):
                    duration = self._probe(path)
                    if duration is None or duration <= 0:
                        continue
                    self._probe_cache[path.name] = (st.st_size,
                                                    int(st.st_mtime), duration)
                    dirty = True
                else:
                    duration = hit[2]
                # Birth time is when the recorder opened this file, which is
                # the first frame in it. mtime is only where it stopped being
                # touched, and can trail the last frame by half a minute.
                start = births.get(path.name)
                if start is None:
                    start = st.st_mtime - duration
                segments.append({
                    "name": path.name,
                    "day": day.isoformat(),
                    "start": float(start),
                    "end": float(start) + duration,
                    "duration": duration,
                    "size": st.st_size,
                })
            segments.sort(key=lambda s: s["start"])
            # A file's birth time is a fact; its duration is ffmpeg's
            # estimate, and one file in thirty-odd over-reports by a few
            # seconds - enough to run past the moment the next recording
            # actually began. Trust the birth time and trim the estimate, so
            # no instant is ever claimed by two files and the timeline stays
            # strictly ordered.
            for earlier, later in zip(segments, segments[1:]):
                if earlier["end"] > later["start"]:
                    earlier["end"] = later["start"]
                    earlier["duration"] = max(
                        0.0, earlier["end"] - earlier["start"])
            segments = [s for s in segments if s["duration"] > 0.5]
            self._segments = segments
            if dirty:
                # Forget files retention has since deleted.
                live = {s["name"] for s in segments}
                self._probe_cache = {k: v for k, v in self._probe_cache.items()
                                     if k in live}
                self._save()

    # -- queries ------------------------------------------------------------
    def days(self) -> list[str]:
        self.refresh()
        return sorted({s["day"] for s in self._segments}, reverse=True)

    def segments(self, day: date) -> list[dict]:
        self.refresh()
        key = day.isoformat()
        return [s for s in self._segments if s["day"] == key]

    def locate(self, day: date, offset: float):
        """Map seconds-past-midnight to (segment, offset into that file).

        None when the moment falls in a gap - the recorder was down then.
        """
        target = self.midnight(day) + offset
        for seg in self.segments(day):
            if seg["start"] <= target < seg["end"]:
                return seg, target - seg["start"]
        return None

    def next_after(self, day: date, offset: float):
        """Seconds-past-midnight of the next available footage, or None."""
        target = self.midnight(day) + offset
        starts = [s["start"] for s in self.segments(day) if s["start"] > target]
        return min(starts) - self.midnight(day) if starts else None

    def overlapping(self, day: date, start: float, end: float) -> list[dict]:
        """Every segment touching [start, end], with the cut needed from each.

        Each entry says where to seek into that file and how much to take, so
        the caller can make one fast seek per file instead of asking ffmpeg to
        seek through a concatenation - measured at 2.3s versus 28.8s.
        """
        t0 = self.midnight(day) + start
        t1 = self.midnight(day) + end
        cuts = []
        for seg in self.segments(day):
            lo, hi = max(t0, seg["start"]), min(t1, seg["end"])
            if hi - lo <= 0.05:
                continue
            cuts.append({"name": seg["name"], "seek": lo - seg["start"],
                         "take": hi - lo, "start": lo, "end": hi})
        return cuts

    @staticmethod
    def midnight(day: date) -> float:
        return datetime.combine(day, datetime.min.time()).timestamp()


class Recorder:
    """Owns the ffmpeg subprocess and keeps it running."""

    def __init__(self, cam: "Camera") -> None:
        self.cam = cam
        self.cfg = cam.cfg
        self._buf = cam.frames
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

    @property
    def wants_preview(self) -> bool:
        """True when this recorder's ffmpeg also writes MJPEG to stdout."""
        return self.cfg.preview == "split"

    def _segment_args(self) -> list[str]:
        """The daily-file arguments, identical for every source."""
        return [
            "-f", "segment",
            "-segment_time", "86400",
            "-segment_atclocktime", "1",
            "-segment_format", RECORD_FORMAT,
            "-strftime", "1",
            # Load-bearing: it restarts each segment near PTS 0, which keeps
            # 86400s inside the 33-bit 90 kHz MPEG-TS wrap window (~95443s).
            # This is the only reason 24-hour files work at all.
            "-reset_timestamps", "1",
            str(self.cam.video_dir / f"%Y-%m-%d.{RECORD_EXT}"),
        ]

    def _rtsp_cmd(self) -> list[str]:
        """Record a network camera that already speaks H.264.

        No filter graph of any kind. drawtext and split operate on decoded
        frames, so the presence of ANY filter forces a full decode of a
        2560x1920 stream - which alone would exceed what this Pi has left.
        With -c copy the bitstream is written through untouched and the cost
        is I/O, not CPU. The camera burns its own timestamp in firmware, and
        the archive index derives wall-clock from file birth time rather than
        from the picture, so nothing needs an overlay from us.
        """
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            *self.cfg.input_extra,
            "-i", self.cfg.authed_url(),
            "-map", "0:v:0", "-c:v", "copy", "-an",
            *self._segment_args(),
        ]

    def _cmd(self) -> list[str]:
        if self.cfg.kind == "rtsp":
            return self._rtsp_cmd()
        # stdin stays open on purpose: writing 'q' is the only way to make
        # ffmpeg close its output files cleanly. SIGTERM makes it exit at once.
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", self.cfg.capture_size,
            "-framerate", str(self.cfg.capture_fps),
            "-i", self._video_device(),
            "-filter_complex", self._filter_complex(),
            # One file per calendar day, cut exactly at local midnight.
            "-map", "[recout]",
            *RECORD_ENCODER,
            "-b:v", self.cfg.record_bitrate, "-g", str(GOP_FRAMES),
            *self._segment_args(),
            # Low-rate preview for the browser.
            "-map", "[prvout]",
            "-c:v", "mjpeg", "-q:v", str(PREVIEW_QUALITY), "-f", "mjpeg", "pipe:1",
        ]

    # -- lifecycle --------------------------------------------------------
    def _preserve_todays_file(self) -> None:
        """ffmpeg would truncate an existing file for today, so rename it first."""
        stamp = f"{date.today():%Y-%m-%d}"
        video_dir = self.cam.video_dir
        today = video_dir / f"{stamp}.{RECORD_EXT}"
        if not today.exists():
            return
        for n in range(1, 100):
            alt = video_dir / f"{stamp}.part{n:02d}.{RECORD_EXT}"
            if not alt.exists():
                today.rename(alt)
                log.info("Kept earlier recording for today as %s", alt.name)
                return
        # Past 99 restarts in one day, fall back to a name that cannot collide.
        # Leaving the file in place instead would hand it to ffmpeg's segment
        # muxer, which opens it O_TRUNC and destroys the whole accumulated day.
        # DAY_FILE_RE matches part\d+, so a unix stamp stays parseable and the
        # archive index still attributes it to the right day.
        alt = video_dir / f"{stamp}.part{int(time.time())}.{RECORD_EXT}"
        today.rename(alt)
        log.error("Over 99 same-day restarts; kept %s as %s", today.name, alt.name)

    def _scrub(self, text: str) -> str:
        """Remove anything secret before it reaches a log.

        ffmpeg echoes its input URL in several error messages, and that URL
        carries the camera's password.
        """
        for secret in self.cfg.secrets:
            text = text.replace(secret, "***")
        return text

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        suppressed = 0
        for raw in iter(proc.stderr.readline, b""):
            line = self._scrub(raw.decode("utf-8", "replace").strip())
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
            self.cam.video_dir.mkdir(parents=True, exist_ok=True)
            self._preserve_todays_file()

            cmd = self._cmd()
            if self.cfg.kind == "rtsp":
                log.info("[%s] Starting ffmpeg (rtsp, copy, no re-encode)",
                         self.cam.cid)
            else:
                log.info("[%s] Starting ffmpeg (%s @ %s fps, %s)", self.cam.cid,
                         self.cfg.capture_size, self.cfg.capture_fps,
                         self.cfg.record_bitrate)
            try:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE,
                    stdout=(subprocess.PIPE if self.wants_preview
                            else subprocess.DEVNULL),
                    stderr=subprocess.PIPE,
                    bufsize=0, cwd=str(BASE_DIR),
                )
                if self.wants_preview:
                    _widen_pipe(proc.stdout)
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
            if self.wants_preview:
                try:
                    pump_preview(proc.stdout, self._buf, self._stop)
                except Exception:
                    log.exception("[%s] Preview pump died", self.cam.cid)
            else:
                # Nothing to read: this ffmpeg writes only to disk. Poll so a
                # stop request is still noticed promptly.
                while proc.poll() is None and not self._stop.is_set():
                    self._stop.wait(1.0)

            rc = self._reap(proc)
            if self._stop.is_set():
                return

            ran = time.monotonic() - began
            self.restarts += 1
            log.error("[%s] ffmpeg exited rc=%s after %.0fs - restarting",
                      self.cam.cid, rc, ran)
            # A long run means the failure was transient; reset the backoff.
            backoff = 2 if ran > 60 else min(backoff * 2, self.cfg.backoff_max)
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


class Preview:
    """An on-demand MJPEG feed for a camera that cannot produce one itself.

    The analog camera gets its preview free: one ffmpeg already decodes the
    capture, so a second output costs only the MJPEG encode. A network camera
    hands over H.264 that is copied to disk untouched, and putting any filter
    in that path would force a full decode of the main stream - which is the
    one thing this design refuses to do. So the preview comes from the
    camera's own low-resolution substream in a separate process: 640x480 at
    10fps, decoded and re-encoded to MJPEG for a couple of percent of a core.

    It runs only while someone is watching, plus PREVIEW_LINGER seconds. That
    matters less for CPU than for the camera: every RTSP session is a
    resource on the device itself, and holding one open permanently for a
    feed nobody is looking at is the kind of thing that eventually collides
    with the recording.
    """

    def __init__(self, cam: "Camera") -> None:
        self.cam = cam
        self.cfg = cam.cfg
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._last_want = 0.0
        self.started_at: datetime | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def want(self) -> None:
        """Register interest. Starts the feed if it is not already up."""
        self._last_want = time.monotonic()
        if self.running:
            return
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name=f"preview-{self.cam.cid}", daemon=True)
            self._thread.start()
            log.info("[%s] Preview starting (substream)", self.cam.cid)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def _cmd(self) -> list[str]:
        # No scale filter: the substream is natively 640x480, so asking for a
        # resize would add work to no purpose.
        return [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
            *self.cfg.input_extra,
            "-i", self.cfg.authed_url(self.cfg.preview_source or self.cfg.source),
            "-an", "-vf", f"fps={self.cfg.preview_fps}",
            "-c:v", "mjpeg", "-q:v", str(self.cfg.preview_quality),
            "-f", "mjpeg", "pipe:1",
        ]

    def _idle(self) -> bool:
        return time.monotonic() - self._last_want > PREVIEW_LINGER

    def _run(self) -> None:
        backoff = 2
        try:
            while not self._stop.is_set() and not self._idle():
                try:
                    proc = subprocess.Popen(
                        self._cmd(), stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        bufsize=0, cwd=str(BASE_DIR))
                    _widen_pipe(proc.stdout)
                except Exception:
                    log.exception("[%s] Could not start the preview",
                                  self.cam.cid)
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, self.cfg.backoff_max)
                    continue

                with self._lock:
                    self._proc = proc
                    self.started_at = datetime.now()
                threading.Thread(target=self.cam.recorder._drain_stderr,
                                 args=(proc,), daemon=True).start()

                began = time.monotonic()
                try:
                    pump_preview(proc.stdout, self.cam.frames, self._idle_event())
                except Exception:
                    log.exception("[%s] Preview pump died", self.cam.cid)
                Recorder._reap(proc)
                if self._stop.is_set() or self._idle():
                    break
                ran = time.monotonic() - began
                log.warning("[%s] Preview ended after %.0fs - restarting",
                            self.cam.cid, ran)
                backoff = 2 if ran > 60 else min(backoff * 2, self.cfg.backoff_max)
                self._stop.wait(backoff)
        finally:
            with self._lock:
                proc, self._proc = self._proc, None
            if proc is not None and proc.poll() is None:
                Recorder._reap(proc)
            log.info("[%s] Preview stopped", self.cam.cid)

    def _idle_event(self) -> threading.Event:
        """An Event that becomes set when the feed should wind down.

        pump_preview() takes a stop Event, and this feed has two reasons to
        stop: an explicit shutdown, or nobody watching any more. A tiny
        watcher thread bridges the second to the first.
        """
        ev = threading.Event()

        def watch():
            while not ev.is_set():
                if self._stop.is_set() or self._idle() or shutdown.is_set():
                    ev.set()
                    return
                time.sleep(1.0)

        threading.Thread(target=watch, daemon=True).start()
        return ev


class Camera:
    """One camera and everything it owns.

    Nothing mutable is shared between cameras. That is deliberate rather than
    tidy: the three worst ways this could go wrong - one camera's archive
    index trimming another's segments, a probe cache collision on identical
    day filenames, and a playback window cache serving the wrong camera's
    video with a cheerful 200 - are all silent wrong-data failures. Giving
    each camera its own directory, index and cache makes them impossible by
    construction instead of merely absent.
    """

    def __init__(self, cfg: CameraConfig) -> None:
        self.cfg = cfg
        self.cid = cfg.cid
        self.video_dir = VIDEO_ROOT / cfg.cid
        self.index = ArchiveIndex(
            self.video_dir, LOG_DIR / f"archive-index-{cfg.cid}.json")
        self.frames = FrameBuffer()
        self.recorder = Recorder(self)
        # Only for cameras whose recording pipeline cannot also make a preview.
        self.preview = (Preview(self) if cfg.preview == "substream" else None)

    def __repr__(self) -> str:
        return f"<Camera {self.cid} ({self.cfg.kind})>"


CAMERAS: dict[str, Camera] = {}
PRIMARY_CAMERA_ID = _env("PRIMARY_CAMERA", "mic612")


def primary() -> Camera:
    """The camera the un-parameterised routes still refer to."""
    return CAMERAS[PRIMARY_CAMERA_ID]


def camera_configs() -> list[CameraConfig]:
    """The cameras this host runs, in display order."""
    return [
        CameraConfig(
            cid="mic612", name="MIC 612", kind="v4l2",
            source=_cam_env("mic612", "SOURCE", VIDEO_DEVICE,
                            legacy="VIDEO_DEVICE"),
            source_fallback=VIDEO_DEVICE_FALLBACK,
            capture_size=_cam_env("mic612", "CAPTURE_SIZE", CAPTURE_SIZE,
                                  legacy="CAPTURE_SIZE"),
            capture_fps=_cam_env_int("mic612", "CAPTURE_FPS", CAPTURE_FPS,
                                     legacy="CAPTURE_FPS"),
            encode=True,                      # analog in: there is no choice
            record_fps=_cam_env_int("mic612", "RECORD_FPS", RECORD_FPS,
                                    legacy="RECORD_FPS"),
            record_bitrate=_cam_env("mic612", "RECORD_BITRATE", RECORD_BITRATE,
                                    legacy="RECORD_BITRATE"),
            overlay_timestamp=True,           # nothing else stamps this camera
            retention_days=_cam_env_int("mic612", "RETENTION_DAYS",
                                        RETENTION_DAYS, legacy="RETENTION_DAYS"),
            preview_width=PREVIEW_WIDTH, preview_fps=PREVIEW_FPS,
            preview_quality=PREVIEW_QUALITY,
            play_window_seconds=PLAY_WINDOW_SECONDS,
            aspect="16/9",
            capabilities=frozenset({"ptz", "presets", "imager", "wiper",
                                    "osd", "tour"}),
            backoff_max=60,
            noise=FFMPEG_NOISE,
            preview="split",
        ),
        CameraConfig(
            cid="backyard", name="Backyard", kind="rtsp",
            # No credentials in the URL: they come from the environment and
            # are inserted, percent-encoded, only when the command is built.
            source=_cam_env("backyard", "SOURCE",
                            "rtsp://192.168.1.126:554/h264Preview_01_main"),
            user=_cam_env("backyard", "USER", ""),
            password=_cam_env("backyard", "PASS", ""),
            # -timeout, NOT -stimeout: verified against this ffmpeg build
            # (5.1.9). Getting it wrong either makes ffmpeg refuse to start
            # (loud) or leaves a black-holed socket hanging forever with the
            # supervisor never seeing an exit (silent, and much worse).
            # TCP because a dropped UDP packet under -c copy is corruption
            # written straight into the archive.
            input_extra=tuple(_cam_env(
                "backyard", "INPUT_EXTRA",
                "-rtsp_transport tcp -timeout 5000000").split()),
            encode=False,           # already h264 High - copy, ~free
            overlay_timestamp=False,  # firmware OSD; a filter would force a decode
            retention_days=_cam_env_int("backyard", "RETENTION_DAYS",
                                        RETENTION_DAYS),
            # Measured: 5.35 Mbit/s, 2560x1920 at 25fps, GOP 2.0s.
            play_window_seconds=_cam_env_int("backyard",
                                             "PLAY_WINDOW_SECONDS", 60),
            aspect="4/3",           # 2560x1920 - not 16:9 like the MIC
            capabilities=frozenset(),   # fixed bullet: no motors
            backoff_max=30,
            # From the camera's own 640x480 substream in a separate
            # process - the main stream is copied to disk untouched and
            # must stay that way.
            preview="substream",
            preview_fps=10, preview_quality=8, preview_width=640,
            preview_source=_cam_env(
                "backyard", "PREVIEW_SOURCE",
                "rtsp://192.168.1.126:554/h264Preview_01_sub"),
            noise=("RTP: missed", "max delay reached", "Non-monotonous DTS",
                   "first_dts"),
        ),
    ]


def build_cameras() -> None:
    """Create every camera's object graph and its directory on disk."""
    global VIDEO_DIR, frames, recorder, archive
    wanted = [c.strip() for c in _env("CAMERAS", "").split(",") if c.strip()]
    for cfg in camera_configs():
        if wanted and cfg.cid not in wanted:
            log.info("Camera %s defined but not in CAMERAS - skipping", cfg.cid)
            continue
        if cfg.kind == "rtsp" and not cfg.password:
            log.error("Camera %s has no password (set CAM_%s_PASS) - skipping",
                      cfg.cid, cfg.cid.upper())
            continue
        cam = Camera(cfg)
        cam.video_dir.mkdir(parents=True, exist_ok=True)
        CAMERAS[cfg.cid] = cam
        log.info("Camera %-10s %-14s -> %s (retention %dd, %s)",
                 cfg.cid, cfg.kind, cam.video_dir, cfg.retention_days,
                 "encode" if cfg.encode else "copy")
    if PRIMARY_CAMERA_ID not in CAMERAS:
        raise SystemExit(f"PRIMARY_CAMERA '{PRIMARY_CAMERA_ID}' is not defined")
    # The routes still speak in terms of a single camera; point the names they
    # use at the primary one until Step 3 parameterises them.
    cam = primary()
    VIDEO_DIR = cam.video_dir
    frames = cam.frames
    recorder = cam.recorder
    archive = cam.index


# ---------------------------------------------------------------------------
# Retention - whole calendar days, keyed off the filename not mtime
# ---------------------------------------------------------------------------
def purge_old_recordings() -> None:
    """Drop each camera's recordings past its own retention window."""
    today = date.today()
    for cam in CAMERAS.values():
        cutoff = today - timedelta(days=cam.cfg.retention_days)
        for path in sorted(cam.video_dir.glob(f"*.{RECORD_EXT}")):
            day = day_of(path)
            if day is None or day >= today:
                continue              # unrecognised names and today are safe
            if day < cutoff:
                # Per-file, so one EACCES cannot abort the pass and with it
                # the enforce_free_space() call that follows.
                try:
                    size = path.stat().st_size
                    path.unlink()
                    log.info("Retention[%s]: removed %s (%.1f GB, %d days old)",
                             cam.cid, path.name, size / 2**30,
                             (today - day).days)
                except OSError as exc:
                    log.error("Could not remove %s: %s", path.name, exc)


def stray_recording_dirs() -> list[Path]:
    """Directories under videos/ that no registered camera owns.

    Retention cannot delete what it cannot see, but the disk still charges
    for it - so an orphan left by a renamed or removed camera would be paid
    for by deleting a live camera's footage instead. Report them loudly
    rather than deleting anything automatically.
    """
    if not VIDEO_ROOT.exists():
        return []
    known = {cam.video_dir.name for cam in CAMERAS.values()}
    return [d for d in VIDEO_ROOT.iterdir()
            if d.is_dir() and d.name not in known]


def enforce_free_space() -> None:
    today = date.today()
    for stray in stray_recording_dirs():
        log.warning("videos/%s belongs to no registered camera - retention "
                    "will never touch it, but the disk still charges for it",
                    stray.name)
    while True:
        free_gb = shutil.disk_usage(VIDEO_ROOT).free / 2**30
        if free_gb >= MIN_FREE_GB:
            return
        # Oldest day across ALL cameras loses, so the emergency path cannot
        # favour whichever camera happens to be listed first.
        candidates = []
        for cam in CAMERAS.values():
            for path in cam.video_dir.glob(f"*.{RECORD_EXT}"):
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
            # Before measuring free space, reclaim scratch older than an hour.
            # It lives on the filesystem enforce_free_space() measures, so
            # otherwise abandoned clips are paid for out of the archive.
            sweep_clips(max_age_seconds=3600)
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
#
# The aux and tour numbers are VERIFIED on this physical camera and must
# never be renumbered. Aux assignments are remappable in the camera's own
# Pelco AUX Setup menu: this unit's aux 2 (OSD open) is a local remap - the
# factory-documented opener is Set Preset 95 - and the imager toggle is
# documented at aux 5 on factory defaults but lives at aux 4 here. A factory
# reset of the camera would move both; the fallbacks are documented in
# pelcoD.openmenu() and the deploy README, but the live numbers stay.
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
    "iris_open":            ("irisopen",   ()),
    "iris_close":           ("irisclose",  ()),
    "osd_menu":             ("auxon",      (2,)),
    "thermal_camera":       ("auxon",      (4,)),
    "visible_light_camera": ("auxoff",     (4,)),
    "windshield_wiper":     ("auxon",      (1,)),
    "wiper_off":            ("auxoff",     (1,)),
    "tour_1":               ("gotopreset", (81,)),
    "tour_2":               ("gotopreset", (82,)),
}

# Motion actions whose first argument is a speed the client may override.
PTZ_SPEED_ACTIONS = {"pan_left", "pan_right", "tilt_up", "tilt_down"}

# Actions that take a preset number rather than fixed arguments.
PTZ_PRESET_ACTIONS = {"set_preset": "setpreset", "goto_preset": "gotopreset",
                      "clear_preset": "clearpreset"}

# Preset numbers a user may store scenes in. Everything else is refused:
# 33/34 trigger flip/home actions, 62 is the washer nozzle position, and
# 80-99 is the Bosch special band - 81/82 run the tours (verified), 92/93
# WRITE the AutoScan pan limits, 95 opens the setup menu, and 97 is
# FastAddress, which re-addresses the camera on the bus. The tours stay
# reachable as named actions; raw numbers in these bands never pass.
RESERVED_PRESETS = frozenset({33, 34, 62}) | frozenset(range(80, 100))
PRESET_MAX = 79


def preset_allowed(preset: int) -> bool:
    return 1 <= preset <= PRESET_MAX and preset not in RESERVED_PRESETS


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
    "/iris_open": "iris_open",
    "/iris_close": "iris_close",
    "/OSD_menu": "osd_menu",
    "/Thermal_Camera": "thermal_camera",
    "/Visible_Light_Camera": "visible_light_camera",
    "/Windshield_Wiper": "windshield_wiper",
    "/Wiper_Off": "wiper_off",
    "/Tour_1": "tour_1",
    "/Tour_2": "tour_2",
}

# Last imager we COMMANDED. RS-485 is one-way - there is no readback - so
# this is an assumption, not ground truth: MQTT or a camera-side change can
# move the real state behind our back, and a service restart forgets it.
# The UI is told exactly that.
imager_state = "unknown"


def ptz_camera_or_error(cam) -> str:
    """Empty string if this camera can be driven, else why it cannot.

    A fixed camera has no motors, so a PTZ request against it is a mistake
    worth naming rather than a silent no-op that looks like a broken camera.
    """
    if cam is None:
        return "no such camera"
    if not cam.cfg.has_ptz:
        return f"{cam.cfg.name} has no pan/tilt/zoom"
    return ""


def execute_ptz(action: str, preset: int | None = None,
                speed: int | None = None) -> tuple[bool, str]:
    """Run one PTZ action. Returns (ok, error message)."""
    global imager_state
    if ptz is None:
        return False, "no serial link"
    if action in PTZ_PRESET_ACTIONS:
        if preset is None:
            return False, "preset number required"
        if not preset_allowed(preset):
            return False, "reserved preset"
        return ptz.send(PTZ_PRESET_ACTIONS[action], preset), ""
    entry = PTZ_ACTIONS.get(action)
    if entry is None:
        return False, f"unknown action '{action}'"
    method, args = entry
    if action in PTZ_SPEED_ACTIONS and speed is not None:
        args = (max(1, min(63, int(speed))),)
    ok = ptz.send(method, *args)
    if ok and action == "thermal_camera":
        imager_state = "thermal"
    elif ok and action == "visible_light_camera":
        imager_state = "visible"
    return ok, ""


# Diagonal (combined pan+tilt) frames are spec-confirmed but have never been
# sent to this camera. Until one bench test passes, /move requests with both
# axes active are served by the dominant axis alone.
DIAGONALS_ENABLED = _env("DIAGONALS_ENABLED", "0").lower() in ("1", "true", "yes")


def execute_move(pan: int, tilt: int, pan_speed: int, tilt_speed: int) -> tuple[bool, str]:
    """Combined-axis motion for the D-pad. pan/tilt in -1/0/1."""
    if ptz is None:
        return False, "no serial link"
    pan = max(-1, min(1, int(pan)))
    tilt = max(-1, min(1, int(tilt)))
    pan_speed = max(1, min(63, int(pan_speed)))
    tilt_speed = max(1, min(63, int(tilt_speed)))
    if pan and tilt and not DIAGONALS_ENABLED:
        # Dominant axis: larger speed wins, tie goes to pan.
        if tilt_speed > pan_speed:
            pan = 0
        else:
            tilt = 0
    return ptz.send("move", pan, tilt, pan_speed, tilt_speed), ""


# ---------------------------------------------------------------------------
# Web application
# ---------------------------------------------------------------------------
app = Flask(__name__)
# Rebound by build_cameras() to the primary camera's objects. Declared here
# so the routes below can close over the names before the registry exists.
frames: FrameBuffer | None = None
recorder: "Recorder | None" = None
archive: "ArchiveIndex | None" = None
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

# Static assets get a long cache with a version stamp from their mtime, so a
# changed stylesheet reaches a phone immediately instead of after the browser
# decides to revalidate. The pages themselves are never cached: the PTZ
# JavaScript is inline, so a stale page means stale controls.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = timedelta(days=30)


@app.url_defaults
def _stamp_static(endpoint, values):
    if endpoint == "static" and "filename" in values:
        try:
            stamp = (Path(app.static_folder) / values["filename"]).stat().st_mtime
        except OSError:
            return
        values["v"] = int(stamp)


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

    Deliberately no HSTS: with a certificate problem, a pinned HSTS policy
    removes the browser's "proceed anyway" option and locks you out of your
    own camera.

    No 'unsafe-inline' anywhere: every template loads styles from lcars.css
    and behaviour from static/js/main.js, with zero inline handlers or style
    attributes. Scripts and styles that are not ours cannot run even if
    markup injection is ever found.
    """
    if not request.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self'; script-src 'self'; "
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
    scripted = request.path.startswith(("/timeline", "/play", "/clip"))
    if (request.method == "POST"
            or request.path in ("/camera", "/health", "/snapshot")
            or scripted):
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
    return render_template(
        "index.html",
        cameras=[{"cid": c.cid, "name": c.cfg.name, "aspect": c.cfg.aspect,
                  "ptz": c.cfg.has_ptz} for c in CAMERAS.values()],
        cam=primary().cid,
        diagonals=DIAGONALS_ENABLED,
        default_speed=PTZ_SPEED,
        auth_enabled=AUTH_ENABLED,
    )


@app.route("/camera")
def camera():
    cam = _camera_arg()
    if cam is None:
        return _no_such_camera(request.args.get("cam", ""))
    buf = cam.frames
    if cam.preview is not None:
        cam.preview.want()          # start it if nobody else is watching

    def stream():
        last = buf.seq - 1
        idle = 0
        while not shutdown.is_set():
            # Re-register on every frame, so the feed stays up exactly as long
            # as somebody is actually reading it.
            if cam.preview is not None:
                cam.preview.want()
            got = buf.wait_for(last, timeout=2.0)
            if got is None:
                idle += 1
                # A generator that never yields can never notice the client has
                # gone, so a camera producing no frames pinned one cheroot
                # worker per viewer until shutdown - and with the pool
                # exhausted, /health went down too, hiding the cause. Ending
                # the response is the only way to release the worker.
                if idle >= STREAM_IDLE_GIVEUP:
                    log.info("Preview idle %.0fs - closing the stream",
                             idle * 2.0)
                    return
                continue
            idle = 0
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
            cam = _camera_arg()
            why = ptz_camera_or_error(cam)
            if why:
                return make_response(
                    jsonify({"ok": False, "action": action, "error": why}), 400)
            payload = request.get_json(silent=True) or {}
            speed = payload.get("speed") if isinstance(payload, dict) else None
            try:
                speed = int(speed) if speed is not None else None
            except (TypeError, ValueError):
                speed = None
            ok, err = execute_ptz(action, speed=speed)
            return make_response(
                jsonify({"ok": ok, "action": action, "error": err}), 200
            )
        view.__name__ = f"ptz_{action}"
        app.route(rule, methods=["POST"])(view)


_register_ptz_routes()


@app.route("/move", methods=["POST"])
def move():
    """Combined-axis motion for the D-pad.

    {"pan": -1|0|1, "tilt": -1|0|1, "panSpeed": 1-63, "tiltSpeed": 1-63}
    Both axes zero is a stop.
    """
    p = request.get_json(silent=True) or {}
    try:
        pan = int(p.get("pan", 0))
        tilt = int(p.get("tilt", 0))
        pan_speed = int(p.get("panSpeed", PTZ_SPEED))
        tilt_speed = int(p.get("tiltSpeed", PTZ_SPEED))
    except (TypeError, ValueError):
        return make_response(jsonify({"ok": False, "error": "bad payload"}), 400)
    ok, err = execute_move(pan, tilt, pan_speed, tilt_speed)
    return make_response(jsonify({"ok": ok, "error": err}), 200)


@app.route("/Set_preset", methods=["POST"])
def set_preset():
    return _preset("set_preset")


@app.route("/Goto_preset", methods=["POST"])
def goto_preset():
    return _preset("goto_preset")


@app.route("/Clear_preset", methods=["POST"])
def clear_preset():
    return _preset("clear_preset")


def _preset(action: str):
    payload = request.get_json(silent=True) or {}
    # "preset" is the current name; "flabber" is accepted for the old UI.
    raw = payload.get("preset", payload.get("flabber"))
    try:
        number = int(raw)
    except (TypeError, ValueError):
        return make_response(jsonify({"ok": False, "error": "bad preset"}), 400)
    ok, err = execute_ptz(action, number)
    status = 200 if ok or not err else 400
    return make_response(
        jsonify({"ok": ok, "preset": number, "error": err}), status
    )


@app.route("/snapshot")
def snapshot():
    """The latest preview frame as a downloadable JPEG.

    Preview resolution (640 wide) with the timestamp already burned in -
    drawtext runs before ffmpeg's split, so every path carries it.
    """
    cam = _camera_arg()
    if cam is None:
        return _no_such_camera(request.args.get("cam", ""))
    if cam.preview is not None:
        cam.preview.want()
        # A cold substream needs a moment to produce its first frame.
        for _ in range(30):
            if cam.frames.latest() is not None:
                break
            time.sleep(0.2)
    jpeg = cam.frames.latest()
    if jpeg is None:
        return make_response(jsonify({"ok": False, "error": "no frame yet"}), 503)
    resp = make_response(jpeg)
    resp.headers["Content-Type"] = "image/jpeg"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="{cam.cid}-{stamp}.jpg"')
    return resp


def _recording_entries(cam) -> list[dict]:
    entries = []
    for path in sorted(cam.video_dir.glob(f"*.{RECORD_EXT}"), reverse=True):
        day = day_of(path)
        if day is None:
            continue
        size = path.stat().st_size
        entries.append({
            "name": path.name,
            "day": day.isoformat(),
            "size_gb": round(size / 2**30, 2),
            "growing": day == date.today() and not path.name.count("part"),
        })
    return entries


@app.route("/recordings")
def recordings():
    cam = _camera_arg()
    if cam is None:
        return _no_such_camera(request.args.get("cam", ""))
    entries = _recording_entries(cam)
    return render_template(
        "recordings.html",
        entries=entries,
        cam=cam.cid,
        cameras=[{"cid": c.cid, "name": c.cfg.name} for c in CAMERAS.values()],
        total_gb=round(sum(e["size_gb"] for e in entries), 1),
        free_gb=round(shutil.disk_usage(VIDEO_ROOT).free / 2**30, 1),
        retention_days=cam.cfg.retention_days,
    )


@app.route("/recordings/<name>")
def recording_file(name: str):
    cam = _camera_arg()
    if cam is None:
        return _no_such_camera(request.args.get("cam", ""))
    # Only names the glob actually produced - user input never touches a path.
    # Scoped to one camera, so a name that exists under a different camera is
    # not silently served from the wrong archive.
    if not any(e["name"] == name for e in _recording_entries(cam)):
        return make_response(jsonify({"ok": False, "error": "no such recording"}), 404)
    return send_from_directory(cam.video_dir, name, as_attachment=True,
                               conditional=True)


# ---------------------------------------------------------------------------
# Archive playback and clipping
#
# Both paths are remux-only: ffmpeg copies the H.264 bitstream and rewrites
# only the container, so nothing is ever re-encoded and the picture is bit
# identical to what was recorded. Playback streams a fragmented MP4 straight
# to a plain <video> element, which means no media-source library to vendor
# and no MSE dependency - it plays anywhere, including browsers that have no
# Media Source Extensions at all.
# ---------------------------------------------------------------------------
_media_slots = threading.Semaphore(MEDIA_JOBS)


class MediaBusy(Exception):
    """Every archive worker slot is taken."""


def _take_slot():
    if not _media_slots.acquire(timeout=MEDIA_WAIT):
        raise MediaBusy()


def _camera_arg(param: str = "cam"):
    """The camera a request is about, or None if it named one that is unknown.

    Absent means the primary camera, so every URL that predates multi-camera
    support keeps working exactly as it did.
    """
    cid = (request.args.get(param) or "").strip()
    if not cid:
        return primary()
    return CAMERAS.get(cid)


def _no_such_camera(cid: str):
    return make_response(jsonify({
        "ok": False, "error": f"no camera '{cid}'",
        "cameras": list(CAMERAS)}), 404)


def _parse_day(raw: str):
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _float_arg(name: str, default: float) -> float:
    try:
        return float(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


def _run_media(args: list, timeout: int = 600):
    """Run a niced ffmpeg to completion. Returns (ok, stderr)."""
    try:
        done = subprocess.run(
            NICE + args, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=timeout,
        )
        return done.returncode == 0, done.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return False, "ffmpeg timed out"
    except Exception as exc:
        return False, str(exc)


@app.route("/timeline")
def timeline():
    """What footage exists for a day, and where the holes are.

    Gaps are real: every service restart ends one file and starts another,
    and whatever happened in between was not recorded. The UI draws them
    rather than papering over them.
    """
    cam = _camera_arg()
    if cam is None:
        return _no_such_camera(request.args.get("cam", ""))
    day = _parse_day(request.args.get("day", ""))
    if day is None:
        return make_response(jsonify({"ok": False, "error": "bad day"}), 400)

    midnight = ArchiveIndex.midnight(day)
    segs = cam.index.segments(day)
    out, gaps, previous = [], [], None
    for seg in segs:
        begin = seg["start"] - midnight
        finish = seg["end"] - midnight
        if previous is not None and begin - previous > 1.0:
            gaps.append({"from": round(previous, 1), "to": round(begin, 1)})
        previous = finish
        out.append({
            "name": seg["name"],
            "from": round(begin, 1),
            "to": round(finish, 1),
            "size_gb": round(seg["size"] / 2**30, 2),
        })
    return jsonify({
        "ok": True,
        "cam": cam.cid,
        "cameras": [{"cid": c.cid, "name": c.cfg.name,
                     "aspect": c.cfg.aspect, "ptz": c.cfg.has_ptz}
                    for c in CAMERAS.values()],
        "day": day.isoformat(),
        "days": cam.index.days(),
        "segments": out,
        "gaps": gaps,
        "covered_seconds": round(sum(s["duration"] for s in segs)),
        "clip_max": CLIP_MAX_SECONDS,
        "window": cam.cfg.play_window_seconds,
    })


@app.route("/watch")
@app.route("/watch/<day>")
def watch(day: str = ""):
    cam = _camera_arg()
    if cam is None:
        return _no_such_camera(request.args.get("cam", ""))
    parsed = _parse_day(day) if day else None
    if parsed is None:
        days = cam.index.days()
        parsed = _parse_day(days[0]) if days else date.today()
    return render_template(
        "watch.html", day=parsed.isoformat(), cam=cam.cid,
        cameras=[{"cid": c.cid, "name": c.cfg.name} for c in CAMERAS.values()])


WINDOW_DIR = CLIP_DIR / "windows"


def cam_window_dir(cam) -> Path:
    """Where one camera's cached playback windows live.

    A subdirectory per camera, because the key is built from the segment
    filename and two cameras both produce "2026-08-16.ts" - a shared
    directory would serve one camera's video for the other's request, with a
    200 and an X-Segment header that agreed with itself.
    """
    return WINDOW_DIR / cam.cid


# Last time each cached window was actually served, by path. The
# filesystem cannot answer this: the root filesystem is mounted noatime, so
# st_atime is never updated by a read and sorting on it evicts in creation
# order - FIFO wearing an LRU label. Reads are recorded here instead.
_window_hits: dict[str, float] = {}
_window_hits_lock = threading.Lock()


def _note_window_use(key: str) -> None:
    with _window_hits_lock:
        _window_hits[key] = time.time()


def _window_key(path: Path) -> str:
    """Cache identity of a window file: camera directory plus filename."""
    return f"{path.parent.name}/{path.name}"


def _sweep_windows() -> None:
    """Hold the window cache under budget, dropping least-recently-used.

    Recursive, and across every camera: the budget is the disk, which none of
    them owns individually.
    """
    try:
        entries = list(WINDOW_DIR.glob("**/*.mp4"))
    except OSError:
        return
    files = []
    with _window_hits_lock:
        for f in entries:
            try:
                st = f.stat()
            except OSError:
                continue
            # Anything not served since this process started falls back to
            # its build time, which is the best evidence available.
            files.append((_window_hits.get(_window_key(f), st.st_mtime),
                          st.st_size, f))
        live = {_window_key(f) for f in entries}
        for gone in [k for k in _window_hits if k not in live]:
            del _window_hits[gone]
    total = sum(size for _, size, _ in files)
    budget = WINDOW_CACHE_MB * 2**20
    for _, size, path in sorted(files):
        if total <= budget:
            break
        try:
            path.unlink()
            total -= size
        except OSError:
            pass


def _build_window(cam, seg: dict, into: float, length: float, key: str):
    """Cut one playback window, reusing the cached copy when there is one."""
    window_dir = cam_window_dir(cam)
    try:
        window_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"cache directory {window_dir} is not writable: {exc}"
    final = window_dir / key
    if final.exists() and final.stat().st_size > 0:
        return final, None

    # Built under a unique name and renamed into place, so two viewers asking
    # for the same window cannot serve each other a half-written file.
    scratch = window_dir / f".{key}.{os.getpid()}.{threading.get_ident()}.part"
    ok, err = _run_media([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", f"{into:.3f}", "-t", f"{length:.3f}",
        "-i", str(cam.video_dir / seg["name"]),
        "-c", "copy", "-an", "-movflags", "+faststart",
        "-f", "mp4", "-y", str(scratch),
    ], timeout=180)
    if not ok or not scratch.exists() or scratch.stat().st_size == 0:
        scratch.unlink(missing_ok=True)
        return None, err or "ffmpeg produced nothing"
    scratch.replace(final)
    _sweep_windows()
    return final, None


@app.route("/play")
def play():
    """Serve a bounded window of archive footage as a seekable MP4.

    A fragmented stream was tried first and is worse: with no moov duration
    the browser reports a 299s window as 6s, its scrubber does not work, and
    nothing can seek inside what it already has. A complete MP4 costs about a
    second to cut and is then fully seekable, cached, and range-serveable.

    Playback that runs past the end of a segment stops there; the page picks
    up the next one.
    """
    cam = _camera_arg()
    if cam is None:
        return _no_such_camera(request.args.get("cam", ""))
    day = _parse_day(request.args.get("day", ""))
    if day is None:
        return make_response(jsonify({"ok": False, "error": "bad day"}), 400)

    offset = max(0.0, _float_arg("t", 0.0))
    hit = cam.index.locate(day, offset)
    if hit is None:
        nxt = cam.index.next_after(day, offset)
        return make_response(jsonify({
            "ok": False, "error": "no recording at that time",
            "next": round(nxt, 1) if nxt is not None else None,
        }), 404)

    seg, into = hit
    cam_window = cam.cfg.play_window_seconds
    window = min(_float_arg("d", cam_window), cam_window)
    window = min(window, max(1.0, seg["duration"] - into))

    # Windows are quantised so that scrubbing around one moment keeps asking
    # for the same file instead of cutting a new one at every position.
    grid = max(1.0, window)
    aligned = int(into // grid) * grid
    length = min(grid, max(1.0, seg["duration"] - aligned))
    key = f"{seg['name']}.{int(aligned)}.{int(length)}.mp4"

    window_dir = cam_window_dir(cam)
    cached = window_dir / key
    if not (cached.exists() and cached.stat().st_size > 0):
        try:
            _take_slot()
        except MediaBusy:
            return make_response(jsonify({"ok": False, "error": "busy"}), 503)
        try:
            built, err = _build_window(cam, seg, aligned, length, key)
        finally:
            _media_slots.release()
        if built is None:
            log.error("Window build failed for %s/%s: %s", cam.cid, key, err)
            return make_response(
                jsonify({"ok": False, "error": err or "cut failed"}), 500)

    _note_window_use(f"{cam.cid}/{key}")
    resp = send_from_directory(window_dir, key, mimetype="video/mp4",
                               conditional=True)
    resp.headers["X-Camera"] = cam.cid
    resp.headers["X-Segment"] = seg["name"]
    resp.headers["X-Window-Start"] = f"{(seg['start'] - ArchiveIndex.midnight(day)) + aligned:.1f}"
    resp.headers["X-Window"] = f"{length:.1f}"
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


@app.route("/clip")
def clip():
    """Cut [from, to] out of the archive and hand back a real MP4 file.

    Ranges that straddle a restart are stitched: each file is cut with its
    own fast seek and the pieces are concatenated, because seeking through a
    concatenation instead takes twelve times as long. Gaps inside the range
    cannot be filled - the burned-in clock in the picture jumps, and the
    header reports how much time is missing.
    """
    cam = _camera_arg()
    if cam is None:
        return _no_such_camera(request.args.get("cam", ""))
    day = _parse_day(request.args.get("day", ""))
    if day is None:
        return make_response(jsonify({"ok": False, "error": "bad day"}), 400)

    start = max(0.0, _float_arg("from", 0.0))
    end = _float_arg("to", 0.0)
    span = end - start
    if span <= 0:
        return make_response(jsonify({"ok": False, "error": "empty range"}), 400)
    if span > CLIP_MAX_SECONDS:
        return make_response(jsonify({
            "ok": False,
            "error": f"clip longer than {CLIP_MAX_SECONDS // 60} minutes",
        }), 400)

    cuts = cam.index.overlapping(day, start, end)
    if not cuts:
        return make_response(jsonify({
            "ok": False, "error": "no recording in that range"}), 404)

    covered = sum(c["take"] for c in cuts)
    try:
        _take_slot()
    except MediaBusy:
        return make_response(jsonify({"ok": False, "error": "busy"}), 503)

    CLIP_DIR.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=CLIP_DIR, prefix="clip-"))
    out = work / "clip.mp4"
    try:
        pieces = []
        for i, cut in enumerate(cuts):
            piece = work / f"{i:03d}.ts"
            ok, err = _run_media([
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-ss", f"{cut['seek']:.3f}", "-t", f"{cut['take']:.3f}",
                "-i", str(cam.video_dir / cut["name"]),
                "-c", "copy", "-f", "mpegts", "-y", str(piece),
            ])
            if not ok or not piece.exists() or piece.stat().st_size == 0:
                log.error("Clip piece failed for %s: %s", cut["name"], err)
                continue
            pieces.append(piece)

        if not pieces:
            raise RuntimeError("no usable footage in that range")

        if len(pieces) == 1:
            ok, err = _run_media([
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-i", str(pieces[0]), "-c", "copy", "-an",
                "-movflags", "+faststart", "-y", str(out),
            ])
        else:
            listing = work / "list.txt"
            listing.write_text("".join(f"file '{p}'\n" for p in pieces))
            # No -ss here: the pieces already start where they should, so
            # the concat demuxer never has to seek.
            ok, err = _run_media([
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(listing),
                "-c", "copy", "-an", "-movflags", "+faststart", "-y", str(out),
            ])
        if not ok or not out.exists() or out.stat().st_size == 0:
            raise RuntimeError(err or "ffmpeg produced nothing")

        size = out.stat().st_size
        payload = out.read_bytes() if size <= 8 * 2**20 else None
    except Exception as exc:
        shutil.rmtree(work, ignore_errors=True)
        _media_slots.release()
        log.error("Clip failed: %s", exc)
        return make_response(jsonify({"ok": False, "error": str(exc)}), 500)

    # The ffmpeg work is done. The slot exists to bound concurrent ffmpeg, not
    # concurrent downloads: holding it until the last byte reaches a phone on
    # a slow link starved every other request, and a client that vanished
    # mid-download never released it at all.
    _media_slots.release()

    def deliver():
        try:
            if payload is not None:
                yield payload
                return
            with open(out, "rb") as fh:
                while True:
                    chunk = fh.read(262144)
                    if not chunk:
                        break
                    yield chunk
        finally:
            shutil.rmtree(work, ignore_errors=True)

    stamp = (datetime.fromtimestamp(ArchiveIndex.midnight(day) + start)
             .strftime("%Y%m%d-%H%M%S"))
    resp = Response(deliver(), mimetype="video/mp4")
    resp.headers["Content-Length"] = str(size)
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="{cam.cid}-{stamp}-{int(span)}s.mp4"')
    resp.headers["X-Camera"] = cam.cid
    resp.headers["X-Covered-Seconds"] = f"{covered:.1f}"
    resp.headers["X-Missing-Seconds"] = f"{max(0.0, span - covered):.1f}"
    return resp


def check_cache_writable() -> bool:
    """Confirm at startup that archive playback will actually be able to run.

    The service is sandboxed with ProtectSystem=strict, so a cache directory
    that is not in ReadWritePaths reads as a read-only filesystem however the
    permissions look. Better to say so on boot than on the first click.
    """
    try:
        CLIP_DIR.mkdir(parents=True, exist_ok=True)
        probe = CLIP_DIR / ".writable"
        probe.write_bytes(b"")
        probe.unlink()
        log.info("Media cache at %s", CLIP_DIR)
        return True
    except OSError as exc:
        log.error("Media cache %s is not writable (%s) - archive playback and "
                  "clipping will fail. Under systemd, add CacheDirectory= or "
                  "list the path in ReadWritePaths.", CLIP_DIR, exc)
        return False


def sweep_clips(max_age_seconds: float = 0.0) -> None:
    """Clear abandoned scratch and re-budget the window cache.

    Called at startup with no age limit (nothing can be in flight yet) and
    again from the retention loop with one, because a clip whose client
    vanished leaves its directory behind and CLIP_DIR shares a filesystem
    with the recordings - so orphaned scratch is paid for by deleting
    footage.
    """
    cutoff = time.time() - max_age_seconds if max_age_seconds else None
    try:
        for stale in CLIP_DIR.glob("clip-*"):
            if cutoff is not None:
                try:
                    if stale.stat().st_mtime > cutoff:
                        continue        # possibly still being written
                except OSError:
                    continue
            shutil.rmtree(stale, ignore_errors=True)
            log.info("Swept abandoned clip scratch %s", stale.name)
        # Recursive: windows move into per-camera subdirectories next, and a
        # non-recursive glob would silently stop matching then.
        for partial in WINDOW_DIR.glob("**/.*"):
            if cutoff is not None:
                try:
                    if partial.stat().st_mtime > cutoff:
                        continue
                except OSError:
                    continue
            partial.unlink(missing_ok=True)
        _sweep_windows()
    except Exception:
        log.debug("Scratch sweep failed", exc_info=True)


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


def camera_state(cam) -> dict:
    """Health of one camera."""
    current = cam.video_dir / f"{date.today():%Y-%m-%d}.{RECORD_EXT}"
    exists = current.exists()
    rec = cam.recorder
    return {
        "cam": cam.cid,
        "name": cam.cfg.name,
        "kind": cam.cfg.kind,
        "aspect": cam.cfg.aspect,
        "ptz": cam.cfg.has_ptz,
        "recording": rec.alive,
        "ffmpeg_restarts": rec.restarts,
        "ffmpeg_started": rec.started_at.isoformat() if rec.started_at else None,
        "preview_frames": cam.frames.seq,
        "preview_mode": cam.cfg.preview,
        "preview_running": (cam.preview.running if cam.preview is not None
                            else rec.alive),
        "current_file": current.name if exists else None,
        "current_size_gb": round(current.stat().st_size / 2**30, 2) if exists else 0,
        "retention_days": cam.cfg.retention_days,
        "archive_gb": round(sum(
            f.stat().st_size for f in cam.video_dir.glob(f"*.{RECORD_EXT}")
        ) / 2**30, 1),
    }


def current_state() -> dict:
    """One snapshot of service health, shared by /health and MQTT.

    The top level keeps the single-camera keys the existing page and MQTT
    bridge already read, reporting the primary camera, and adds a "cameras"
    map beside them. Renaming them instead would have broken the UI and every
    MQTT consumer for no gain.
    """
    per_cam = {cam.cid: camera_state(cam) for cam in CAMERAS.values()}
    head = dict(per_cam.get(primary().cid, {}))
    head.update({
        "cameras": per_cam,
        "primary": primary().cid,
        "serial": ptz is not None and ptz.connected,
        "free_gb": round(shutil.disk_usage(VIDEO_ROOT).free / 2**30, 1),
        # Last COMMANDED imager - RS-485 has no readback, so this is an
        # assumption the UI must present as such, never as ground truth.
        "imager": imager_state,
    })
    return head


@app.route("/health")
def health():
    """Whole-service health, or one camera's with ?cam=<id>."""
    cid = (request.args.get("cam") or "").strip()
    if cid:
        cam = CAMERAS.get(cid)
        if cam is None:
            return _no_such_camera(cid)
        return jsonify(camera_state(cam))
    return jsonify(current_state())


# ---------------------------------------------------------------------------
# MQTT bridge
# ---------------------------------------------------------------------------
class MqttBridge:
    """Publishes camera state and accepts PTZ commands over MQTT.

    Strictly optional. If the broker is unreachable the camera carries on
    exactly as before; paho reconnects in the background on its own.

    Topics, all under MQTT_PREFIX:
        <prefix>/status              online | offline (retained, LWT)
        <prefix>/state               JSON health, whole service (retained)
        <prefix>/<cid>/state         JSON health, one camera (retained)
        <prefix>/<cid>/ptz/set       subscribed, per camera
        <prefix>/<cid>/ptz/result    outcome of the last command
        <prefix>/ptz/set             subscribed, means the primary camera
        <prefix>/ptz/result          outcome, primary camera

    The unqualified ptz topics are kept because anything already publishing
    to them predates there being a second camera, and silently retargeting
    an existing automation at a different device would be the worst possible
    way to introduce one. They address the primary camera, explicitly.
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
        for cid in CAMERAS:
            client.subscribe(f"{MQTT_PREFIX}/{cid}/ptz/set", qos=1)
        self._publish_state()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        if not self._stop.is_set():
            log.warning("MQTT disconnected (%s) - retrying", reason_code)

    def _camera_for(self, topic: str):
        """Which camera a command topic addresses.

        <prefix>/<cid>/ptz/set names one; <prefix>/ptz/set means the primary.
        """
        parts = topic.split("/")
        if len(parts) >= 4 and parts[1] in CAMERAS:
            return CAMERAS[parts[1]], parts[1]
        return primary(), primary().cid

    def _on_message(self, client, userdata, msg):
        try:
            cam, cid = self._camera_for(msg.topic)
            result_topic = (f"{MQTT_PREFIX}/{cid}/ptz/result"
                            if msg.topic.startswith(f"{MQTT_PREFIX}/{cid}/")
                            else f"{MQTT_PREFIX}/ptz/result")
            why = ptz_camera_or_error(cam)
            if why:
                log.warning("MQTT PTZ for '%s' refused: %s", cid, why)
                client.publish(result_topic,
                               json.dumps({"ok": False, "error": why,
                                           "cam": cid}), qos=0)
                return
            raw = msg.payload.decode("utf-8", "replace").strip()
            speed = None
            if raw.startswith("{"):
                payload = json.loads(raw)
                action = str(payload.get("action", ""))
                preset = payload.get("preset")
                preset = int(preset) if preset is not None else None
                speed = payload.get("speed")
                speed = int(speed) if speed is not None else None
            else:
                action, preset = raw, None      # bare action string also works
            if action == "move" and raw.startswith("{"):
                ok, err = execute_move(
                    int(payload.get("pan", 0)), int(payload.get("tilt", 0)),
                    int(payload.get("panSpeed", PTZ_SPEED)),
                    int(payload.get("tiltSpeed", PTZ_SPEED)),
                )
            else:
                ok, err = execute_ptz(action, preset, speed)
            if not ok:
                log.warning("MQTT PTZ '%s' on '%s' rejected: %s",
                            action, cid, err or "serial write failed")
            client.publish(
                result_topic,
                json.dumps({"action": action, "ok": ok, "error": err,
                            "cam": cid}), qos=0
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
            # One topic per camera as well, so a consumer can subscribe to the
            # one it cares about rather than parsing the whole service out of
            # a combined document.
            for cam in CAMERAS.values():
                self._client.publish(f"{MQTT_PREFIX}/{cam.cid}/state",
                                     json.dumps(camera_state(cam)),
                                     qos=0, retain=True)
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
    # Do not wait indefinitely for a worker that is blocked writing to a
    # client which has stopped reading - an MJPEG viewer on a stalled link
    # will do exactly that.
    server.shutdown_timeout = 5
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


def _shutdown_watchdog(seconds: float = 25.0) -> threading.Timer:
    """Guarantee the process exits rather than being SIGKILLed by systemd.

    cheroot's thread-pool stop takes a timeout, but after it expires it
    forces SHUT_RD on the worker's socket and then joins it *unbounded* - so
    a worker that is blocked on anything other than a socket read hangs the
    stop forever. Measured here: 90s, ended by systemd's SIGKILL, with one
    browser watching the live preview.

    Exiting on our own terms is strictly better than being killed: the
    recorders have already been asked to stop by then, and MPEG-TS needs no
    trailer, so nothing is corrupted either way. The thread dump is the point
    - it names the stuck frame instead of leaving another silent 90 seconds.
    """
    def bite() -> None:
        log.error("Shutdown still running after %.0fs - dumping threads and "
                  "exiting", seconds)
        try:
            faulthandler.dump_traceback()
        except Exception:
            pass
        os._exit(1)

    timer = threading.Timer(seconds, bite)
    timer.daemon = True
    timer.start()
    return timer


def graceful_stop(stop_http: bool = True) -> None:
    """Stop everything, narrating each phase.

    Narrating matters: a stop that overran its systemd timeout logged
    "Shutting down" and then nothing at all, so the only way to tell which
    call had hung was to add these lines and make it happen again. Each phase
    is cheap to log and turns a silent 90-second SIGKILL into one line.
    """
    if shutdown.is_set():
        return
    shutdown.set()
    log.info("Shutting down")
    watchdog = _shutdown_watchdog()

    # Before the web server: a preview generator parked waiting for a frame
    # holds a worker, and cheroot's stop() waits for its workers.
    for cam in CAMERAS.values():
        cam.frames.wake()

    # Recorders first. They own the only state that a hard exit could damage,
    # and the web server holding a socket open for another few seconds costs
    # nothing. Doing this the other way round meant a stuck HTTP worker could
    # stop the recordings from ever being closed properly.
    for cam in CAMERAS.values():
        if cam.preview is not None:
            cam.preview.stop()
        log.info("Stopping recorder %s", cam.cid)
        try:
            cam.recorder.stop()
        except Exception:
            log.warning("Recorder %s stop raised", cam.cid, exc_info=True)

    log.info("Stopping the MQTT bridge")
    try:
        mqtt_bridge.stop_bridge()
    except Exception:
        log.warning("MQTT bridge stop raised", exc_info=True)

    # Only from a thread that is NOT the one running the server loop.
    #
    # cheroot's ConnectionManager.stop() is "self._stop_requested = True;
    # while self._serving: sleep(0.01)" - it spins until the selector loop in
    # run() notices. run() executes on the main thread, and a signal handler
    # also runs on the main thread, so calling this from the handler makes the
    # main thread wait for a loop it is itself blocking. That deadlocked for
    # 90s until systemd SIGKILLed the service, on every restart that had a
    # browser watching. All 32 workers were idle throughout, which is what
    # ruled out a stuck request.
    #
    # Skipping it costs nothing: os._exit() follows immediately and the kernel
    # closes the listening socket and every connection. An HTTP client sees a
    # reset, which is what a process exit looks like anyway. The recorders -
    # the only things owning state worth protecting - were already stopped
    # above.
    if http_server is not None and stop_http:
        log.info("Stopping the web server")
        try:
            http_server.stop()
        except Exception:
            log.warning("Web server stop raised", exc_info=True)
    elif http_server is not None:
        log.info("Leaving the web server to the exit (called from the "
                 "serving thread)")

    if ptz:
        ptz.close()
    watchdog.cancel()
    log.info("Stopped cleanly")


def handle_signal(sig, _frame) -> None:
    # Runs on the main thread, which is the thread inside the server loop -
    # hence stop_http=False. See graceful_stop() for why that matters.
    log.info("Signal %s received", sig)
    graceful_stop(stop_http=False)
    os._exit(0)


def main() -> None:
    global ptz

    setup_logging()
    VIDEO_ROOT.mkdir(parents=True, exist_ok=True)
    build_cameras()
    check_cache_writable()
    sweep_clips()

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

    for cam in CAMERAS.values():
        threading.Thread(target=cam.recorder.run,
                         name=f"recorder-{cam.cid}", daemon=True).start()
    threading.Thread(target=retention_loop, args=(shutdown,),
                     name="retention", daemon=True).start()
    mqtt_bridge.start()

    serve()


if __name__ == "__main__":
    main()
