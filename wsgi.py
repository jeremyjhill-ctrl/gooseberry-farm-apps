"""One server, several storefronts.

- The playdate booking app (playdates/app.py) runs exactly as it does on its own. This file
  doesn't change it; it only decides which app answers each visitor.
- Mollie's bakery answers at its own web address (any host starting with "bakery.", or the
  hosts listed in BAKERY_HOSTS), and at /bakery/ on this server for local testing.

Run locally:   python wsgi.py           -> http://127.0.0.1:5050/book and /bakery/
On Render:     gunicorn wsgi:application
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("FARM_DATA_DIR") or os.path.join(ROOT, "data")
os.makedirs(DATA, exist_ok=True)
os.environ["FARM_DATA_DIR"] = DATA
os.environ.setdefault("PLAYDATE_DB", os.path.join(DATA, "playdates.db"))

# On the live server, refuse to start with test passwords or missing keys rather than run unsafely.
if os.environ.get("DATABASE_URL") and not os.environ.get("FARM_TEST_MODE"):
    missing = [name for name in ("SECRET_KEY", "BAKERY_PASSWORD", "BAKERY_SECRET_KEY") if not os.environ.get(name)]
    if os.environ.get("ADMIN_PASSWORD", "playdate") == "playdate":
        missing.append("ADMIN_PASSWORD (still the test password)")
    if missing:
        sys.exit("Not starting: set these in Render's Environment settings first: " + ", ".join(missing))

sys.path.insert(0, os.path.join(ROOT, "playdates"))
import app as playdates  # noqa: E402  (the playdate app, unchanged)
from bakery import create_app  # noqa: E402
from werkzeug.middleware.dispatcher import DispatcherMiddleware  # noqa: E402

bakery = create_app()
BAKERY_HOSTS = {h.strip().lower() for h in os.environ.get("BAKERY_HOSTS", "").split(",") if h.strip()}
by_path = DispatcherMiddleware(playdates.app.wsgi_app, {"/bakery": bakery.wsgi_app})


def application(environ, start_response):
    host = environ.get("HTTP_X_FORWARDED_HOST") or environ.get("HTTP_HOST", "")
    host = host.split(",")[0].split(":")[0].strip().lower()
    if host in BAKERY_HOSTS or host.startswith("bakery."):
        return bakery.wsgi_app(environ, start_response)
    if environ.get("PATH_INFO") == "/bakery":
        start_response("301 Moved Permanently", [("Location", environ.get("SCRIPT_NAME", "") + "/bakery/")])
        return [b""]
    return by_path(environ, start_response)


if __name__ == "__main__":
    from werkzeug.serving import run_simple
    port = int(os.environ.get("PORT", 5050))
    print("\n  Farm apps are running.")
    print("  Playdate booking:   http://127.0.0.1:{}/book".format(port))
    print("  Playdate admin:     http://127.0.0.1:{}/admin   (password: {})".format(port, playdates.ADMIN_PASSWORD))
    print("  Mollie's bakery:    http://127.0.0.1:{}/bakery/".format(port))
    print("  Bakery admin:       http://127.0.0.1:{}/bakery/#admin\n".format(port))
    run_simple("0.0.0.0", port, application, use_reloader=True)
