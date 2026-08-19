#!/bin/bash
# Is everything working? One command instead of six.
#
#   sudo deploy/health.sh              the standard pass (~2 min):
#                                        docs      documentation matches the code
#                                        live      archive + PTZ through the running service
#                                        hvac      the thermostat, live and by injected payloads
#                                        remote    the chain that lets you in from outside
#   sudo deploy/health.sh --full       adds the deep suites (~10 min):
#                                        archive   indexing, extraction, timestamps
#                                        multicam  per-camera isolation
#   sudo deploy/health.sh --watch [s]  sit on udp/51820 while you toggle a VPN client
#   deploy/health.sh docs remote       run only the named checks
#
# Each check is a file under deploy/checks/ and runs on its own too. Without
# root, the remote check skips its privileged links and says so; everything
# else works unprivileged.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
CHECKS="$DIR/checks"
PY=/opt/camera/venv/bin/python

case "${1:-}" in
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    --watch)   shift; exec bash "$CHECKS/remote.sh" --watch "${1:-180}" ;;
esac

run_docs()     { python3 "$CHECKS/docs.py"; }
run_live()     { bash "$CHECKS/live.sh"; }
run_hvac()     { bash "$CHECKS/hvac.sh"; }
run_remote()   { bash "$CHECKS/remote.sh"; }
run_archive()  { "$PY" "$CHECKS/archive.py"; }
run_multicam() { "$PY" "$CHECKS/multicam.py"; }

if [ "$#" -gt 0 ] && [ "$1" != "--full" ]; then
    LIST="$*"
else
    LIST="docs live hvac remote"
    if [ "${1:-}" = "--full" ]; then LIST="$LIST archive multicam"; fi
fi

for c in $LIST; do
    case "$c" in
        docs|live|hvac|remote|archive|multicam) : ;;
        *) echo "unknown check '$c' - known: docs live hvac remote archive multicam"
           exit 1 ;;
    esac
done

declare -A RESULT
FAILED=0
for c in $LIST; do
    printf '\n================ %s ================\n' "$c"
    if "run_$c"; then RESULT[$c]=PASS; else RESULT[$c]=FAIL; FAILED=$((FAILED+1)); fi
done

printf '\n================ summary ================\n'
for c in $LIST; do printf '  %-9s %s\n' "$c" "${RESULT[$c]}"; done
if [ "$FAILED" -eq 0 ]; then
    echo "HEALTH PASS"
else
    echo "HEALTH FAILURES: $FAILED check(s)"
fi
exit "$((FAILED > 0 ? 1 : 0))"
