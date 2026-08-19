#!/bin/bash
# Verify the archive through the RUNNING service: real systemd sandbox, real
# cheroot, real TLS, real session cookie. The in-process test client runs as
# the invoking user outside the sandbox and cannot see problems like a
# read-only cache directory - which is exactly what it missed.
. "$(dirname "$0")/lib.sh"

UA="live-verify/1.0"
COOKIE=$(mint_cookie "$UA")
[ -z "$COOKIE" ] && { echo "  could not mint a session cookie"; exit 1; }
Q=(curl -s --cacert /etc/camera-tls/server.crt --resolve "$HOST:5000:127.0.0.1"
   -A "$UA" -b "session=$COOKIE")

FAILS=0
ok() { echo "  OK   $1  $2"; }
bad() { echo "  FAIL $1  $2"; FAILS=$((FAILS+1)); }
chk() { if [ "$1" = "1" ]; then ok "$2" "$3"; else bad "$2" "$3"; fi }

DAY=$(date +%F)
TL=$("${Q[@]}" "$BASE/timeline?day=$DAY")
echo "$TL" > $SCRATCH/live_tl.json
NSEG=$(python3 -c "import json;print(len(json.load(open('$SCRATCH/live_tl.json'))['segments']))" 2>/dev/null || echo 0)
chk "$([ "$NSEG" -gt 0 ] && echo 1 || echo 0)" "/timeline over HTTPS" "$NSEG segments"

# A moment that definitely has footage: 300s into the longest segment.
AT=$(python3 - "$SCRATCH/live_tl.json" <<'PY'
import json, sys
j = json.load(open(sys.argv[1]))
seg = max(j["segments"], key=lambda s: s["to"] - s["from"])
print(int(seg["from"] + min(300, (seg["to"] - seg["from"]) / 2)))
PY
)
echo "  probing t=$AT ($(date -d @$(( $(date -d "$DAY 00:00:00" +%s) + AT )) '+%H:%M:%S'))"

CODE=$("${Q[@]}" -o $SCRATCH/live_win.mp4 -w '%{http_code}' "$BASE/play?day=$DAY&t=$AT")
SIZE=$(stat -c%s $SCRATCH/live_win.mp4 2>/dev/null || echo 0)
chk "$([ "$CODE" = "200" ] && [ "$SIZE" -gt 100000 ] && echo 1 || echo 0)" \
    "/play first cut" "HTTP $CODE, $((SIZE/1048576))MB"
if [ "$CODE" != "200" ]; then
  echo "       body: $(head -c 200 $SCRATCH/live_win.mp4)"
fi

FTYP=$(python3 -c "print(open('$SCRATCH/live_win.mp4','rb').read(8)[4:8].decode('ascii','replace'))" 2>/dev/null)
chk "$([ "$FTYP" = "ftyp" ] && echo 1 || echo 0)" "/play is an MP4" "starts with $FTYP"

DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 $SCRATCH/live_win.mp4 2>/dev/null)
chk "$(python3 -c "print(1 if $DUR > 100 else 0)" 2>/dev/null || echo 0)" \
    "/play duration readable" "${DUR}s"

T0=$(date +%s%N)
CODE2=$("${Q[@]}" -o /dev/null -w '%{http_code}' "$BASE/play?day=$DAY&t=$((AT+30))")
T1=$(date +%s%N)
MS=$(( (T1-T0)/1000000 ))
chk "$([ "$CODE2" = "200" ] && echo 1 || echo 0)" "/play cached window" "HTTP $CODE2 in ${MS}ms"

RCODE=$("${Q[@]}" -o /dev/null -w '%{http_code}' -H "Range: bytes=0-1023" "$BASE/play?day=$DAY&t=$AT")
chk "$([ "$RCODE" = "206" ] && echo 1 || echo 0)" "/play range request" "HTTP $RCODE"

CCODE=$("${Q[@]}" -o $SCRATCH/live_clip.mp4 -w '%{http_code}' "$BASE/clip?day=$DAY&from=$AT&to=$((AT+20))")
CSIZE=$(stat -c%s $SCRATCH/live_clip.mp4 2>/dev/null || echo 0)
chk "$([ "$CCODE" = "200" ] && [ "$CSIZE" -gt 100000 ] && echo 1 || echo 0)" \
    "/clip download" "HTTP $CCODE, $((CSIZE/1024))KB"
if [ "$CCODE" != "200" ]; then
  echo "       body: $(head -c 200 $SCRATCH/live_clip.mp4)"
fi

WCODE=$("${Q[@]}" -o /dev/null -w '%{http_code}' "$BASE/watch")
chk "$([ "$WCODE" = "200" ] && echo 1 || echo 0)" "/watch page" "HTTP $WCODE"

# Unauthenticated must be refused, not served.
UCODE=$(curl -s --cacert /etc/camera-tls/server.crt --resolve "$HOST:5000:127.0.0.1" \
        -o /dev/null -w '%{http_code}' "$BASE/play?day=$DAY&t=$AT")
chk "$([ "$UCODE" = "401" ] && echo 1 || echo 0)" "/play refuses anonymous" "HTTP $UCODE"

echo
# Every route that can reach the serial port, aimed at the camera with no
# motors and at an id that does not exist. /move and the preset routes used to
# skip the check the other eighteen performed, so the HTTP and MQTT command
# paths had drifted apart in silence.
#
# preset 33 on purpose: it is a reserved camera function that preset_allowed()
# refuses, so even if the camera gate were missing again this test still
# cannot move anything. Finding the hole with preset 5 moved a real camera.
PTZ_BAD=""
for route in stop pan_left pan_right tilt_up tilt_down zoom_tele zoom_wide \
             focus_near focus_far iris_open iris_close OSD_menu Tour_1 Tour_2 \
             Windshield_Wiper Wiper_Off move Set_preset Goto_preset Clear_preset; do
  for who in backyard nope; do
    BODY=$("${Q[@]}" -X POST -H 'content-type: application/json' \
           -d '{"pan":0,"tilt":0,"preset":33}' "$BASE/$route?cam=$who" 2>/dev/null)
    case "$BODY" in
      *'"ok":false'*|*'"ok": false'*) : ;;
      *) PTZ_BAD="$PTZ_BAD $route?cam=$who" ;;
    esac
  done
done
chk "$([ -z "$PTZ_BAD" ] && echo 1 || echo 0)" \
    "PTZ routes refuse a motorless and an unknown camera" \
    "${PTZ_BAD:-all 40 refused}"

echo
echo "  cache now: $(du -sh /opt/camera/logs/media-cache 2>/dev/null | cut -f1)"
echo
[ "$FAILS" = "0" ] && echo "LIVE PASS" || echo "LIVE FAILURES: $FAILS"
exit $([ "$FAILS" = "0" ] && echo 0 || echo 1)
