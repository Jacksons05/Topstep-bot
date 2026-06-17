#!/bin/bash
# Double-click launcher for the Polymarket scanner.
# Starts `run.py` in the background, logs to bot.out/bot.err, records the PID.
cd "$(dirname "$0")" || exit 1

# Don't start a second copy if one is already running.
if [ -f .pids ] && pgrep -f "run.py" >/dev/null 2>&1; then
  echo "Bot already appears to be running (run.py found). Not starting a duplicate."
  echo "Existing run.py PIDs: $(pgrep -f 'run.py' | tr '\n' ' ')"
  sleep 4
  exit 0
fi

# Prefer the project venv if usable, else fall back to system python3.
if [ -x ".venv/bin/python" ] && .venv/bin/python -c "import config" >/dev/null 2>&1; then
  PY=".venv/bin/python"
else
  PY="python3"
fi

echo "Starting bot with: $PY run.py"
nohup "$PY" run.py >> bot.out 2>> bot.err &
BOT_PID=$!
echo "bot pid $BOT_PID" > .pids
echo "Started. PID $BOT_PID. Logging to bot.out / bot.err."
echo "You can close this window."
sleep 4
