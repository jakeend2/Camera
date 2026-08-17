#!/usr/bin/env python3
"""Verify the multi-camera HTTP surface.

Runs against the module in-process. Complements deploy/verify-archive.py
(which exercises one camera end to end) by checking the things that only
matter once there is more than one: that omitting ?cam still means the
primary camera, that naming an unknown one is refused rather than silently
defaulted, that each camera's playback windows are cached somewhere the
others cannot reach, and that PTZ is refused for a camera with no motors.
"""
import os
import pathlib
import sys

# Load the same EnvironmentFile systemd gives the service, BEFORE importing
# the module - its configuration is read at import time. Parsed rather than
# sourced: the camera password contains shell metacharacters, and sourcing it
# in bash is what printed it to a terminal once already.
_env_file = pathlib.Path("/etc/camera-service.env")
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _v = _v.strip()
            # systemd strips surrounding quotes; parsing by hand must too,
            # or a quoted password is passed with its quotes attached.
            if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in "\"'":
                _v = _v[1:-1]
            os.environ.setdefault(_k.strip(), _v)

sys.path.insert(0, "/opt/camera")
import camera_service as cs

cs.AUTH_ENABLED = False
cs.build_cameras()
c = cs.app.test_client()
day = cs.date.today().isoformat()
fails = []


def chk(desc, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + desc + (("  " + extra) if extra else ""))
    if not cond:
        fails.append(desc)


print(f"  cameras registered: {list(cs.CAMERAS)}   primary: {cs.primary().cid}\n")

# --- the default is the primary camera, so old URLs still mean what they did
a = c.get(f"/timeline?day={day}").get_json()
b = c.get(f"/timeline?day={day}&cam={cs.primary().cid}").get_json()
chk("omitting ?cam == primary", a["segments"] == b["segments"])
chk("/timeline advertises the camera list",
    {x["cid"] for x in a["cameras"]} == set(cs.CAMERAS))
chk("/timeline reports the camera's own window size",
    a["window"] == cs.primary().cfg.play_window_seconds)

# --- an unknown camera must never fall back to a real one -----------------
for path in (f"/timeline?day={day}&cam=ghost", f"/play?day={day}&t=100&cam=ghost",
             f"/clip?day={day}&from=0&to=5&cam=ghost", "/health?cam=ghost",
             "/recordings?cam=ghost", "/snapshot?cam=ghost", "/camera?cam=ghost"):
    r = c.get(path)
    chk(f"unknown cam refused: {path.split('?')[0]}",
        r.status_code == 404 and "no camera" in r.get_data(as_text=True),
        f"HTTP {r.status_code}")

# --- per-camera storage, index and window cache ---------------------------
seen_dirs, seen_index = set(), set()
for cam in cs.CAMERAS.values():
    seen_dirs.add(str(cam.video_dir))
    seen_index.add(str(cam.index.index_file))
chk("every camera has its own recording directory", len(seen_dirs) == len(cs.CAMERAS))
chk("every camera has its own probe cache", len(seen_index) == len(cs.CAMERAS))

for cam in cs.CAMERAS.values():
    segs = cam.index.segments(cs.date.today())
    if not segs:
        print(f"  SKIP {cam.cid}: nothing recorded today")
        continue
    longest = max(segs, key=lambda s: s["duration"])
    at = longest["start"] - cs.ArchiveIndex.midnight(cs.date.today()) + 60
    r = c.get(f"/play?day={day}&t={at:.0f}&cam={cam.cid}")
    chk(f"/play[{cam.cid}] 200 and names its camera",
        r.status_code == 200 and r.headers.get("X-Camera") == cam.cid,
        f"HTTP {r.status_code}")
    wd = cs.cam_window_dir(cam)
    chk(f"/play[{cam.cid}] caches under its own directory",
        wd.exists() and any(wd.glob("*.mp4")), str(wd))

# --- health ---------------------------------------------------------------
h = c.get("/health").get_json()
chk("/health keeps the pre-multicam top-level keys",
    all(k in h for k in ("recording", "preview_frames", "free_gb", "imager")))
chk("/health carries every camera", set(h.get("cameras", {})) == set(cs.CAMERAS))
one = c.get(f"/health?cam={cs.primary().cid}").get_json()
chk("/health?cam= returns just that camera",
    one["cam"] == cs.primary().cid and "cameras" not in one)

# --- PTZ is a capability, not an assumption -------------------------------
for cam in cs.CAMERAS.values():
    why = cs.ptz_camera_or_error(cam)
    if cam.cfg.has_ptz:
        chk(f"ptz allowed for {cam.cid}", why == "", why)
    else:
        chk(f"ptz refused for {cam.cid} with a reason", "no pan/tilt/zoom" in why, why)
chk("ptz refused for an unknown camera",
    cs.ptz_camera_or_error(None) == "no such camera")

# --- recordings are scoped ------------------------------------------------
for cam in cs.CAMERAS.values():
    names = [e["name"] for e in cs._recording_entries(cam)]
    if not names:
        continue
    chk(f"/recordings/<name> serves {cam.cid}'s own file",
        c.get(f"/recordings/{names[0]}?cam={cam.cid}").status_code == 200)
chk("/recordings/<name> refuses a name that does not exist",
    c.get("/recordings/definitely-not-here.ts").status_code == 404)

print()
print("PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
