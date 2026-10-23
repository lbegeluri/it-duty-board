#!/usr/bin/env bash
# =============================================================================
# 00-prep.sh — prepare the VM: time, packages, firewall, SSH, fail2ban, Docker
# -----------------------------------------------------------------------------
# WHY: the host is the foundation. Here the firewall (UFW) protects nginx DIRECTLY, because
#   nginx runs on the host and the app container has no network at all (see 10-deploy.sh).
# RUN : sudo bash 00-prep.sh
# KEEP your current SSH session open, and test a NEW login from the admin PC before closing it.
# Safe to re-run.
# =============================================================================
source "$(dirname "$0")/_lib.sh"
require_root
# No questions during upgrades (needrestart would otherwise ask which services to restart).
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
require_set VM_IP SSH_USER TZ_NAME APP_DIR ADMIN_SSH_NET WEB_NETS ITDUTY_ADMIN_NETS
check_nets $ADMIN_SSH_NET $WEB_NETS $BLOCK_NETS $ITDUTY_ADMIN_NETS
id "$SSH_USER" >/dev/null 2>&1 || die "User $SSH_USER does not exist"
[[ "$(lsb_release -rs)" == "24.04" ]] || warn "Scripts target Ubuntu 24.04, this is $(lsb_release -rs)"

# --- 1. Time zone and packages ---------------------------------------------
section "1. Time zone and packages"
timedatectl set-timezone "$TZ_NAME"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get -y -qq upgrade
DEBIAN_FRONTEND=noninteractive apt-get -y -qq install \
  ufw fail2ban chrony unattended-upgrades nginx curl ca-certificates gnupg jq openssl sqlite3
# nginx starts with a default site on port 80: stop it until our own config exists.
systemctl stop nginx
rm -f /etc/nginx/sites-enabled/default
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
ok "Packages installed, automatic security updates on"

# --- 2. Time sync -------------------------------
# A wrong clock means the wrong "today" on the page (the duty person flips at midnight).
section "2. Time sync (chrony; internal NTP servers first if you list any)"
{
  echo "# Managed by 00-prep.sh"
  for s in $NTP_SERVERS; do echo "server $s iburst"; done
  echo "pool ntp.ubuntu.com iburst maxsources 2"
  echo "driftfile /var/lib/chrony/chrony.drift"
  echo "makestep 1.0 3"
  echo "rtcsync"
} > /etc/chrony/chrony.conf
systemctl enable --now chrony >/dev/null
systemctl restart chrony
sleep 4
chronyc sources 2>/dev/null | sed 's/^/    /' | head -8 || true
timedatectl | grep -E 'Time zone|synchronized' | sed 's/^ */    /'

# --- 3. SSH ------------------------------------------------------------------
section "3. SSH hardening"
cat > /etc/ssh/sshd_config.d/10-hardening.conf <<EOF
# Managed by 00-prep.sh
PasswordAuthentication yes       # kept ON by choice; limited by AllowUsers, MaxAuthTries, fail2ban and the firewall
PermitRootLogin no
PubkeyAuthentication yes
AllowUsers $SSH_USER
MaxAuthTries 3
LoginGraceTime 30
X11Forwarding no
AllowAgentForwarding no
AllowTcpForwarding no
EOF
sshd -t || die "sshd config invalid: nothing applied"
systemctl reload ssh
ok "SSH reloaded"

# --- 4. Firewall -------------------------------------------------------------
# Order matters: the first matching rule wins, so the guest-network DENY rules come first.
section "4. Firewall (UFW)"
# Safety net: never enable the firewall unless an SSH session that is open RIGHT NOW comes from an allowed address.
PEERS=$(ss -Htn state established '( sport = :22 )' | awk '{print $4}' | sed 's/:[0-9]*$//' | sort -u)
python3 - $ADMIN_SSH_NET <<PYEOF2 || die "None of the open SSH sessions comes from $ADMIN_SSH_NET: enabling the firewall would lock you out. Add your address to ADMIN_SSH_NET in duty.conf."
import sys, ipaddress
peers = """$PEERS""".split()
nets = [ipaddress.ip_network(n, strict=False) for n in sys.argv[1:]]
sys.exit(0 if any(ipaddress.ip_address(p) in n for p in peers for n in nets) else 1)
PYEOF2
ok "An open SSH session comes from an allowed address ($(echo $PEERS | tr '
' ' '))"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing        # tighten later (see the hardening guide)
for n in $ADMIN_SSH_NET; do ufw allow from "$n" to any port 22 proto tcp comment 'SSH admin'; done
for n in $BLOCK_NETS; do
  ufw deny from "$n" to any port 80  proto tcp comment 'guest network'
  ufw deny from "$n" to any port 443 proto tcp comment 'guest network'
done
for n in $WEB_NETS; do
  ufw allow from "$n" to any port 80  proto tcp comment 'duty page redirect'
  ufw allow from "$n" to any port 443 proto tcp comment 'duty page'
done
ufw --force enable
ufw status numbered | sed 's/^/    /'

# --- 5. fail2ban -------------------------------------------------------------
section "5. fail2ban"
cat > /etc/fail2ban/jail.d/duty.local <<EOF
[DEFAULT]
ignoreip = 127.0.0.1/8 $ADMIN_SSH_NET
bantime  = 1h
findtime = 10m
maxretry = 5
backend  = systemd

[sshd]
enabled = true
EOF
systemctl enable --now fail2ban >/dev/null
systemctl restart fail2ban
ok "fail2ban running"

# --- 6. Kernel network settings ----------------------------------------------
section "6. sysctl"
cat > /etc/sysctl.d/99-duty-hardening.conf <<'EOF'
net.ipv4.tcp_syncookies = 1
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.icmp_echo_ignore_broadcasts = 1
net.ipv4.conf.all.log_martians = 1
kernel.kptr_restrict = 2
kernel.dmesg_restrict = 1
EOF
sysctl --system >/dev/null
ok "sysctl applied"

# --- 7. Docker -----------------------------------------------------------------
# Not added to the "docker" group on purpose: that group is root-equivalent. Use "sudo docker".
section "7. Docker (official repository)"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get -y -qq install docker-ce docker-ce-cli containerd.io docker-compose-plugin
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" },
  "no-new-privileges": true,
  "live-restore": true
}
EOF
systemctl enable docker >/dev/null
systemctl restart docker
docker --version
docker compose version

# --- 8. App folder ---------------------------------------------------------------
section "8. App folder"
mkdir -p "$APP_DIR"
chown "$SSH_USER:$SSH_USER" "$APP_DIR"
ok "$APP_DIR ready (owned by $SSH_USER)"

section "Result"
if [[ $WARNINGS -eq 0 ]]; then ok "Host is ready. Open a SECOND SSH session from the admin PC now to prove you can still log in."
else warn "$WARNINGS warning(s) above: read them"; fi
info "Next: upload the app (the assistant does this), then:  sudo bash 09-cert.sh selfsigned && sudo bash 10-deploy.sh"
