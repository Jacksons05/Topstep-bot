#!/usr/bin/env bash
# One-shot setup for the bot inside WSL2 Ubuntu on a Windows desktop.
# Run from inside WSL (not PowerShell):
#   chmod +x deploy/wsl_bootstrap.sh && ./deploy/wsl_bootstrap.sh
#
# Prerequisite: systemd must already be enabled in WSL (see
# deploy/WINDOWS_WSL_DEPLOY.md step 2) — this script refuses to run
# otherwise, since it installs a systemd unit.
#
# Assumes the repo is at ~/Topstep-bot inside WSL and .env has already
# been placed there (copy it directly, never via git — it's gitignored).

set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/Topstep-bot}"
PY_VERSION="3.12"

if ! grep -qi microsoft /proc/version 2>/dev/null; then
  echo "ERROR: this doesn't look like WSL. Run inside your WSL2 Ubuntu shell." >&2
  exit 1
fi

if ! (ps --no-headers -o comm 1 | grep -q systemd); then
  cat >&2 <<'EOF'
ERROR: systemd is not PID 1 in this WSL instance.
Enable it first:
  1. sudo tee -a /etc/wsl.conf <<'CONF'
     [boot]
     systemd=true
     CONF
  2. From PowerShell: wsl --shutdown
  3. Reopen your WSL terminal, then re-run this script.
EOF
  exit 1
fi

echo "==> apt update + base packages"
sudo apt-get update -y
sudo apt-get install -y software-properties-common curl git build-essential

echo "==> Python ${PY_VERSION}"
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt-get update -y
sudo apt-get install -y "python${PY_VERSION}" "python${PY_VERSION}-venv"

echo "==> Ollama (installs its own systemd service + starts on boot)"
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl enable --now ollama

if command -v nvidia-smi >/dev/null 2>&1; then
  echo "==> NVIDIA GPU detected — Ollama will use it automatically via WSL2 CUDA passthrough"
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
else
  echo "==> No NVIDIA GPU detected (or drivers not passed through) — Ollama runs on CPU"
fi

echo "==> Pulling qwen2.5:14b (several GB, be patient)"
ollama pull qwen2.5:14b

if [[ ! -d "$REPO_DIR" ]]; then
  echo "ERROR: $REPO_DIR not found. Clone or copy the repo there first, then re-run." >&2
  exit 1
fi

echo "==> Python venv + deps"
cd "$REPO_DIR"
"python${PY_VERSION}" -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo "==> Installing systemd unit"
sudo cp "$REPO_DIR/deploy/topstep-bot.service" /etc/systemd/system/topstep-bot.service
sudo sed -i "s#/home/ubuntu/Topstep-bot#$REPO_DIR#g; s#User=ubuntu#User=$(whoami)#g" /etc/systemd/system/topstep-bot.service
sudo systemctl daemon-reload
sudo systemctl enable topstep-bot

cat <<EOF

==> Bootstrap done. Remaining manual steps:
  1. Confirm .env is at $REPO_DIR/.env (copy it in if you haven't)
  2. Sanity check:   ollama run qwen2.5:14b "reply with OK"
  3. Start the bot:  sudo systemctl start topstep-bot
  4. Tail logs:      journalctl -u topstep-bot -f
  5. Dashboard: WSL2 forwards localhost automatically — from Windows,
     just open http://localhost:8790 in a browser. No tunnel needed.

Next: set up the Windows Task Scheduler entry so WSL (and this service)
comes up automatically at boot — see WINDOWS_WSL_DEPLOY.md step 4.
EOF
