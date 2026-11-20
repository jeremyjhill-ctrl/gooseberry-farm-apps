#!/bin/bash
# Clears test playdate bookings, farm shop orders and passes, and bakery orders on this Mac so you can play again.
# Playdate settings, wording, prices, hours and calendar are kept, and so are the shop's products,
# credits, sale pages and settings, and Mollie's menu.
# A backup copy of everything is saved first. Doesn't touch the live site.
cd "$(dirname "$0")/data" 2>/dev/null || { echo "Nothing to reset."; read -r -p "Press Return to close."; exit 0; }
stamp=$(date +%Y%m%d-%H%M%S)
mkdir -p old-test-data
for f in playdates.db bakery.db; do [ -f "$f" ] && cp "$f" "old-test-data/${f%.db}-$stamp.db"; done
echo "Saved backup copies in data/old-test-data/ ($stamp)."
echo
read -r -p "Also put every playdate setting back to the original defaults? Type yes, or just press Return to keep them: " answer
PY=python3; [ -x ../.venv/bin/python ] && PY=../.venv/bin/python
run_sql() { "$PY" -c 'import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.executescript(sys.argv[2]); c.commit()' "$1" "$2"; }
if [ -f playdates.db ]; then
  run_sql playdates.db "BEGIN; DELETE FROM payments; DELETE FROM bookings; DELETE FROM outbox; DELETE FROM stopped_days;
  UPDATE settings SET value='false' WHERE key='paused_all'; COMMIT;" || { echo "Couldn't clear playdate test data."; read -r -p "Press Return to close."; exit 1; }
  "$PY" -c 'import sqlite3; c = sqlite3.connect("playdates.db")
have = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
for t in ["shop_pass_uses", "shop_pass_holds", "shop_pass_credits", "shop_passes", "shop_order_lines", "shop_orders"]:
    if t in have:
        c.execute("DELETE FROM " + t)
c.commit()' && echo "Farm shop test orders and passes cleared (products, credits, sale pages and settings kept)."
  case "$answer" in yes|YES|Yes) run_sql playdates.db "DELETE FROM settings; DELETE FROM day_plan; DELETE FROM day_overrides;"; echo "Playdate settings are back to the defaults.";; esac
fi
if [ -f bakery.db ]; then
  "$PY" -c 'import sqlite3, json; c = sqlite3.connect("bakery.db"); r = c.execute("SELECT data FROM bakery_store WHERE id=1").fetchone()
if r:
    s = json.loads(r[0]); s["orders"] = []; s["nextOrderNumber"] = 1001
    c.execute("UPDATE bakery_store SET data=? WHERE id=1", (json.dumps(s),)); c.commit()' && echo "Bakery test orders cleared (menu and settings kept)."
fi
echo "Done. Restart Start Farm Apps if it's running."
read -r -p "Press Return to close."
