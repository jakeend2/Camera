#!/bin/bash
# Verify the thermostat panel through the RUNNING service, the same way
# verify-live.sh does the archive: real systemd sandbox, real cheroot, real
# TLS, real session cookie. Written after a write that the Z-Wave driver had
# refused outright was reported to the page as applied - the service published
# a command and returned success without ever reading the gateway's verdict.
cd /opt/camera || exit 1

UA="hvac-verify/1.0"
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
ok()  { echo "  OK   $1"; }
bad() { echo "  FAIL $1"; FAILS=$((FAILS+1)); }

post() {  # post <path> <body> -> "<code> <body>"
  "${Q[@]}" -o /tmp/hv.json -w '%{http_code}' -X POST \
    -H 'content-type: application/json' -d "$2" "$BASE/$1"
  echo " $(cat /tmp/hv.json)"
}

echo "== reading =="
STATE=$("${Q[@]}" "$BASE/hvac")
echo "$STATE" | venv/bin/python -c '
import json, sys
s = json.load(sys.stdin)
print("  state: online=%s fresh=%s alive=%s age=%ss eco=%s" % (
    s.get("online"), s.get("fresh"), s.get("alive"), s.get("age_s"), s.get("eco")))
print("  temp=%s mode=%s running=%s op=%s" % (
    s.get("temperature_f"), s.get("mode_label"), s.get("running"),
    s.get("operating_label")))
for k in ("fresh", "alive", "age_s", "running", "eco"):
    assert k in s, "missing field " + k
'
[ $? -eq 0 ] && ok "GET /hvac carries the new liveness fields" \
             || bad "GET /hvac is missing fields"

# The retained values are hours old. Freshness now comes from the timestamp
# the gateway puts in each payload, not from when we happened to receive it,
# so a reconnect must not make them look new.
echo "$STATE" | grep -q '"fresh": *false' \
  && ok "hours-old retained values report fresh=false" \
  || bad "stale values are being reported as fresh"

echo
echo "== writing =="
# Ask for the value it already holds, so a success would change nothing.
CODE=$(post "hvac/cool" '{"value": 72}' | cut -d' ' -f1)
echo "  -> $CODE $(cat /tmp/hv.json)"
# Whether the radio accepts or refuses depends on the thermostat, so both are
# a pass. What must never happen again is ok:true with nothing behind it.
venv/bin/python - "$CODE" <<'PY'
import json, sys
code = sys.argv[1]
r = json.load(open("/tmp/hv.json"))
if r.get("ok"):
    assert code == "200", "ok:true with status " + code
    print("  accepted" + (" - " + r["note"] if r.get("note") else ""))
else:
    assert code == "400", "ok:false with status " + code
    assert r.get("error"), "refused without saying why"
    assert not r.get("note"), "a refusal must not carry a note"
PY
[ $? -eq 0 ] && ok "the write reports what the radio actually did" \
             || bad "write result is not trustworthy"

echo
echo "== rejecting bad input =="
R=$(post "hvac/cool" '{"value": 200}');   echo "  clamp:     $R"
[ "${R:0:3}" = "400" ] && ok "200F refused" || bad "200F was not refused"
R=$(post "hvac/cool" '{"value": 1e999}'); echo "  infinity:  $R"
[ "${R:0:3}" = "400" ] && ok "infinity refused, not a 500" || bad "infinity was not handled"
R=$(post "hvac/cool" '[1,2]');            echo "  list body: $R"
[ "${R:0:3}" = "400" ] && ok "non-object body refused, not a 500" || bad "list body broke it"
R=$(post "hvac/nonsense" '{"value": 1}'); echo "  unknown:   $R"
[ "${R:0:3}" = "400" ] && ok "unknown control refused" || bad "unknown control accepted"
R=$(post "hvac/mode" '{"value": 7}');     echo "  bad mode:  $R"
[ "${R:0:3}" = "400" ] && ok "unwritable mode refused" || bad "mode 7 accepted"

echo
echo "== no session =="
U=$(curl -s --cacert /etc/camera-tls/server.crt --resolve "$HOST:5000:127.0.0.1" \
    -o /tmp/hv_un.json -w '%{http_code}' "$BASE/hvac")
echo "  -> $U $(head -c 90 /tmp/hv_un.json)"
{ [ "$U" = "401" ] && grep -q '"ok": *false' /tmp/hv_un.json; } \
  && ok "401 JSON rather than a login redirect" \
  || bad "unauthenticated /hvac did not answer 401 JSON"

echo
[ "$FAILS" -eq 0 ] && echo "HVAC PASS" || echo "HVAC FAILURES: $FAILS"
exit $((FAILS > 0))
