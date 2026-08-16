#!/bin/bash
# Verify the archive through the RUNNING service: real systemd sandbox, real
# cheroot, real TLS, real session cookie. The in-process test client runs as
# the invoking user outside the sandbox and cannot see problems like a
# read-only cache directory - which is exactly what it missed.
cd /opt/camera || exit 1

UA="live-verify/1.0"
# The signing key lives in the systemd EnvironmentFile, which a hand-run
# python does not load - without it the cookie is signed with the wrong key
# and the service rightly answers 401.
set -a; . /etc/camera-service.env; set +a
COOKIE=$(timeout 60 venv/bin/python - "$UA" <<'PY' 2>/dev/null
import sys
sys.path.insert(0, "/opt/camera")
import camera_service as cs
from flask_login.utils import _create_identifier
with cs.app.test_request_context(
        "/", environ_base={"REMOTE_ADDR": "127.0.0.1",
                           "HTTP_USER_AGENT": sys.argv[1]}):
    ident = _create_identifier()
print(cs.app.session_interface.get_signing_serializer(cs.app).dumps(
    {"_user_id": cs.WEB_USERNAME, "_fresh": True, "_id": ident}))
PY
)
[ -z "$COOKIE" ] && { echo "  could not mint a session cookie"; exit 1; }

HOST=camera.jakeend2.dedyn.io
BASE="https://$HOST:5000"
Q=(curl -s --cacert /etc/camera-tls/server.crt --resolve "$HOST:5000:127.0.0.1"
   -A "$UA" -b "session=$COOKIE")

FAILS=0
ok() { echo "  OK   $1  $2"; }
bad() { echo "  FAIL $1  $2"; FAILS=$((FAILS+1)); }
chk() { if [ "$1" = "1" ]; then ok "$2" "$3"; else bad "$2" "$3"; fi }

DAY=$(date +%F)
TL=$("${Q[@]}" "$BASE/timeline?day=$DAY")
echo "$TL" > /tmp/live_tl.json
NSEG=$(python3 -c "import json;print(len(json.load(open('/tmp/live_tl.json'))['segments']))" 2>/dev/null || echo 0)
chk "$([ "$NSEG" -gt 0 ] && echo 1 || echo 0)" "/timeline over HTTPS" "$NSEG segments"

# A moment that definitely has footage: 300s into the longest segment.
AT=$(python3 - <<'PY'
import json
j = json.load(open("/tmp/live_tl.json"))
seg = max(j["segments"], key=lambda s: s["to"] - s["from"])
print(int(seg["from"] + min(300, (seg["to"] - seg["from"]) / 2)))
PY
)
echo "  probing t=$AT ($(date -d @$(( $(date -d "$DAY 00:00:00" +%s) + AT )) '+%H:%M:%S'))"

CODE=$("${Q[@]}" -o /tmp/live_win.mp4 -w '%{http_code}' "$BASE/play?day=$DAY&t=$AT")
SIZE=$(stat -c%s /tmp/live_win.mp4 2>/dev/null || echo 0)
chk "$([ "$CODE" = "200" ] && [ "$SIZE" -gt 100000 ] && echo 1 || echo 0)" \
    "/play first cut" "HTTP $CODE, $((SIZE/1048576))MB"
if [ "$CODE" != "200" ]; then
  echo "       body: $(head -c 200 /tmp/live_win.mp4)"
fi

FTYP=$(python3 -c "print(open('/tmp/live_win.mp4','rb').read(8)[4:8].decode('ascii','replace'))" 2>/dev/null)
chk "$([ "$FTYP" = "ftyp" ] && echo 1 || echo 0)" "/play is an MP4" "starts with $FTYP"

DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 /tmp/live_win.mp4 2>/dev/null)
chk "$(python3 -c "print(1 if $DUR > 100 else 0)" 2>/dev/null || echo 0)" \
    "/play duration readable" "${DUR}s"

T0=$(date +%s%N)
CODE2=$("${Q[@]}" -o /dev/null -w '%{http_code}' "$BASE/play?day=$DAY&t=$((AT+30))")
T1=$(date +%s%N)
MS=$(( (T1-T0)/1000000 ))
chk "$([ "$CODE2" = "200" ] && echo 1 || echo 0)" "/play cached window" "HTTP $CODE2 in ${MS}ms"

RCODE=$("${Q[@]}" -o /dev/null -w '%{http_code}' -H "Range: bytes=0-1023" "$BASE/play?day=$DAY&t=$AT")
chk "$([ "$RCODE" = "206" ] && echo 1 || echo 0)" "/play range request" "HTTP $RCODE"

CCODE=$("${Q[@]}" -o /tmp/live_clip.mp4 -w '%{http_code}' "$BASE/clip?day=$DAY&from=$AT&to=$((AT+20))")
CSIZE=$(stat -c%s /tmp/live_clip.mp4 2>/dev/null || echo 0)
chk "$([ "$CCODE" = "200" ] && [ "$CSIZE" -gt 100000 ] && echo 1 || echo 0)" \
    "/clip download" "HTTP $CCODE, $((CSIZE/1024))KB"
if [ "$CCODE" != "200" ]; then
  echo "       body: $(head -c 200 /tmp/live_clip.mp4)"
fi

WCODE=$("${Q[@]}" -o /dev/null -w '%{http_code}' "$BASE/watch")
chk "$([ "$WCODE" = "200" ] && echo 1 || echo 0)" "/watch page" "HTTP $WCODE"

# Unauthenticated must be refused, not served.
UCODE=$(curl -s --cacert /etc/camera-tls/server.crt --resolve "$HOST:5000:127.0.0.1" \
        -o /dev/null -w '%{http_code}' "$BASE/play?day=$DAY&t=$AT")
chk "$([ "$UCODE" = "401" ] && echo 1 || echo 0)" "/play refuses anonymous" "HTTP $UCODE"

echo
echo "  cache now: $(du -sh /opt/camera/logs/media-cache 2>/dev/null | cut -f1)"
echo
[ "$FAILS" = "0" ] && echo "LIVE PASS" || echo "LIVE FAILURES: $FAILS"
exit $([ "$FAILS" = "0" ] && echo 0 || echo 1)
