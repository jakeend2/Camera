# Shared plumbing for the health checks. Sourced, never executed.
#
# Minting a session cookie needs the signing key from the systemd
# EnvironmentFile - a hand-run python does not load it, and without it the
# cookie is signed with the wrong key and the service rightly answers 401.
# This block used to be pasted identically at the top of every check that
# talks to the running service.

cd /opt/camera || exit 1

HOST=camera.jakeend2.dedyn.io
BASE="https://$HOST:5000"

set -a; . /etc/camera-service.env; set +a

# Scratch space, private to this run. The checks used to share fixed names in
# /tmp, and Debian's fs.protected_regular refuses to reopen another user's
# file there even as root - so a check run as pi and later as root read the
# PREVIOUS run's responses out of the stale file. hvac failed honestly;
# live passed dishonestly, on stale but valid mp4s.
SCRATCH=$(mktemp -d)
trap 'rm -rf "$SCRATCH"' EXIT

mint_cookie() {  # mint_cookie <user-agent>  -> prints a signed session cookie
    timeout 60 venv/bin/python - "$1" <<'PY' 2>/dev/null
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
}
