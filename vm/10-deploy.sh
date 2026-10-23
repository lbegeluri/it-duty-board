#!/usr/bin/env bash
# =============================================================================
# 10-deploy.sh — build and start the IT-duty site (nginx on the host + app in a no-network container)
# -----------------------------------------------------------------------------
#   browser --443--> nginx (host, firewall UFW protects it) --unix socket--> it-duty container
#
# WHY A SOCKET AND "network_mode: none": the container has NO network interface at all. It cannot reach
#   the internet or any other machine, and nothing can reach it except through the socket file that
#   nginx opens. The app never needed a network: no outside calls, local fonts, local database.
# RUN : sudo bash 10-deploy.sh        (safe to re-run: rebuilds the image and refreshes the config)
# Needs: the app in $APP_DIR/itduty (Dockerfile, requirements.txt, app/) and a certificate
#        ($APP_DIR/certs: run 09-cert.sh selfsigned first).
# =============================================================================
source "$(dirname "$0")/_lib.sh"
require_root
require_set APP_DIR ITDUTY_HOST VM_IP TZ_NAME WEB_NETS ITDUTY_ADMIN_NETS
check_nets $WEB_NETS $BLOCK_NETS $ITDUTY_ADMIN_NETS

# The app sources travel next to these scripts: either in a folder "itduty" (copied layout) or, when the scripts are
# run from a clone of the repository, in the folder above (Dockerfile, requirements.txt, app/). Copy them into place.
SRC_APP=""
if [[ -f "$SCRIPT_DIR/itduty/Dockerfile" ]]; then SRC_APP="$SCRIPT_DIR/itduty"
elif [[ -f "$SCRIPT_DIR/../Dockerfile" && -d "$SCRIPT_DIR/../app" ]]; then SRC_APP="$SCRIPT_DIR/.."; fi
if [[ -n "$SRC_APP" ]]; then
  mkdir -p "$APP_DIR/itduty"
  cp -a "$SRC_APP/Dockerfile" "$SRC_APP/requirements.txt" "$SRC_APP/app" "$APP_DIR/itduty/"
  chown -R "$SSH_USER:$SSH_USER" "$APP_DIR/itduty"
fi
[[ -f "$APP_DIR/itduty/Dockerfile" ]] || die "App sources missing: expected $APP_DIR/itduty/Dockerfile"
[[ -s "$APP_DIR/certs/duty.crt" && -s "$APP_DIR/certs/duty.key" ]] || die "No certificate yet: run  sudo bash 09-cert.sh selfsigned"

# --- 1. Folders and the socket directory --------------------------------------
section "1. Folders"
mkdir -p "$APP_DIR/data"
[[ "$(stat -c %u "$APP_DIR/data")" == 10001 ]] || chown -R 10001:10001 "$APP_DIR/data"
chmod 750 "$APP_DIR/data"
# The socket FILE is world-writable (umask 0) because gunicorn re-owns it to its own group; access is controlled by
# this FOLDER instead (mode 2750: only uid 10001 and group www-data = nginx can even enter it).
# /run is wiped at every boot, so a tmpfiles rule recreates the socket folder: owner = the container user,
# group = nginx's group (www-data), setgid so the socket file inherits that group.
echo 'd /run/duty 2750 10001 www-data -' > /etc/tmpfiles.d/duty.conf
systemd-tmpfiles --create /etc/tmpfiles.d/duty.conf
ok "Data folder $APP_DIR/data (uid 10001) and socket folder /run/duty"

# --- 2. docker-compose.yml -------------------------------------------------------
section "2. Container definition"
cat > "$APP_DIR/docker-compose.yml" <<'EOF'
services:
  it-duty:
    build: ./itduty
    image: it-duty:local
    container_name: it-duty
    restart: unless-stopped
    network_mode: none                 # no network interface: no internet, no LAN, nothing
    user: "10001:10001"
    command: ["gunicorn", "--bind", "unix:/run/duty/duty.sock", "--umask", "0", "--workers", "2",
              "--preload", "--worker-tmp-dir", "/dev/shm", "--access-logfile", "-", "app.main:app"]
    environment:
      TZ_NAME: "@@TZ@@"
      COOKIE_SECURE: "1"               # the login cookie travels only over HTTPS
      ADMIN_NETS: "@@ADMIN_NETS@@"     # shown read-only on the Settings page
    volumes:
      - ./data:/data                   # database, secret key and backups live on the host
      - /run/duty:/run/duty            # the socket nginx talks to
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    mem_limit: 256m
    pids_limit: 100
    healthcheck:
      test: ["CMD", "python", "-c", "import socket; s=socket.socket(socket.AF_UNIX); s.settimeout(2); s.connect('/run/duty/duty.sock'); s.sendall(b'GET /healthz HTTP/1.0\\r\\nHost: x\\r\\n\\r\\n'); assert b' 200 ' in s.recv(200)"]
      interval: 30s
      timeout: 4s
      retries: 3
