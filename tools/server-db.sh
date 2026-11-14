# Shared by the Backup and Copy Settings files: finds the live database address and the add-ons they need.
[ -x .venv/bin/python ] || { echo "Run Start Farm Apps once first."; read -r -p "Press Return to close."; return 1; }
./.venv/bin/pip install -q "psycopg[binary]" >/dev/null 2>&1 || { echo "Couldn't install the database add-on (offline?)."; read -r -p "Press Return to close."; return 1; }
if [ ! -s .server-db-url ]; then
  echo "Paste the External Database URL from Render (Pasture Keeper's database > Connect > External)."
  echo "It's saved only on this Mac, in a hidden file in this folder."
  read -r -p "> " url
  [ -n "$url" ] || { echo "No address entered."; read -r -p "Press Return to close."; return 1; }
  printf '%s\n' "$url" > .server-db-url && chmod 600 .server-db-url
fi
export DATABASE_URL="$(cat .server-db-url)"
