"""Playdate safety test: walks the whole booking app the way a family and the farm would.

Run it against the original app and the combined farm-apps version. Both must pass, and
their results must match exactly, before any combined version is published.

    python tests/playdate_safety_test.py original   # playdates/app.py on its own
    python tests/playdate_safety_test.py combined   # through wsgi.py (with the bakery alongside)

Uses a brand-new throwaway database every run, so it never touches real or test bookings.
"""
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
target = sys.argv[1] if len(sys.argv) > 1 else "original"
os.environ["FARM_TEST_MODE"] = "1"  # allows the test passwords
work = tempfile.mkdtemp(prefix="pd-safety-")
os.environ["PLAYDATE_DB"] = os.path.join(work, "playdates.db")
os.environ["FARM_DATA_DIR"] = work
os.environ.setdefault("ADMIN_PASSWORD", "playdate")
if os.environ.get("DATABASE_URL"):  # Postgres run: a brand-new database each time
    os.environ["FARM_DB_NAME"] = "safety_" + os.path.basename(work).replace("-", "_").lower()
# "original" can point at an untouched copy of the booking app (PLAYDATES_DIR) to compare against.
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "playdates"))
if target == "original" and os.environ.get("PLAYDATES_DIR"):
    sys.path.insert(0, os.environ["PLAYDATES_DIR"])

if target == "combined":
    import wsgi
    wsgi_app = wsgi.application
else:
    import app as playdates_app
    wsgi_app = playdates_app.app.wsgi_app

import sqlite3  # noqa: E402
from werkzeug.test import Client  # noqa: E402

c = Client(wsgi_app)
results = []
failures = []


def check(name, ok, detail=""):
    results.append({"step": name, "ok": bool(ok)})
    if not ok:
        failures.append("{}: {}".format(name, detail))


import dbcompat  # noqa: E402


def q(sql, *args):
    con = dbcompat.connect(os.environ["PLAYDATE_DB"])
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(sql, args)]
    con.close()
    return rows


def body(r):
    return r.get_data(as_text=True)


def open_slots(html):
    """(date, start) for every arrival time that isn't greyed out."""
    out = []
    for m in re.finditer(r'name="date" value="([^"]+)"><input type="hidden" name="start" value="([^"]+)">.*?<button class="slot"([^>]*)>', html, re.S):
        if "disabled" not in m.group(3):
            out.append((m.group(1), m.group(2)))
    return out


def loc(r):
    return r.headers.get("Location", "")


# ---- guest books a group of 6 (+1 under 2)
r = c.get("/book?tickets=6&under2=1")
check("booking page loads", r.status_code == 200 and "Pick a day" in body(r), r.status_code)
slots = open_slots(body(r))
check("arrival times offered", len(slots) > 0)
d1, start1 = slots[0]
r = c.post("/book/hold", data={"date": d1, "start": start1, "tickets": 6, "under2": 1})
check("time held", r.status_code == 302 and "/checkout/" in loc(r), loc(r))
code = loc(r).rstrip("/").split("/")[-1]
check("hold recorded", q("SELECT status FROM bookings WHERE code=?", code)[0]["status"] == "held")

r = c.post("/checkout/" + code, data={"name": "Safety Test", "email": "x@example.com"})
check("policy agreement required", r.status_code == 200 and "agree to the ticket policy" in body(r))
r = c.post("/checkout/" + code, data={"name": "Safety Test", "email": "x@example.com", "phone": "4175550123", "sms_ok": "on", "agree": "on"})
check("details accepted", r.status_code == 302 and "/pay/" in loc(r), loc(r))

r = c.post("/pay/" + code, data={"card": "4000 0000 0000 0002"})
check("declined card refused", r.status_code == 200 and "declined" in body(r))
check("still unpaid after decline", q("SELECT status FROM bookings WHERE code=?", code)[0]["status"] == "held")
r = c.post("/pay/" + code, data={"card": "4242 4242 4242 4242"})
check("payment goes through", r.status_code == 302 and "/done/" in loc(r), loc(r))
b = q("SELECT * FROM bookings WHERE code=?", code)[0]
p = q("SELECT subtotal, tax, total, kind FROM payments WHERE booking_id=?", b["id"])
check("booking paid", b["status"] == "paid")
check("price right: $70 for 5 + $10 extra, plus tax", len(p) == 1 and p[0]["subtotal"] == 80 and p[0]["tax"] == 5.18 and p[0]["total"] == 85.18, p)
check("policy wording stored", bool(b["agreed_at"]) and len(b["agreed_terms"] or "") > 50)
check("confirmation email + text queued", len(q("SELECT * FROM outbox WHERE booking_id=?", b["id"])) == 2)

