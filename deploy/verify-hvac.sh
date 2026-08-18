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

echo "$STATE" | venv/bin/python -c '
import json, sys
s = json.load(sys.stdin)
age, fresh = s.get("age_s"), s.get("fresh")
assert age is not None, "no age_s"
assert fresh == (age < 900), "fresh=%s does not follow from age=%s" % (fresh, age)
' && ok "freshness follows from the reading age" \
  || bad "freshness does not follow from the reading age"

# The live checks above can only see whatever the thermostat happens to be
# doing. These feed the class known payloads, so the cases that caused real
# bugs - retained replay, a dead node, the wrong temperature scale - are
# checked every run rather than whenever the house cooperates.
timeout 60 venv/bin/python - <<'PY'
import sys, time
sys.path.insert(0, "/opt/camera")
import camera_service as cs
import json as _j

NODE = int(cs.HVAC_NODE.rsplit("_", 1)[-1])
F, C = "°F", "°C"


def fresh_hvac(unit=F):
    h = cs.Hvac()
    if unit:
        h._absorb_units({"result": [{"id": NODE, "values": {
            "49-0-Air temperature": {"unit": unit},
            "67-0-setpoint-1": {"unit": unit},
            "67-0-setpoint-2": {"unit": unit},
            "67-0-setpoint-11": {"unit": unit},
            "67-0-setpoint-12": {"unit": unit}}}]})
    return h


def feed(h, leaf, value, ago=0):
    h.ingest("%s/%s" % (h.base, leaf), _j.dumps(
        {"time": int((time.time() - ago) * 1000), "value": value}).encode())


fails = []


def check(name, cond):
    print(("  OK   " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


# Retained replay: the broker hands over hours-old values the moment we
# connect. Arrival time would call them new.
h = fresh_hvac()
feed(h, "49/0/Air_temperature", 72, ago=7200)
s = h.state
check("a 2h-old retained reading is not fresh", s["fresh"] is False)
check("its age is reported honestly", 7100 < s["age_s"] < 7300)

# A dead-node notice must not refresh the clock that decides it is alive.
h = fresh_hvac()
feed(h, "49/0/Air_temperature", 72)
h.ingest("%s/status" % h.base, b'{"value": false}')
s = h.state
check("a dead node reads offline even with a fresh value",
      s["alive"] is False and s["online"] is False)
check("a node that never reported liveness stays unknown",
      fresh_hvac().state["alive"] is None)

# Temperature scale, which is a property of the device, not an assumption.
h = fresh_hvac(F); feed(h, "49/0/Air_temperature", 80)
check("a Fahrenheit device is not converted", h.state["temperature_f"] == 80.0)
h = fresh_hvac(C); feed(h, "49/0/Air_temperature", 26.5)
check("a Celsius device is converted", h.state["temperature_f"] == 79.7)
h = fresh_hvac(None); feed(h, "49/0/Air_temperature", 80)
s = h.state
check("an unknown unit yields no Fahrenheit claim",
      s["temperature_f"] is None and s["temperature_raw"] == 80)
check("a write is refused while the unit is unknown",
      h.set_setpoint("cool", 72)[0] is False)

# Operating state, which used to be shifted by two.
h = fresh_hvac()
feed(h, "66/0/state", 5)
check("state 5 is pending cool, not aux heat",
      h.state["operating_label"] == "Pending cool" and h.state["running"] is False)
feed(h, "66/0/state", 8)
check("state 8 decodes instead of falling through",
      h.state["operating_label"] == "2nd stage heat" and h.state["running"] is True)

# Eco: the eco setpoints are the ones driving the furnace.
h = fresh_hvac()
feed(h, "64/0/mode", 11)
feed(h, "67/0/setpoint/1", 70); feed(h, "67/0/setpoint/11", 62)
s = h.state
check("eco mode surfaces the eco setpoint", s["eco"] is True and s["setpoint_heat"] == 62)

# Clamping, at the edges.
h = fresh_hvac()
check("45F is allowed", h.set_setpoint("cool", 45)[0] or "no broker" in h.set_setpoint("cool", 45)[1])
check("44F is refused", h.set_setpoint("cool", 44)[0] is False
      and "45-90" in h.set_setpoint("cool", 44)[1])

sys.exit(1 if fails else 0)
PY
[ $? -eq 0 ] || FAILS=$((FAILS+1))

echo
echo "== writing =="
# Ask for the value it already holds, so a success would change nothing.
# Write back the value it already holds. That exercises the whole path -
# route, clamp, gateway, radio, verdict - without moving anyone's thermostat.
SP=$(echo "$STATE" | venv/bin/python -c \
  'import json,sys; v=json.load(sys.stdin).get("setpoint_cool"); print(v if v is not None else "")')
if [ -z "$SP" ]; then
  bad "no cool setpoint reported, cannot exercise the write path"
  SP=72
fi
CODE=$(post "hvac/cool" "{\"value\": $SP}" | cut -d' ' -f1)
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
