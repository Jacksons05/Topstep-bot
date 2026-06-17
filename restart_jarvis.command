#!/bin/zsh
# Restart JARVIS via API (works even if Mac is locked — Claude uses this)
curl -s http://localhost:8787/api/restart && echo "Restart triggered" || echo "Bot not running — use start_jarvis.command"