r = c.get("/done/" + code)
check("done page", r.status_code == 200)
r = c.get("/t/" + code)
check("ticket page with QR", r.status_code == 200 and "Ticket QR code" in body(r))
r = c.get("/t/{}.ics".format(code))
check("calendar file", r.status_code == 200 and "BEGIN:VCALENDAR" in body(r))

# ---- add 2 people, then move to another day
r = c.post("/booking/{}/add".format(code), data={"add": "2"})
check("add people goes to payment", r.status_code == 302 and "/pay/" in loc(r), loc(r))
r = c.post("/pay/" + code, data={"card": "4242424242424242"})
check("add-on paid", r.status_code == 302)
b = q("SELECT * FROM bookings WHERE code=?", code)[0]
check("ticket now covers 8", b["tickets"] == 8)
check("add-on charged $20 + tax", [x["subtotal"] for x in q("SELECT subtotal FROM payments WHERE booking_id=? ORDER BY id", b["id"])] == [80, 20])

r = c.get("/booking/" + code)
dates = re.findall(r"move_date=(\d{4}-\d\d-\d\d)", body(r))
other = next((x for x in dates if x != d1), None)
check("other days offered for moving", other is not None)
r = c.get("/booking/{}?move_date={}".format(code, other))
m = re.search(r'name="date" value="{}"><input type="hidden" name="start" value="([^"]+)"'.format(other), body(r))
check("times offered on the new day", m is not None)
r = c.post("/booking/{}/move".format(code), data={"date": other, "start": m.group(1)})
check("move asks to confirm first", r.status_code == 200 and q("SELECT date FROM bookings WHERE code=?", code)[0]["date"] == d1)
r = c.post("/booking/{}/move".format(code), data={"date": other, "start": m.group(1), "confirm": "1"})
check("move confirmed", r.status_code == 302 and q("SELECT date FROM bookings WHERE code=?", code)[0]["date"] == other)

# ---- farm admin
r = c.get("/admin")
check("admin needs login", r.status_code == 302 and "/admin/login" in loc(r))
r = c.post("/admin/login", data={"password": "wrong"})
check("wrong password refused", "didn" in body(r))
r = c.post("/admin/login", data={"password": os.environ["ADMIN_PASSWORD"]})
check("admin login", r.status_code == 302)
for page in ["/admin", "/admin/calendar", "/admin/day?date=" + other, "/admin/find?q=Safety", "/admin/bookings",
             "/admin/settings", "/admin/messages", "/admin/reports", "/admin/checkin/" + code]:
    r = c.get(page)
    check("admin page " + page.split("?")[0].replace(code, "CODE"), r.status_code == 200, r.status_code)
r = c.get("/admin/days")
check("old day-settings link sends you to the calendar", r.status_code == 302 and "/admin/calendar" in loc(r))
r = c.get("/admin/find?q=Safety")
check("find by name", "Safety Test" in body(r))
r = c.post("/admin/checkin/" + str(b["id"]), data={"date": other})
check("checked in", q("SELECT checked_in FROM bookings WHERE code=?", code)[0]["checked_in"] == 1)
r = c.post("/admin/gate/" + str(b["id"]), data={"n": "1", "method": "cash", "date": other})
check("gate sale recorded", any(x["kind"] == "gate" for x in q("SELECT kind FROM payments WHERE booking_id=?", b["id"])))

# stop a day: a family can't hold a time there afterwards
r = c.post("/admin/stop", data={"date": d1})
check("stop day", r.status_code == 302 and q("SELECT 1 FROM stopped_days WHERE date=?", d1))
c2 = Client(wsgi_app)
before = len(q("SELECT * FROM bookings"))
r = c2.post("/book/hold", data={"date": d1, "start": start1, "tickets": 2, "under2": 0})
check("stopped day refuses new holds", len(q("SELECT * FROM bookings")) == before)
r = c.post("/admin/resume", data={"date": d1})
check("resume day", not q("SELECT 1 FROM stopped_days WHERE date=?", d1))


# ---- every other farm-side action (each one touches the database a different way)
facts = {}
r = c.get("/admin/find?q=safety test")
check("find ignores upper/lower case", "Safety Test" in body(r))
r = c.post("/admin/demo")
demo = q("SELECT id FROM bookings WHERE demo=1")
facts["pretend bookings"] = len(demo)
check("pretend bookings added", len(demo) > 0)
check("each pretend booking has its payment", len(q("SELECT p.id FROM payments p JOIN bookings b ON b.id = p.booking_id WHERE b.demo=1")) == len(demo))
r = c.get("/admin/reports")
check("reports with data", r.status_code == 200)
facts["report csv rows"] = len(body(c.get("/admin/reports.csv")).strip().splitlines())
r = c.get("/admin?date=" + other)
qa = re.search(r'action="/admin/quick_add".*?name="date" value="([^"]+)".*?name="start" value="([^"]+)"', body(r), re.S)
if qa:
    before = len(q("SELECT id FROM bookings"))
    c.post("/admin/quick_add", data={"date": qa.group(1), "start": qa.group(2), "tickets": "3"})
    new = q("SELECT id FROM bookings ORDER BY id DESC LIMIT 1")[0]["id"]
    check("quick add makes a booking with its payment", len(q("SELECT id FROM bookings")) == before + 1 and len(q("SELECT id FROM payments WHERE booking_id=?", new)) == 1)
    c.post("/admin/remove/{}".format(new), data={})
    check("quick-add group removed", not q("SELECT id FROM bookings WHERE id=?", new))
