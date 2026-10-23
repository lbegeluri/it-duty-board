#!/usr/bin/env bash
# =============================================================================
# 09-cert.sh — certificate for the site (self-signed first, then the one from your CA)
# -----------------------------------------------------------------------------
# WHY TWO STEPS: a self-signed certificate lets everything be tested at once, but browsers only show a
#   clean padlock for a certificate that chains to a CA they trust. If your PCs already trust
#   your company CA, the final certificate comes from it (one request).
# The private key is created HERE and never leaves this server; only the public request (CSR) goes out.
#
#   sudo bash 09-cert.sh selfsigned        # temporary, valid 1 year, browsers show a warning
#   sudo bash 09-cert.sh csr               # creates ~/duty.csr for the CA
#   sudo bash 09-cert.sh install <file>    # installs the certificate the CA issued (PEM or DER)
# =============================================================================
source "$(dirname "$0")/_lib.sh"
require_root
require_set APP_DIR ITDUTY_HOST SSH_USER

DIR="$APP_DIR/certs"
KEY="$DIR/duty.key"; CSR="$DIR/duty.csr"; CRT="$DIR/duty.crt"
mkdir -p "$DIR"; chmod 700 "$DIR"; chown root:root "$DIR"

make_key() {
  if [[ -s "$KEY" ]]; then info "Key already exists: reusing $KEY"
  else ( umask 077; openssl genrsa -out "$KEY" 3072 2>/dev/null ); ok "Created $KEY (root only, mode 600)"; fi
  chmod 600 "$KEY"; chown root:root "$KEY"
}
# All names the certificate must cover: the main name, EXTRA_HOSTS (also served) and REDIRECT_HOSTS (old names that only
# redirect: they need a valid certificate too, or browsers warn before they can follow the redirect).
san_list() { local s="DNS:$ITDUTY_HOST" h; for h in ${EXTRA_HOSTS:-} ${REDIRECT_HOSTS:-}; do s+=",DNS:$h"; done; echo "$s"; }
reload_nginx() { if systemctl is-active --quiet nginx; then nginx -t && systemctl reload nginx && ok "nginx reloaded"; fi; }

case "${1:-}" in
selfsigned)
  section "Self-signed TEST certificate for $ITDUTY_HOST (1 year)"
  warn "Browsers will show 'not secure' for it. Replace it with the CA certificate (csr, then install)."
  make_key
  openssl req -new -x509 -key "$KEY" -out "$CRT" -days 365 \
    -subj "/O=${ORG_NAME:-Example Organization} (TEST)/CN=$ITDUTY_HOST" -addext "subjectAltName=$(san_list)"
  chmod 644 "$CRT"; chown root:root "$CRT"
  openssl x509 -in "$CRT" -noout -subject -enddate | sed 's/^/    /'
  ok "Installed $CRT"
  reload_nginx
  ;;

csr)
  section "Certificate request for $ITDUTY_HOST"
  make_key
  # subjectAltName is mandatory: modern browsers ignore the CN field.
  openssl req -new -key "$KEY" -out "$CSR" -subj "/O=${ORG_NAME:-Example Organization}/CN=$ITDUTY_HOST" -addext "subjectAltName=$(san_list)"
  chmod 644 "$CSR"
  install -m 644 -o "$SSH_USER" -g "$SSH_USER" "$CSR" "/home/$SSH_USER/duty.csr"
  ok "Request written: /home/$SSH_USER/duty.csr"
  openssl req -in "$CSR" -noout -subject | sed 's/^/    /'
  openssl req -in "$CSR" -noout -text | grep -A1 'Subject Alternative Name' | sed 's/^ */    /'
  ;;

install)
  SRC="${2:-}"; CHAIN="${3:-}"
  [[ -f "$SRC" ]] || die "Usage: $0 install <issued-certificate-file> [<ca-chain.pem>]"
  [[ -s "$KEY" ]] || die "No private key at $KEY: run '$0 csr' first"
  section "Check and install"
  TMP=$(mktemp); trap 'rm -f "$TMP"' EXIT
  if grep -q 'BEGIN CERTIFICATE' "$SRC"; then cp "$SRC" "$TMP"
  else openssl x509 -inform der -in "$SRC" -out "$TMP" 2>/dev/null || die "Not a valid certificate file"; fi
  openssl x509 -in "$TMP" -noout -subject -issuer -dates | sed 's/^/    /'
  [[ "$(openssl x509 -in "$TMP" -noout -pubkey | openssl sha256)" == "$(openssl pkey -in "$KEY" -pubout | openssl sha256)" ]] \
    || die "This certificate does NOT match the private key on this server (was it issued for a different request?)"
  ok "Matches the private key"
  for NAME in $ITDUTY_HOST ${EXTRA_HOSTS:-} ${REDIRECT_HOSTS:-}; do
    openssl x509 -in "$TMP" -noout -ext subjectAltName 2>/dev/null | grep -q "DNS:$NAME" \
      || die "Certificate has no subjectAltName for $NAME (the CA template must accept the names from the request)"
    ok "Covers $NAME"
  done
  openssl x509 -in "$TMP" -noout -checkend $((7*86400)) >/dev/null || die "Certificate is expired or expires within 7 days"
  ok "Not expired"
  { cat "$TMP"; if [[ -n "$CHAIN" && -f "$CHAIN" ]]; then cat "$CHAIN"; fi; } > "$CRT"
  chmod 644 "$CRT"; chown root:root "$CRT"
  ok "Installed $CRT ($(grep -c 'BEGIN CERTIFICATE' "$CRT") certificate(s) in the file)"
  reload_nginx
  ;;

*) die "Usage: sudo bash $0 selfsigned | csr | install <issued-cert> [<chain.pem>]" ;;
esac
