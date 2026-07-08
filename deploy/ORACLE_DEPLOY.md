# Deploying to Oracle Cloud Free Tier (24/7, $0/mo compute)

Replaces the local launchd service (`com.topstep.trading`, port 8790) with a
systemd service on an Always-Free Oracle ARM VM, so the bot runs whether or
not your laptop is on. Same Ollama backend (`qwen2.5:14b`), no code changes.

Ignore `DEPLOY.md` / `railway.toml` / `Procfile` at the repo root — those are
stale, from an earlier Polymarket-bot iteration of this repo, not this bot.

---

## 1. Create the Oracle Cloud account

cloud.oracle.com → sign up for Free Tier. Requires a card for identity
verification; Always Free resources are never billed as long as you stay
within the always-free shapes/limits.

## 2. Provision the VM

Compute → Instances → Create Instance:

- **Image:** Canonical Ubuntu 22.04 (aarch64/arm64)
- **Shape:** `VM.Standard.A1.Flex` — set to the max Always Free allotment,
  4 OCPU / 24GB RAM (enough headroom for qwen2.5:14b + the bot)
- **Networking:** new VCN is fine, keep a public IP
- **SSH keys:** add your public key (or let Oracle generate one and download it)

Always Free ARM capacity is sometimes exhausted in a given region/AD — if
instance creation fails with an out-of-capacity error, retry, or try a
different Availability Domain.

## 3. Lock down the security list / NSG

By default Oracle opens port 22. Edit the VCN's security list (or use an NSG)
so ingress on 22 is restricted to your home/office IP, not `0.0.0.0/0`. Do
**not** open 8790 (dashboard) publicly — access it over an SSH tunnel
(step 6).

## 4. Get the repo + secrets onto the box

```bash
ssh ubuntu@<vm-ip>
git clone <your-repo-url> ~/Topstep-bot   # HTTPS + fine-grained PAT, or public if applicable
exit

# from your laptop — .env is gitignored, copy it directly, never via git:
scp ~/Topstep-bot/.env ubuntu@<vm-ip>:~/Topstep-bot/.env
scp ~/Topstep-bot/deploy/oracle_bootstrap.sh ubuntu@<vm-ip>:~/oracle_bootstrap.sh
```

## 5. Run the bootstrap script

```bash
ssh ubuntu@<vm-ip>
chmod +x oracle_bootstrap.sh
./oracle_bootstrap.sh
```

Installs Python 3.12, Ollama (own systemd service), pulls `qwen2.5:14b`,
creates the venv, installs `requirements.txt`, enables `ufw` (SSH-only
inbound), and installs `topstep-bot.service` (not started yet).

## 6. Verify, then start

```bash
ollama run qwen2.5:14b "reply with OK"     # confirm the model actually loads
sudo systemctl start topstep-bot
journalctl -u topstep-bot -f               # watch startup banner + first cycles
```

Expect the same startup line as local: `=== JARVIS engine starting | mode=paper | broker=sim | ... ===`

Dashboard, via tunnel only:

```bash
ssh -L 8790:localhost:8790 ubuntu@<vm-ip>
# then open http://localhost:8790 on your laptop
```

## 7. Stop running it locally

Once the cloud instance is confirmed stable (a day or two in paper mode),
unload the local launchd job so you don't have two instances trading against
the same ProjectX account:

```bash
launchctl unload ~/Library/LaunchAgents/com.topstep.trading.plist
```

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
| Disk check | `df -h` — signals.log/logs grow unbounded over time, rotate if needed |

## Known gaps (carry over from local, not solved by this migration)

- `DATABENTO_API_KEY` still flagged for rotation (see memory) — do this
  before or right after cutover, same key either way.
- No auto-deploy wired up on purpose — a trading bot restarting mid-cycle on
  every push is riskier than a manual `git pull` + restart. Revisit only if
  you want CI/CD badly enough to accept that tradeoff.
- Single point of failure: if the VM reboots, `ollama.service` and
  `topstep-bot.service` are both `enable`d so they come back on their own,
  but you won't get a push notification unless `notifier.py`'s channel is
  reachable from the VM (check its config still points somewhere you'll see).
