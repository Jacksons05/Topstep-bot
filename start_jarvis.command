#!/bin/zsh
cd "$(dirname "$0")"
echo "=== Starting JARVIS Trading Bot ==="
echo "Installing yfinance if needed..."
.venv/bin/pip install yfinance --quiet 2>/dev/null || true
echo "Launching..."
.venv/bin/python run.py
