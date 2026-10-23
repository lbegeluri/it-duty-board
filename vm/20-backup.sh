#!/usr/bin/env bash
# =============================================================================
# 20-backup.sh — nightly backup of the duty database (and the way to run one right now)
# -----------------------------------------------------------------------------
#   sudo bash 20-backup.sh          make a backup now and install the nightly job (03:00)
# The app itself writes a consistent copy (safe while the site is running) into
# $APP_DIR/data/backups and keeps the newest 14. Copying them off the VM (a file share, NAS, object storage...) is a separate,
# optional step: set BACKUP_COPY_CMD in duty.conf to a command that receives the file path as $1.
# =============================================================================
source "$(dirname "$0")/_lib.sh"
require_root
require_set APP_DIR

section "1. Backup now"
NEWEST=$(docker exec it-duty flask backup | tail -1)
ok "Created $APP_DIR/data/backups/$(basename "$NEWEST")"

if [[ -n "${BACKUP_COPY_CMD:-}" ]]; then
  section "2. Copy off the VM"
  bash -c "$BACKUP_COPY_CMD" _ "$APP_DIR/data/backups/$(basename "$NEWEST")" && ok "copied" || warn "copy command failed"
else
  info "No BACKUP_COPY_CMD set: backups stay on this VM only (add an off-site copy later)"
fi

section "3. Nightly job"
SELF="$(readlink -f "$0")"
echo "0 3 * * * root /usr/bin/bash $SELF >> /var/log/duty-backup.log 2>&1" > /etc/cron.d/duty-backup
chmod 644 /etc/cron.d/duty-backup
ok "Installed /etc/cron.d/duty-backup (every day at 03:00, log in /var/log/duty-backup.log)"
ls -l "$APP_DIR/data/backups" | tail -5 | sed 's/^/    /'
