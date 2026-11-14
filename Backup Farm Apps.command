#!/bin/bash
# Copies everything on the live farm apps server (bookings, orders, settings) into backups/ on this Mac,
# as ordinary database files the apps can run on. Photos stay in the R2 bucket, which keeps its own copies.
cd "$(dirname "$0")" || exit 1
source "./tools/server-db.sh" || exit 1
OUT=$(./.venv/bin/python tools/farm_data.py backup | tee /dev/tty | tail -1)
if [ -d "$OUT" ]; then
  echo
  echo "A backup nobody has opened is a guess. Open this one in the apps now to look it over?"
  read -r -p "Type y to open it (at http://127.0.0.1:5051/admin and /bakery/), or press Return to skip: " look
  if [ "$look" = "y" ] || [ "$look" = "Y" ]; then
    (sleep 2; open "http://127.0.0.1:5051/admin"; open "http://127.0.0.1:5051/bakery/#admin") &
    echo "Close this window (or press Control-C) when you're done looking. Nothing on the live site changes."
    env -u DATABASE_URL -u PLAYDATE_DB FARM_DATA_DIR="$OUT" PORT=5051 ./.venv/bin/python wsgi.py
  fi
else
  echo "Backup didn't finish."
fi
read -r -p "Press Return to close."
