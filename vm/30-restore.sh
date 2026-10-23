#!/usr/bin/env bash
# =============================================================================
# 30-restore.sh — put a database backup into the running site (accounts, staff, schedule, audit log)
# -----------------------------------------------------------------------------
# Use it to move data from another copy of the site, or to recover after a crash.
# The current database is kept as itduty.db.pre-restore.<time> first. Logins keep working with the same
# passwords (only hashes are stored); everybody has to sign in again because sessions are not part of a backup.
# RUN : sudo bash 30-restore.sh /path/to/itduty-backup.db
# =============================================================================
source "$(dirname "$0")/_lib.sh"
require_root
require_set APP_DIR

SRC="${1:-}"
[[ -f "$SRC" ]] || die "Usage: sudo bash $0 <backup-file.db>"
DB="$APP_DIR/data/itduty.db"

section "1. Check the backup file"
[[ "$(sqlite3 "$SRC" 'PRAGMA integrity_check;')" == "ok" ]] || die "The backup fails its integrity check: nothing was changed"
for t in users staff duty audit; do printf '    %-6s %s rows\n' "$t" "$(sqlite3 "$SRC" "SELECT count(*) FROM $t;")"; done

section "2. Replace the database"
(cd "$APP_DIR" && docker compose stop it-duty)
if [[ -f "$DB" ]]; then cp -p "$DB" "$DB.pre-restore.$(date +%Y%m%d-%H%M%S)"; ok "kept the old database as $DB.pre-restore.*"; fi
rm -f "$DB-wal" "$DB-shm"
install -m 640 -o 10001 -g 10001 "$SRC" "$DB"
[[ "$(sqlite3 "$DB" 'PRAGMA integrity_check;')" == "ok" ]] && ok "restored database passes the integrity check"

section "3. Start again and compare"
(cd "$APP_DIR" && docker compose up -d)
for _ in $(seq 1 20); do
  [[ "$(docker inspect -f '{{.State.Health.Status}}' it-duty 2>/dev/null)" == "healthy" ]] && break
  sleep 2
done
docker exec it-duty flask list-users | sed 's/^/    /'
ok "Done. Open the site and sign in with the existing accounts."
