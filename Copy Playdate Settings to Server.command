#!/bin/bash
# One-time (or occasional): copies the playdate settings you tuned on this Mac (wording, prices,
# hours, limits, release calendar) to the live server. Bookings are never touched.
cd "$(dirname "$0")" || exit 1
source "./tools/server-db.sh" || exit 1
echo "This replaces the live server's playdate settings with the ones on this Mac."
read -r -p "Type copy to continue: " ok
[ "$ok" = "copy" ] || { echo "Nothing changed."; read -r -p "Press Return to close."; exit 0; }
./.venv/bin/python tools/farm_data.py copy-settings data/playdates.db || echo "Copy didn't finish. Nothing was changed."
read -r -p "Press Return to close."
