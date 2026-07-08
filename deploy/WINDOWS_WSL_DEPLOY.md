# Deploying to a Windows desktop (WSL2, 24/7, $0)

Runs the bot inside WSL2 Ubuntu on your desktop instead of a cloud VM — same
Linux tooling as the Oracle plan (`deploy/ORACLE_DEPLOY.md`), no code
changes, and if the desktop has an NVIDIA GPU, Ollama uses it automatically
(much faster than CPU-only inference).

Ignore `DEPLOY.md` / `railway.toml` / `Procfile` at the repo root — stale,
from an earlier Polymarket-bot iteration of this repo.

---

## 1. Stop the desktop from sleeping

Bot dies the moment Windows sleeps. In an **elevated** PowerShell:

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

(Monitor can still sleep — `standby`/`hibernate` are what matter.) Also
check Settings → System → Power & battery → Screen and sleep, confirm
"Sleep" is set to Never for plugged-in.

## 2. Install WSL2 + enable systemd

PowerShell (elevated):

```powershell
wsl --install -d Ubuntu-22.04
```

Reboot if prompted, then open the Ubuntu app once to finish first-run setup
(create a Unix username/password). Then enable systemd, which the bot's
service file needs:

```bash
# inside the WSL Ubuntu shell
sudo tee -a /etc/wsl.conf <<'EOF'
[boot]
systemd=true
EOF
```

```powershell
# back in PowerShell
wsl --shutdown
```

Reopen the WSL terminal — `systemctl` should now work (`systemctl status`
should show systemd as PID 1, not error out). Needs a reasonably recent WSL
(`wsl --version` — update via `wsl --update` if `systemd=true` doesn't take).

## 3. Get the repo + secrets into WSL

From inside the WSL shell, your Windows files are visible under `/mnt/c/...`,
but run the bot from the native WSL filesystem (`~/Topstep-bot`), not
`/mnt/c` — much faster I/O, avoids Windows/Linux file-lock weirdness.

```bash
git clone <your-repo-url> ~/Topstep-bot
cp /mnt/c/path/to/your/.env ~/Topstep-bot/.env   # however you get .env onto this machine
chmod +x ~/Topstep-bot/deploy/wsl_bootstrap.sh
~/Topstep-bot/deploy/wsl_bootstrap.sh
```

The script installs Python 3.12, Ollama, pulls `qwen2.5:14b`, sets up the
venv, and installs (but doesn't start) `topstep-bot.service`.

## 4. Verify, then start

```bash
ollama run qwen2.5:14b "reply with OK"
sudo systemctl start topstep-bot
journalctl -u topstep-bot -f
```

Dashboard: WSL2 forwards localhost to Windows automatically — open
`http://localhost:8790` directly in a browser on the desktop. From another
machine on your LAN, you'd need `netsh interface portproxy` on the Windows
side; skip that unless you actually need remote access.

## 5. Make WSL (and the bot) survive a Windows reboot

WSL2 distros don't auto-start at boot on their own — something has to invoke
`wsl.exe` once. Use Task Scheduler:

1. Open **Task Scheduler** → Create Task (not "Basic Task" — need the
   "run whether user is logged on or not" option)
2. **General:** name it `Start WSL Topstep`, select "Run whether user is
   logged on or not", check "Run with highest privileges"
3. **Triggers:** New → "At startup"
4. **Actions:** New → Program: `wsl.exe`, arguments:
   `-d Ubuntu-22.04 -u <your-wsl-username> -- true`
   (just needs to touch WSL once — systemd inside then starts
   `topstep-bot.service` on its own since it's `enable`d)
5. **Settings:** uncheck "Stop the task if it runs longer than", leave
   everything else default

Reboot the desktop once to confirm: after boot, wait ~30s, then
`http://localhost:8790` should be up without you opening a terminal.

## 6. Cut over from the laptop

Once confirmed stable for a day or two in paper mode:

```bash
launchctl unload ~/Library/LaunchAgents/com.topstep.trading.plist
```

so you're not running two instances against the same ProjectX account.

---

## Day-2 ops

| Task | Command |
|---|---|
| Restart bot | `sudo systemctl restart topstep-bot` |
| Stop bot | `sudo systemctl stop topstep-bot` |
| Logs (live) | `journalctl -u topstep-bot -f` |
| Logs (file) | `tail -f ~/Topstep-bot/topstep-std{out,err}.log` |
| Deploy new code | `cd ~/Topstep-bot && git pull && sudo systemctl restart topstep-bot` |
| Ollama status | `sudo systemctl status ollama` |
| GPU check | `nvidia-smi` (inside WSL) |

## Known gaps (same as the Oracle plan)

- `DATABENTO_API_KEY` still flagged for rotation.
- No auto-deploy on push, on purpose — `git pull` + restart manually.
- Windows Update reboots can still interrupt the bot even with sleep
  disabled — the Task Scheduler job brings it back up, but you'll miss
  whatever window the reboot happened in. Worth checking Windows Update's
  active-hours setting so forced reboots don't land during market hours.
