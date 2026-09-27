#!/usr/bin/env bash
# One-time VPS bootstrap for Ubuntu/Debian: Docker Engine + Compose plugin + firewall.
# Usage:  sudo bash scripts/install-docker.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash $0" >&2
  exit 1
fi

echo "==> Updating packages"
apt-get update -y
apt-get install -y ca-certificates curl git ufw openssl

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Installing Docker (official convenience script)"
  curl -fsSL https://get.docker.com | sh
else
  echo "==> Docker already installed: $(docker --version)"
fi

systemctl enable --now docker

# Let the invoking (non-root) user run docker without sudo.
if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
  usermod -aG docker "$SUDO_USER"
  echo "==> Added $SUDO_USER to the docker group (log out/in to take effect)"
fi

# Small VPS (1-2 GB RAM)? Chromium + n8n like some swap.
if ! swapon --show | grep -q .; then
  echo "==> Creating 2G swap file"
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> Configuring firewall (SSH, HTTP, HTTPS)"
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

docker compose version
echo
echo "Done. Next: cp .env.example .env && nano .env && docker compose up -d --build"
