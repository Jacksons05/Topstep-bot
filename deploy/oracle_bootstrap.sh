#!/usr/bin/env bash
# One-shot setup for a fresh Oracle Cloud "Always Free" ARM instance
# (VM.Standard.A1.Flex, Ubuntu 22.04 aarch64).
#
# Run as the normal sudo-capable user (e.g. `ubuntu`), NOT root:
#   scp deploy/oracle_bootstrap.sh ubuntu@<vm-ip>:~/
#   ssh ubuntu@<vm-ip>
#   chmod +x oracle_bootstrap.sh && ./oracle_bootstrap.sh
#
# Assumes the repo has already been cloned to ~/Topstep-bot (git clone over
# HTTPS with a fine-grained PAT, or scp'd up). This script does NOT touch
# .env — copy that over separately with scp, never via git.

set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/Topstep-bot}"
PY_VERSION="3.12"

if [[ ! -d "$REPO_DIR" ]]; then
  echo "ERROR: $REPO_DIR not found. Clone or scp the repo there first." >&2
  exit 1
fi

echo "==> apt update + base packages"
sudo apt-get update -y
sudo apt-get install -y software-properties-common curl git ufw build-essential

echo "==> Python ${PY_VERSION} (deadsnakes PPA, arm64-supported)"
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt-get update -y
sudo apt-get install -y "python${PY_VERSION}" "python${PY_VERSION}-venv"

echo "==> Ollama (installs its own systemd service + starts on boot)"
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl enable --now ollama

echo "==> Pulling qwen2.5:14b (several GB, be patient)"
ollama pull qwen2.5:14b

echo "==> Python venv + deps"
cd "$REPO_DIR"
"python${PY_VERSION}" -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo "==> Firewall: SSH only, deny everything else inbound"
sudo ufw allow OpenSSH
sudo ufw --force enable

echo "==> Installing systemd unit"
sudo cp "$REPO_DIR/deploy/topstep-bot.service" /etc/systemd/system/topstep-bot.service
sudo sed -i "s#/home/ubuntu/Topstep-bot#$REPO_DIR#g; s#User=ubuntu#User=$(whoami)#g" /etc/systemd/system/topstep-bot.service
sudo systemctl daemon-reload
sudo systemctl enable topstep-bot

cat <<EOF

==> Bootstrap done. Remaining manual steps:
  1. Copy secrets:   scp .env $(whoami)@<this-vm-ip>:$REPO_DIR/.env
  2. Sanity check:   ollama run qwen2.5:14b "reply with OK"
  3. Start the bot:  sudo systemctl start topstep-bot
  4. Tail logs:      journalctl -u topstep-bot -f
  5. Dashboard (never exposed publicly — tunnel it):
       ssh -L 8790:localhost:8790 $(whoami)@<this-vm-ip>
       open http://localhost:8790
EOF