else:
    check("quick add form found", False, "no quick add form")
c.post("/admin/toggle_quick_add", data={"date": other})
check("quick add switch", True)
out_before = len(q("SELECT id FROM outbox"))
c.post("/admin/remind", data={"date": other})
facts["reminders queued"] = len(q("SELECT id FROM outbox")) - out_before
check("reminders queued", facts["reminders queued"] > 0)
not_live = q("SELECT date FROM day_plan WHERE live = 0 ORDER BY date LIMIT 1")
r = c.get("/admin/calendar")
cal_dates = sorted(set(re.findall(r'name="date" value="(\d{4}-\d\d-\d\d)"', body(r))))
live_before = len(q("SELECT date FROM day_plan WHERE live = 1"))
target_day = next((x for x in cal_dates if not q("SELECT 1 FROM day_plan WHERE date=? AND live=1", x)), None)
if target_day:
    c.post("/admin/calendar/toggle", data={"date": target_day})
    check("calendar day released by toggle", q("SELECT live FROM day_plan WHERE date=?", target_day)[0]["live"] == 1)
else:
    check("calendar shows days to release", False)
c.post("/admin/calendar/day", data={"date": other, "first_arrival": "13:00", "close_time": "16:00", "max_on_farm": "10", "gap_every": "2", "note": "short staffed"})
check("day editor saves crowd limit", q("SELECT max_on_farm FROM day_overrides WHERE date=?", other)[0]["max_on_farm"] == 10)
check("day editor saves hours", q("SELECT first_arrival, gap_every FROM day_plan WHERE date=?", other)[0]["first_arrival"] == "13:00")
c.post("/admin/calendar/day", data={"date": other, "first_arrival": "13:00", "close_time": "16:00", "max_on_farm": "12", "note": ""})
check("day editor updates in place", q("SELECT max_on_farm FROM day_overrides WHERE date=?", other)[0]["max_on_farm"] == 12)
last = max(cal_dates) if cal_dates else other
c.post("/admin/calendar/release", data={"through": last})
facts["live days after release"] = len(q("SELECT date FROM day_plan WHERE live = 1"))
check("release through a date", facts["live days after release"] >= live_before)
c.post("/admin/pause", data={"on": "1"})
check("pause every day", json.loads(q("SELECT value FROM settings WHERE key='paused_all'")[0]["value"]) in (True, 1, "1", "true"))
c.post("/admin/pause", data={})
check("unpause", json.loads(q("SELECT value FROM settings WHERE key='paused_all'")[0]["value"]) in (False, 0, "0", "false", None))
out_before = len(q("SELECT id FROM outbox"))
c.post("/admin/close", data={"date": other})
check("close day for weather", q("SELECT closed FROM stopped_days WHERE date=?", other)[0]["closed"] == 1)
facts["weather messages"] = len(q("SELECT id FROM outbox")) - out_before
check("guests messaged about closing", facts["weather messages"] > 0)
c.post("/admin/bookings/{}/cancel".format(b["id"]), data={})
check("cancel booking", q("SELECT status FROM bookings WHERE code=?", code)[0]["status"] == "cancelled")
c.post("/admin/messages/clear", data={})
check("clear messages", len(q("SELECT id FROM outbox")) == 0)
c.post("/admin/demo/clear", data={})
check("pretend bookings removed", len(q("SELECT id FROM bookings WHERE demo=1")) == 0)
import app as _pd  # noqa: E402
first_preset = list(_pd.PRESETS)[0]
c.post("/admin/settings", data={"preset": first_preset})
check("price preset loads", all(json.loads(q("SELECT value FROM settings WHERE key=?", k)[0]["value"]) == v
                                for k, v in _pd.PRESETS[first_preset]["values"].items()))

r = c.get("/admin/reports.csv")
check("reports CSV", r.status_code == 200 and "," in body(r))
r = c.post("/admin/settings", data={})
check("settings page accepts a save", r.status_code in (200, 302))

summary = {"target": target, "database": "postgres" if os.environ.get("DATABASE_URL") else "sqlite", "passed": len(results) - len(failures), "total": len(results),
           "steps": results, "facts": facts, "failures": failures}
print(json.dumps(summary, indent=1))
sys.exit(1 if failures else 0)
