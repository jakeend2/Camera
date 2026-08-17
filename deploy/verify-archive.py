"""End-to-end verification of the archive playback and clip endpoints."""
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/camera")
import camera_service as cs

cs.AUTH_ENABLED = False
# The module no longer builds its camera registry at import time - main() does.
# Without this the per-camera index, storage and recorder objects do not exist
# and every global the routes still use is None.
cs.build_cameras()
print("  camera under test:", cs.primary().cid, "->", cs.primary().video_dir)
c = cs.app.test_client()
fails = []


def check(desc, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + desc + (("  " + extra) if extra else ""))
    if not cond:
        fails.append(desc)


# ---------------------------------------------------------------- index --
t0 = time.time()
cs.archive.refresh(force=True)
first = time.time() - t0
t0 = time.time()
cs.archive.refresh(force=True)
cached = time.time() - t0
check("index builds", len(cs.archive._segments) > 0,
      f"{len(cs.archive._segments)} segments in {first:.1f}s")
check("cached refresh is fast", cached < 2.0, f"{cached:.2f}s")
check("index cache persisted", cs.primary().index.index_file.exists())

day = cs.date.today()
segs = cs.archive.segments(day)
check("segments sorted and non-overlapping",
      all(segs[i]["end"] <= segs[i + 1]["start"] + 0.5
          for i in range(len(segs) - 1)))

# ------------------------------------------------------------- timeline --
r = c.get(f"/timeline?day={day.isoformat()}")
j = r.get_json()
check("/timeline 200", r.status_code == 200 and j["ok"])
check("/timeline reports segments", len(j["segments"]) > 0,
      f"{len(j['segments'])} segments, {len(j['gaps'])} gaps")
check("/timeline lists days", day.isoformat() in j["days"])
check("/timeline bad day 400", c.get("/timeline?day=nonsense").status_code == 400)
check("gaps are ordered and positive",
      all(g["to"] > g["from"] for g in j["gaps"]))

# Every gap must sit between two segments, never inside one.
covered = [(s["from"], s["to"]) for s in j["segments"]]
def inside_any(x):
    return any(lo < x < hi for lo, hi in covered)
check("no gap overlaps a segment",
      not any(inside_any((g["from"] + g["to"]) / 2) for g in j["gaps"]))

# --------------------------------------------------------------- locate --
# Pick a segment with room to probe into. The middle segment is whatever the
# last restart happened to leave, and right after a restart that is a file a
# few seconds old - probing 30s into it then fails for reasons that have
# nothing to do with the code under test.
probe_candidates = [s for s in segs if s["duration"] > 120]
assert probe_candidates, "no segment longer than 120s to test against"
mid = max(probe_candidates, key=lambda s: s["duration"])
midnight = cs.ArchiveIndex.midnight(day)
probe = mid["start"] - midnight + 30
hit = cs.archive.locate(day, probe)
check("locate finds the right file", hit and hit[0]["name"] == mid["name"],
      hit[0]["name"] if hit else "none")
check("locate offset correct", hit and abs(hit[1] - 30) < 0.5,
      f"{hit[1]:.1f}s" if hit else "")

if j["gaps"]:
    g = j["gaps"][0]
    inside = (g["from"] + g["to"]) / 2
    check("locate returns None inside a gap", cs.archive.locate(day, inside) is None)
    nxt = cs.archive.next_after(day, inside)
    check("next_after points past the gap", nxt is not None and nxt >= g["to"] - 1,
          f"{nxt:.0f}s" if nxt else "")

