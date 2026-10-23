#!/usr/bin/env bash
# =============================================================================
# _lib.sh — shared helpers, "sourced" (loaded) by every script in this folder
#   set -e          : stop at the first failing command (no half-done changes)
#   set -u          : using an unset variable is an error (catches typos)
#   set -o pipefail : a pipeline fails if ANY part fails
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=duty.conf
source "$SCRIPT_DIR/duty.conf"

WARNINGS=0
section() { printf '\n\e[1;36m== %s ==\e[0m\n' "$*"; }
info()    { printf '\e[34m[i]\e[0m  %s\n' "$*"; }
ok()      { printf '\e[32m[ok]\e[0m %s\n' "$*"; }
warn()    { printf '\e[33m[!]\e[0m  %s\n' "$*"; WARNINGS=$((WARNINGS + 1)); }
die()     { printf '\e[31m[x]\e[0m  %s\n' "$*" >&2; exit 1; }

require_root() { [[ $EUID -eq 0 ]] || die "Run with sudo:  sudo bash $0"; }
require_set() {
  local name
  for name in "$@"; do
    [[ -n "${!name:-}" ]] || die "Set $name in duty.conf first"
  done
}
# Every entry must look like a network (IPv4 or IPv4/prefix).
check_nets() {
  local n
  for n in "$@"; do
    [[ "$n" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?$ ]] || die "duty.conf: '$n' is not a valid network (example: 10.0.10.0/24)"
  done
}
