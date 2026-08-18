#!/usr/bin/env python3
"""Fail when the documentation has drifted away from the code.

Written after an audit found 35 of the 60 settings the service reads appeared
in neither the installer nor the docs, and install.sh had not heard of three
whole subsystems. Documentation rots silently; this makes it rot loudly.

    python3 deploy/verify-docs.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE = ROOT / "camera_service.py"
EXAMPLE = ROOT / "deploy" / "camera-service.env.example"
INSTALL = ROOT / "deploy" / "install.sh"
README = ROOT / "deploy" / "README.md"

fails = []


def check(ok, msg):
    print(("  OK   " if ok else "  FAIL ") + msg)
    if not ok:
        fails.append(msg)


def env_names_in_code() -> set:
    """Every setting camera_service.py reads, however it spells the call."""
    t = CODE.read_text(encoding="utf-8")
    names = set()
    for m in re.finditer(r'\b_env(?:_int)?\(\s*"([A-Z0-9_]+)"', t):
        names.add(m.group(1))
    for m in re.finditer(r'os\.environ\.get\(\s*"([A-Z0-9_]+)"', t):
        names.add(m.group(1))
    for m in re.finditer(r'legacy\s*=\s*"([A-Z0-9_]+)"', t):
        names.add(m.group(1))
    for m in re.finditer(
            r'_cam_env(?:_int)?\(\s*"([a-z0-9]+)"\s*,\s*"([A-Z0-9_]+)"', t):
        names.add("CAM_%s_%s" % (m.group(1).upper(), m.group(2)))
    return names


print("== every setting the code reads is in the env example ==")
code_names = env_names_in_code()
example = EXAMPLE.read_text(encoding="utf-8") if EXAMPLE.exists() else ""
missing = sorted(n for n in code_names
                 if not re.search(r"^%s=" % re.escape(n), example, re.M))
check(EXAMPLE.exists(), "%s exists" % EXAMPLE.name)
check(not missing, "all %d settings documented%s" % (
    len(code_names), "" if not missing else " - missing: " + ", ".join(missing)))

# The reverse: a setting that was renamed away leaves a stale entry behind,
# which is worse than a missing one because it looks authoritative.
documented = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", example, re.M))
# These are read by the deploy scripts and zwave-js-ui, not by the service.
EXTERNAL = {"ZWAVE_MQTT_USER", "ZWAVE_MQTT_PASS"}
stale = sorted(documented - code_names - EXTERNAL)
check(not stale, "no settings documented that the code never reads%s"
      % ("" if not stale else " - stale: " + ", ".join(stale)))

print("\n== the installer covers what the deployment actually contains ==")
inst = INSTALL.read_text(encoding="utf-8") if INSTALL.exists() else ""
for label, needles in [
        ("per-camera video directories", ["videos/"]),
        ("the udev rules", ["70-serial-adapters", "60-io-scheduler"]),
        ("extra broker identities", ["ratgdo", "zwave"]),
        ("the env example", ["camera-service.env.example"]),
]:
    check(all(n in inst for n in needles), label)

print("\n== shell scripts can actually run on the target ==")
# install.sh was committed with CRLF line endings and had never parsed: a
# fresh install died at `case "$1" in\r` before doing anything at all. Nothing
# caught it because nothing ever ran it. This does.
import subprocess
for f in sorted((ROOT / "deploy").glob("*.sh")):
    crlf = f.read_bytes().count(b"\r\n")
    check(crlf == 0, "%s has unix line endings%s" % (
        f.name, "" if not crlf else
        " - %d CRLF lines, bash will not parse it" % crlf))
    r = subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True)
    check(r.returncode == 0, "%s parses%s" % (
        f.name, "" if r.returncode == 0 else " - " + r.stderr.strip()[:90]))

print("\n== no credentials committed to the tree ==")
# The camera's real password sat in deploy/README.md for 14 commits of a
# public repository, because the paragraph explaining that shell
# metacharacters must be quoted used the actual value as its example.
#
# Only a literal assignment counts. `MQTT_PASSWORD = _env("MQTT_PASSWORD", "")`
# is the code READING a secret, which is exactly what it should do, and a
# scanner that cannot tell the difference gets switched off within a week.
ASSIGN = re.compile(
    r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*"
    r"(?:PASS|PASSWORD|PASSWD|SECRET|TOKEN|APIKEY|KEY))"
    r"\s*[=:]\s*(.+?)\s*(?:#.*)?$")
LITERAL = re.compile(r"""^(["']?)([^"']*)\1$""")
INNOCENT = re.compile(
    r"(^$|^<.*>$|/|\\|example|sample|placeholder|changeme|redacted|"
    r"your[-_ ]?|xxx|\.\.\.|generated|set by |^\d+$)", re.I)
SKIP_DIRS = {".git", "venv", "videos", "logs", "__pycache__", "static", "clips"}
SKIP_SUFFIX = {".ttf", ".jpg", ".jpeg", ".png", ".ts", ".mp4", ".woff2"}

leaks = []
for f in sorted(ROOT.rglob("*")):
    rel = f.relative_to(ROOT)
    if not f.is_file() or any(part in SKIP_DIRS for part in rel.parts):
        continue
    if f.suffix.lower() in SKIP_SUFFIX:
        continue
    try:
        text = f.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    for n, line in enumerate(text.splitlines(), 1):
        m = ASSIGN.match(line)
        if not m:
            continue
        name, rhs = m.group(1), m.group(2).strip()
        lit = LITERAL.match(rhs)
        if not lit:
            continue                      # an expression, not a literal secret
        value = lit.group(2)
        # A value containing an expansion is being computed, not embedded:
        # DESEC_TOKEN="${TOKEN}" writes a secret to a root-only file, and
        # CLIENT_KEY=$(wg genkey) mints one. Both are correct code.
        if "$" in value or "`" in value or "%s" in value:
            continue
        if len(value) < 8 or INNOCENT.search(value):
            continue
        leaks.append("%s:%d %s" % (rel, n, name))

check(not leaks, "no literal credentials in tracked files%s"
      % ("" if not leaks else " - " + "; ".join(leaks)))

print("\n== deploy files are either installed or explained ==")
# A file nobody runs and nobody mentions is either dead or a forgotten step.
readme = README.read_text(encoding="utf-8") if README.exists() else ""
orphans = []
for f in sorted((ROOT / "deploy").iterdir()):
    if f.name in ("README.md", "install.sh") or f.is_dir():
        continue
    if f.name in inst or f.name in readme:
        continue
    orphans.append(f.name)
check(not orphans, "every deploy file is installed or documented%s"
      % ("" if not orphans else " - orphaned: " + ", ".join(orphans)))

print()
if fails:
    print("DOCS FAILURES: %d" % len(fails))
    sys.exit(1)
print("DOCS PASS")
