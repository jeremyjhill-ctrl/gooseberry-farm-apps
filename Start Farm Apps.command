#!/bin/bash
# Double-click to run the farm apps on this Mac for testing: playdate booking, the farm shop + Mollie's bakery.
# Test mode only: no real cards, emails or texts. Leave this window open while you use it.
cd "$(dirname "$0")" || exit 1
if [ ! -x ".venv/bin/python" ]; then
  echo "First run: setting things up (about a minute)..."
  if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 isn't installed. Install it from https://www.python.org/downloads/ and try again."
    read -r -p "Press Return to close."; exit 1
  fi
  python3 -m venv .venv && ./.venv/bin/pip install -q --upgrade pip && ./.venv/bin/pip install -q -r requirements-mac.txt || {
    echo "Setup didn't finish. Check your internet connection and try again."; read -r -p "Press Return to close."; exit 1; }
fi
./.venv/bin/pip install -q -r requirements-mac.txt >/dev/null 2>&1 || echo "Couldn't check for updates (offline?). Starting anyway."
mkdir -p data
# First time only: start from the playdate settings you already tuned in the Booking App test version (a copy; the original stays put).
OLD="$HOME/Desktop/Booking App/playdates/playdates.db"
if [ ! -f data/playdates.db ] && [ -f "$OLD" ]; then
  cp "$OLD" data/playdates.db && echo "Copied your playdate test data and settings from the Booking App folder."
fi
LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)
HOST=${LAN_IP:-127.0.0.1}
(sleep 2; open "http://$HOST:5050/book"; open "http://$HOST:5050/shop/admin"; open "http://$HOST:5050/bakery/") &
echo ""
if [ -n "$LAN_IP" ]; then
  echo "On your phone (same Wi-Fi as this Mac):"
  echo "  Playdates:  http://$LAN_IP:5050/book      (admin: /admin)"
  echo "  Farm shop:  http://$LAN_IP:5050/shop/     (admin: /shop/admin, same password as playdates)"
  echo "  Bakery:     http://$LAN_IP:5050/bakery/   (admin: tap Baker login at the bottom)"
  echo "(If your Mac asks whether to allow incoming connections for Python, click Allow.)"
fi
echo "Leave this window open while you use the apps. Close it (or press Control-C) to stop."
./.venv/bin/python wsgi.py
