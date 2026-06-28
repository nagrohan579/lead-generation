#!/bin/bash
# Run this once on the Hetzner Ubuntu server to set everything up.
# Usage: bash setup_hetzner.sh

set -e
echo "=== Lead Finder Setup for Hetzner (Ubuntu) ==="

# 1. System packages
echo "[1/5] Installing system packages..."
sudo apt-get update -q
sudo apt-get install -y python3 python3-pip git curl \
  # Chromium headless dependencies on Linux
  libglib2.0-0 libnss3 libatk1.0-0 libatk-bridge2.0-0 \
  libcups2 libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 \
  libxdamage1 libxrandr2 libgbm1 libxshmfence1 libasound2 \
  libx11-6 libx11-xcb1 libxcb1 libxext6 libxfixes3 libxi6 \
  libxrender1 libxtst6 ca-certificates fonts-liberation \
  libappindicator3-1 xdg-utils wget

# 2. Python packages
echo "[2/5] Installing Python packages..."
pip3 install playwright beautifulsoup4 requests pandas lxml --break-system-packages 2>/dev/null \
  || pip3 install playwright beautifulsoup4 requests pandas lxml

# 3. Playwright Chromium
echo "[3/5] Installing Playwright Chromium browser..."
python3 -m playwright install chromium
python3 -m playwright install-deps chromium

# 4. Verify
echo "[4/5] Verifying install..."
python3 -c "from playwright.sync_api import sync_playwright; print('Playwright OK')"
python3 -c "from bs4 import BeautifulSoup; print('BeautifulSoup OK')"
python3 -c "import requests; print('Requests OK')"

# 5. Quick smoke test
echo "[5/5] Running CLI smoke test..."
python3 cli.py status

echo ""
echo "=== Setup complete! ==="
echo "Run 'python3 cli.py status' to check current state."
echo "Run 'python3 cli.py --help' to see all commands."
