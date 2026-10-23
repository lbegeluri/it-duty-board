#!/usr/bin/env bash
# =============================================================================
# 40-hostnames.sh — host names of the PCs that logged in to the admin panel (shown on the Users page)
# -----------------------------------------------------------------------------
# WHY ON THE HOST: the app container has no network at all, so it cannot ask DNS. This script runs on the VM:
#   it asks the app for the client addresses seen in login events (flask login-ips), looks every address up
#   (reverse DNS through the VM's resolvers, /etc/hosts) and writes <data>/hostnames.json, which the app reads.
#   Addresses without a PTR record are simply left out (the page shows a dash).
#   sudo bash 40-hostnames.sh            resolve now
#   sudo bash 40-hostnames.sh install    resolve now, and again every 10 minutes (cron)
# Safe to re-run. It never touches the database: it only writes the small JSON file.
# =============================================================================
source "$(dirname "$0")/_lib.sh"
require_root
require_set APP_DIR

OUT="$APP_DIR/data/hostnames.json"

resolve_now() {
  local ips
  ips=$(docker exec it-duty flask login-ips 2>/dev/null) || { warn "the it-duty container is not running: nothing resolved"; return 1; }
  python3 -c '
import ipaddress, json, os, subprocess, sys
out = sys.argv[1]
names = {}
for line in sys.stdin.read().split():
    try:
        ip = str(ipaddress.ip_address(line.strip()))
    except ValueError:
        continue
    try:
        r = subprocess.run(["getent", "hosts", ip], capture_output=True, text=True, timeout=4)
    except subprocess.TimeoutExpired:
        continue
    fields = r.stdout.split()
    if r.returncode == 0 and len(fields) >= 2:
        names[ip] = fields[1]
tmp = out + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(names, f, ensure_ascii=False, indent=1)
os.chmod(tmp, 0o640)
os.chown(tmp, 10001, 10001)        # the app user inside the container
os.replace(tmp, out)
print("%d address(es) resolved to a host name" % len(names))
' "$OUT" <<<"$ips"
}

case "${1:-run}" in
  run)
    resolve_now
    ;;
  install)
    section "Host names for the Users page"
    resolve_now || true
    SELF="$(readlink -f "$0")"
    echo "*/10 * * * * root /usr/bin/bash $SELF run >> /var/log/duty-hostnames.log 2>&1" > /etc/cron.d/duty-hostnames
    chmod 644 /etc/cron.d/duty-hostnames
    ok "Installed /etc/cron.d/duty-hostnames (every 10 minutes, log in /var/log/duty-hostnames.log)"
    ;;
  *) die "Usage: sudo bash $0 [run | install]" ;;
esac