EOF
sed -i -e "s|@@TZ@@|$TZ_NAME|" -e "s|@@ADMIN_NETS@@|$ITDUTY_ADMIN_NETS|" "$APP_DIR/docker-compose.yml"
(cd "$APP_DIR" && docker compose config -q) && ok "compose file valid"

# --- 3. nginx -----------------------------------------------------------------------
section "3. nginx"
# 127.0.0.1 may read the PUBLIC page (this VM's own health checks and tests); /admin stays closed to it.
BLOCK_LINES=""; ALLOW_LINES="    allow 127.0.0.1;"$'
'; ADMIN_LINES=""
for n in ${BLOCK_NETS:-}; do BLOCK_LINES+="    deny $n;"$'\n'; done
for n in $WEB_NETS;       do ALLOW_LINES+="    allow $n;"$'\n'; done
# /admin has its own allow/deny list inside its locations (it REPLACES the one above), so the guest networks are refused here too.
for n in ${BLOCK_NETS:-}; do ADMIN_LINES+="        deny $n;"$'\n'; done
for n in $ITDUTY_ADMIN_NETS; do ADMIN_LINES+="        allow $n;"$'\n'; done
# Old names that only redirect to the main name (permanent redirect, same path and query, no page in between).
# They need a valid certificate too, or browsers would warn before following the redirect: 09-cert.sh puts them in the request.
REDIRECT_BLOCK=""
if [[ -n "${REDIRECT_HOSTS:-}" ]]; then
  REDIRECT_BLOCK=$(cat <<EOF
server {                                   # old name(s), plain HTTP
    listen 80;
    server_name $REDIRECT_HOSTS;
    return 301 https://$ITDUTY_HOST\$request_uri;
}
server {                                   # old name(s), HTTPS: the same certificate, then the redirect
    listen 443 ssl http2;
    server_name $REDIRECT_HOSTS;
    ssl_certificate     $APP_DIR/certs/duty.crt;
    ssl_certificate_key $APP_DIR/certs/duty.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    return 301 https://$ITDUTY_HOST\$request_uri;
}
EOF
)
fi
cat > /etc/nginx/conf.d/duty.conf <<'EOF'
# Managed by 10-deploy.sh (generated from duty.conf): do not edit by hand.
server_tokens off;
limit_req_zone $binary_remote_addr zone=dutylogin:10m rate=30r/m;

upstream duty { server unix:/run/duty/duty.sock; }

server {                                   # plain HTTP: only a redirect for the real name
    listen 80;
    server_name @@HOST@@ @@EXTRA_HOSTS@@;
    return 301 https://$host$request_uri;
}
@@REDIRECT_BLOCK@@
server {                                   # any other name or the bare IP on port 80 is dropped
    listen 80 default_server;
    return 444;
}
server {                                   # TLS requests for any other name are refused during the handshake
    listen 443 ssl http2 default_server;
    ssl_reject_handshake on;
}

server {
    listen 443 ssl http2;
    server_name @@HOST@@ @@EXTRA_HOSTS@@;

    ssl_certificate     @@APP_DIR@@/certs/duty.crt;
    ssl_certificate_key @@APP_DIR@@/certs/duty.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:SSL:10m;
    ssl_session_tickets off;

    # Security headers (CSP, nosniff, referrer policy, framing) are set by the app itself: not repeated here.
    proxy_hide_header Server;

    # Public page: internal networks only. First match wins, so the guest networks are refused first.
@@BLOCK_LINES@@@@ALLOW_LINES@@    deny all;

    client_max_body_size 1m;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # /admin: its own allow/deny list (by default the same networks as the public page; a user name and password
    # are still required). A list inside a location REPLACES the one above, so the guest networks are denied here too.
    location = /admin/login {
@@ADMIN_LINES@@        deny all;
        limit_req zone=dutylogin burst=5 nodelay;
        proxy_pass http://duty;
    }
    location ~ ^/admin(/|$) {
@@ADMIN_LINES@@        deny all;
        proxy_pass http://duty;
    }
    location / {
        proxy_pass http://duty;
    }
}
EOF
python3 - "$BLOCK_LINES" "$ALLOW_LINES" "$ADMIN_LINES" "$ITDUTY_HOST" "$APP_DIR" "${EXTRA_HOSTS:-}" "$REDIRECT_BLOCK" <<'PYEOF'
import sys
block, allow, admin, host, app, extra, redirect = sys.argv[1:8]
p = "/etc/nginx/conf.d/duty.conf"
s = open(p, encoding="utf-8").read()
for k, v in (("@@BLOCK_LINES@@", block), ("@@ALLOW_LINES@@", allow), ("@@ADMIN_LINES@@", admin), ("@@HOST@@", host), ("@@APP_DIR@@", app), ("@@REDIRECT_BLOCK@@", redirect), ("@@EXTRA_HOSTS@@", extra)):
    s = s.replace(k, v)
