#!/bin/zsh
# JARVIS one-time service installer
# Run once from Terminal: chmod +x ~/Claude/Trading-Bot/install_jarvis_service.sh && ~/Claude/Trading-Bot/install_jarvis_service.sh

set -e
PLIST=~/Library/LaunchAgents/com.jarvis.trading.plist
SKILL=~/Library/Application\ Support/Claude/plugins/jarvis-heartbeat/SKILL.md

echo "=== JARVIS Service Installer ==="

# 1. Install LaunchAgent (crash auto-recovery)
echo "→ Installing LaunchAgent..."
cat > "$PLIST" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.jarvis.trading</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/jacksonsheehan/Claude/Trading-Bot/.venv/bin/python</string>
        <string>/Users/jacksonsheehan/Claude/Trading-Bot/run.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/jacksonsheehan/Claude/Trading-Bot</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>/Users/jacksonsheehan/Claude/Trading-Bot/jarvis-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/jacksonsheehan/Claude/Trading-Bot/jarvis-stderr.log</string>
    <key>ThrottleInterval</key>
    <integer>10</integer>
</dict>
</plist>
EOF
launchctl load "$PLIST" 2>/dev/null || true
echo "✅ LaunchAgent installed — JARVIS will auto-recover from crashes (10s delay)"

# 2. Make restart_jarvis.command executable
chmod +x ~/Claude/Trading-Bot/restart_jarvis.command 2>/dev/null || true
echo "✅ restart_jarvis.command is executable"

echo ""
echo "=== Done ==="
echo "To start JARVIS via launchd: launchctl start com.jarvis.trading"
echo "To stop it cleanly:          curl http://localhost:8787/api/stop"
echo "To restart it:               curl http://localhost:8787/api/restart"
echo "To uninstall:                launchctl unload $PLIST && rm $PLIST"
