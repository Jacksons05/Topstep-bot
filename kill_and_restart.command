#!/bin/zsh
# Kill the running JARVIS process, clear KILL_SWITCH, and start fresh with new code.
# Double-click from Finder to run, or: bash ~/Claude/Trading-Bot/kill_and_restart.command

cd "$(dirname "$0")"

echo "=== JARVIS Kill & Restart ==="
echo "Stopping old process..."
pkill -f "python.*run\.py" 2>/dev/null && echo "Old process killed." || echo "(No old process found)"
sleep 2

echo "Clearing KILL_SWITCH..."
rm -f KILL_SWITCH && echo "KILL_SWITCH removed." || echo "(No KILL_SWITCH found)"

echo "Starting JARVIS with new code..."
exec .venv/bin/python run.py