open(p, "w", encoding="utf-8").write(s)
PYEOF
nginx -t
systemctl enable nginx >/dev/null
systemctl restart nginx
ok "nginx running with the new configuration"

# --- 4. Build and start --------------------------------------------------------------
section "4. Build the image and start the container (first time: a few minutes)"
(cd "$APP_DIR" && docker compose build it-duty)
(cd "$APP_DIR" && docker compose up -d)
for _ in $(seq 1 30); do
  [[ -S /run/duty/duty.sock ]] && break
  sleep 2
done
[[ -S /run/duty/duty.sock ]] || die "The app did not create its socket: check  sudo docker logs it-duty"
ls -l /run/duty/duty.sock | sed 's/^/    /'
for _ in $(seq 1 15); do
  [[ "$(docker inspect -f '{{.State.Health.Status}}' it-duty 2>/dev/null)" == "healthy" ]] && break
  sleep 2
done
info "container health: $(docker inspect -f '{{.State.Health.Status}}' it-duty)"
# First owner "admin" (temporary password, must be changed at the first login) only if NO accounts exist yet.
docker exec it-duty flask bootstrap-admin || warn "bootstrap-admin failed (check: sudo docker logs it-duty)"

# --- 5. Isolation proof -----------------------------------------------------------------
section "5. The container really has no network"
mode=$(docker inspect -f '{{.HostConfig.NetworkMode}}' it-duty)
[[ "$mode" == "none" ]] && ok "network mode = none" || warn "network mode is '$mode' (expected none)"
if docker exec it-duty python -c "import urllib.request; urllib.request.urlopen('https://example.com', timeout=3)" >/dev/null 2>&1; then
  warn "the container CAN reach the internet: it must not"
else
  ok "the container cannot reach the internet"
fi

# --- 6. Tests -------------------------------------------------------------------------------
# These run ON this VM (address $VM_IP, inside the allowed internal range), so /admin correctly answers 403.
section "6. Tests (from this VM)"
get() { curl -sk -m 8 -o /dev/null -w '%{http_code}' --resolve "$ITDUTY_HOST:443:127.0.0.1" "https://$ITDUTY_HOST$1" || true; }
chk() { printf '    %-26s expect %-10s got %s\n' "$1" "$2" "$(get "$1")"; }
chk /                200
chk /healthz         200
chk /api/today       200
chk /static/style.css 200
chk /admin/login     403
if [[ -n "${REDIRECT_HOSTS:-}" ]]; then
  for h in $REDIRECT_HOSTS; do
    printf '    %-26s expect %s\n    %-26s got    %s\n' "https://$h/x?a=1" "301 -> https://$ITDUTY_HOST/x?a=1" "" "$(curl -sk -m 8 -o /dev/null -w '%{http_code} -> %{redirect_url}' --resolve "$h:443:127.0.0.1" "https://$h/x?a=1" || true)"
    printf '    %-26s expect %s\n    %-26s got    %s\n' "http://$h/x?a=1" "301 -> https://$ITDUTY_HOST/x?a=1" "" "$(curl -s -m 8 -o /dev/null -w '%{http_code} -> %{redirect_url}' --resolve "$h:80:127.0.0.1" "http://$h/x?a=1" || true)"
  done
fi
printf '    %-26s expect %-10s got %s\n' "http:// redirect" 301 "$(curl -s -m 8 -o /dev/null -w '%{http_code}' --resolve "$ITDUTY_HOST:80:127.0.0.1" "http://$ITDUTY_HOST/" || true)"
printf '    %-26s expect %-10s got %s\n' "bare IP over https" 000 "$(curl -sk -m 8 -o /dev/null -w '%{http_code}' "https://127.0.0.1/" 2>/dev/null || true)"

# --- 7. Host names for the Users page --------------------------------------------------------
# Reverse DNS has to run on the host: the container has no network. Not critical: without it the page shows dashes.
if [[ -f "$SCRIPT_DIR/40-hostnames.sh" ]]; then
  bash "$SCRIPT_DIR/40-hostnames.sh" install || warn "could not set up the host name helper (not critical)"
fi

section "Result"
if [[ "$(openssl x509 -in "$APP_DIR/certs/duty.crt" -noout -subject)" == "$(openssl x509 -in "$APP_DIR/certs/duty.crt" -noout -issuer | sed 's/^issuer=/subject=/')" ]]; then
  ok "Deployed: https://$ITDUTY_HOST (self-signed certificate: browsers will warn until you install the CA certificate, see 09-cert.sh)"
else
  ok "Deployed: https://$ITDUTY_HOST"
fi
info "First login: user 'admin', temporary password 'admin' (the app forces a new one at once)."
[[ $WARNINGS -eq 0 ]] || warn "$WARNINGS warning(s) above"