# ----------------------------------------------------------------- play --
r = c.get(f"/play?day={day.isoformat()}&t={probe:.0f}")
body = r.get_data()
check("/play 200 video/mp4", r.status_code == 200 and r.mimetype == "video/mp4")
check("/play returns a seekable MP4",
      body[4:8] == b"ftyp" and b"moov" in body[:len(body) // 8],
      f"{len(body)} bytes, moov at {body.find(b'moov')}")
r2 = c.get(f"/play?day={day.isoformat()}&t={probe:.0f}",
           headers={"Range": "bytes=0-1023"})
check("/play honours range requests", r2.status_code == 206,
      f"status {r2.status_code}")
check("/play window is cached and identical",
      c.get(f"/play?day={day.isoformat()}&t={probe + 20:.0f}").get_data()[:4096]
      == body[:4096])
check("/play reports its segment", r.headers.get("X-Segment") == mid["name"])
check("/play window bounded",
      float(r.headers.get("X-Window", "0")) <= cs.PLAY_WINDOW_SECONDS)

r = c.get(f"/play?day={day.isoformat()}&t=0.0")
if cs.archive.locate(day, 0.0) is None:
    jj = r.get_json()
    check("/play in a gap 404s with a hint",
          r.status_code == 404 and "next" in jj, f"next={jj.get('next')}")
check("/play bad day 400", c.get("/play?day=xx&t=0").status_code == 400)

# ----------------------------------------------------------------- clip --
clip_from = mid["start"] - midnight + 10
r = c.get(f"/clip?day={day.isoformat()}&from={clip_from:.0f}&to={clip_from + 20:.0f}")
data = r.get_data()
check("/clip 200 mp4", r.status_code == 200 and r.mimetype == "video/mp4",
      f"{len(data)} bytes")
check("/clip is a real MP4", data[4:8] == b"ftyp")
check("/clip has faststart moov",
      b"moov" in data[:len(data) // 4] if data else False)
check("/clip content-length matches", int(r.headers["Content-Length"]) == len(data))
check("/clip names the file",
      "attachment" in r.headers.get("Content-Disposition", "")
      and ".mp4" in r.headers.get("Content-Disposition", ""))
Path("/tmp/verify_clip.mp4").write_bytes(data)

check("/clip rejects empty range",
      c.get(f"/clip?day={day.isoformat()}&from=100&to=100").status_code == 400)
check("/clip rejects over-long range",
      c.get(f"/clip?day={day.isoformat()}&from=0&to={cs.CLIP_MAX_SECONDS + 60}"
            ).status_code == 400)
check("/clip bad day 400", c.get("/clip?day=zz&from=0&to=10").status_code == 400)

# Spanning clip: pick a boundary between two adjacent segments.
span_ok = False
for i in range(len(segs) - 1):
    a, b = segs[i], segs[i + 1]
    if b["start"] - a["end"] < 120 and a["duration"] > 30 and b["duration"] > 30:
        lo = a["end"] - midnight - 10
        hi = b["start"] - midnight + 15
        t0 = time.time()
        r = c.get(f"/clip?day={day.isoformat()}&from={lo:.0f}&to={hi:.0f}")
        took = time.time() - t0
        span = r.get_data()
        check("spanning clip built", r.status_code == 200 and len(span) > 10000,
              f"{len(span)} bytes in {took:.1f}s across "
              f"{a['name'].split('.')[-2]}->{b['name'].split('.')[-2]}")
        check("spanning clip reports missing time",
              "X-Missing-Seconds" in r.headers,
              f"missing={r.headers.get('X-Missing-Seconds')}s")
        Path("/tmp/verify_span.mp4").write_bytes(span)
        span_ok = True
        break
if not span_ok:
    print("  SKIP no adjacent segment pair suitable for a spanning test")

# ------------------------------------------------------------ scratch ----
leftover = list(cs.CLIP_DIR.glob("clip-*")) if cs.CLIP_DIR.exists() else []
check("no clip scratch left behind", not leftover, str(leftover[:2]))

# -------------------------------------------------------------- pages ----
r = c.get("/watch")
html = r.data.decode()
check("/watch 200", r.status_code == 200)
check("/watch has the timeline + player",
      'id="tl"' in html and 'id="player"' in html)
check("/watch has no inline script or style",
      "onclick=" not in html and "<style" not in html and 'style="' not in html)
r = c.get("/recordings")
check("archive page links to the player", b"WATCH" in r.data)

# Record what the clip actually starts at, for the visual check.
meta = {"clip_from_seconds": clip_from,
        "clip_expect": time.strftime("%H:%M:%S", time.localtime(midnight + clip_from))}
Path("/tmp/verify_meta.json").write_text(json.dumps(meta))
print("\n  clip requested at " + meta["clip_expect"])
print("\n" + ("PASS" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)
