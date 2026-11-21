# Claude runs this before delivering (needs Chrome + playwright, so Publish Farm Apps does not run it).
"""Every admin page: round buttons / checkboxes keep their size, and no label's words spill out of its box."""
import os, sys, threading
os.environ.update(FARM_TEST_MODE="1", MAIL_PROVIDER="memory", PLAYDATE_DB="/tmp/shotdata/playdates.db", FARM_DATA_DIR="/tmp/shotdata", ADMIN_PASSWORD="playdate")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))); sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "playdates"))
import wsgi
from werkzeug.serving import make_server
srv = make_server("127.0.0.1", 5083, wsgi.application, threaded=True); threading.Thread(target=srv.serve_forever, daemon=True).start()
from playwright.sync_api import sync_playwright
B = "http://127.0.0.1:5083"
JS = """() => {
  const bad = [];
  document.querySelectorAll('input[type=radio], input[type=checkbox]').forEach(e => {
    const r = e.getBoundingClientRect(); if (r.width && (r.width > 30 || r.width < 12)) bad.push('stretched ' + e.type + ' ' + Math.round(r.width) + 'px ' + (e.name || ''));
  });
  document.querySelectorAll('label').forEach(l => { const r = l.getBoundingClientRect(); if (r.width && l.scrollWidth > l.clientWidth + 2) bad.push('words spill out of: ' + l.innerText.trim().slice(0, 40)); });
  return bad;
}"""
pages = ["/shop/admin", "/shop/admin/products/new?kind=item", "/shop/admin/products/new?kind=pass", "/shop/admin/pages", "/shop/admin/credits",
         "/shop/admin/passes/new", "/shop/admin/settings", "/shop/admin/orders", "/shop/admin/scan",
         "/admin", "/admin/settings", "/admin/calendar", "/admin/bookings", "/admin/reports", "/account/logins", "/account/password"]
problems = 0
with sync_playwright() as p:
    br = p.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"))
    for width in (1300, 820, 390):
        pg = br.new_page(viewport={"width": width, "height": 900})
        pg.goto(B + "/shop/admin/login"); pg.fill("input[name=password]", "playdate"); pg.click("button")
        pg.goto(B + "/admin/login"); pg.fill("input[name=password]", "playdate"); pg.click("button")
        for u in pages:
            pg.goto(B + u)
            bad = pg.evaluate(JS)
            if bad:
                problems += 1; print(width, u, bad[:4])
        pg.close()
    br.close()
srv.shutdown()
print("pages with problems:", problems)
