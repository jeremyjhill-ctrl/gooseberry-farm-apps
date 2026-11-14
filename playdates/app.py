"""Farm Baby Playdates - booking app (local test version).

Run with:  python app.py   then open http://127.0.0.1:5050
Admin:     http://127.0.0.1:5050/admin   (password: playdate, or set ADMIN_PASSWORD)

Payments are a TEST STAND-IN for Stripe. No real cards are charged.
"""
import csv
import datetime as dt
import io
import json
import os
import random
import re
import secrets
import sqlite3
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)

import dbcompat  # same SQLite file on the Mac; Postgres on Render (see dbcompat.py)
import logic

try:  # QR codes on tickets; the app still works without them (it just shows the code)
    import qrcode
    import qrcode.image.svg
except ImportError:  # pragma: no cover
    qrcode = None


def qr_svg(data):
    if not qrcode:
        return None
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    svg = img.to_string(encoding="unicode")
    return svg.replace("<svg ", '<svg role="img" aria-label="Ticket QR code" ', 1)
from logic import DEFAULTS, PRESETS, money, nice_date, nice_time, window_label

TZ = ZoneInfo("America/Chicago")
BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PLAYDATE_DB", os.path.join(BASE, "playdates.db"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "playdate")

TEST_CARD_OK = "4242424242424242"
MAX_HOLDS_PER_VISITOR = 3  # unpaid holds from one internet connection at a time (stops bots parking on times)
LOGIN_ATTEMPTS = {}  # ip -> list of recent failed-attempt times
TEST_CARD_DECLINE = "4000000000000002"

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.secret_key = os.environ.get("SECRET_KEY", "local-test-only-change-me")
app.jinja_env.globals.update(money=money, nice_date=nice_date, nice_time=nice_time)

GSM = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")


def text_segments(body):
    """How many billable pieces a text splits into. One fancy character (curly quote, dash, emoji) cuts the limit from 160 to 70."""
    plain = all(ch in GSM for ch in body)
    single, multi = (160, 153) if plain else (70, 67)
    n = len(body)
    return {"chars": n, "segments": 1 if n <= single else -(-n // multi), "plain": plain}


app.jinja_env.globals.update(text_segments=text_segments)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS day_overrides (date TEXT PRIMARY KEY, max_on_farm INTEGER NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS stopped_days (date TEXT PRIMARY KEY, stopped_at TEXT NOT NULL, closed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS bookings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE NOT NULL,
  date TEXT NOT NULL,
  window_start TEXT NOT NULL,
  tickets INTEGER NOT NULL,
  under2 INTEGER DEFAULT 0,
  name TEXT, email TEXT, phone TEXT,
  sms_ok INTEGER DEFAULT 1,
  status TEXT NOT NULL,              -- held, paid, expired, released, cancelled
  hold_expires TEXT,
  created_at TEXT NOT NULL,
  paid_at TEXT,
  checked_in INTEGER DEFAULT 0,
  weather_moved INTEGER DEFAULT 0,
  demo INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  booking_id INTEGER NOT NULL,
  kind TEXT NOT NULL,                -- online, add_on, gate
  tickets INTEGER NOT NULL,
  subtotal REAL NOT NULL, tax REAL NOT NULL, total REAL NOT NULL,
  method TEXT NOT NULL,              -- test_card, cash, card_at_gate
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  booking_id INTEGER,
  channel TEXT NOT NULL, recipient TEXT, subject TEXT, body TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS day_plan (
  date TEXT PRIMARY KEY,
  live INTEGER DEFAULT 0,            -- 1 = released: guests can see and book it
  first_arrival TEXT, close_time TEXT, -- this day's own hours (blank = use the weekday's hours)
  note TEXT, released_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_bookings_date ON bookings(date, status);
"""


# ---------------------------------------------------------------- database

def db():
    if "db" not in g:
        g.db = dbcompat.connect(DB_PATH, timeout=10, isolation_level=None)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = dbcompat.connect(DB_PATH)
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bookings)")}
    for col in ("agreed_at", "agreed_terms", "checked_in_at", "hold_ip"):
        if col not in cols:
            conn.execute("ALTER TABLE bookings ADD COLUMN {} TEXT".format(col))
    if "gap_every" not in {r[1] for r in conn.execute("PRAGMA table_info(day_plan)")}:
        conn.execute("ALTER TABLE day_plan ADD COLUMN gap_every INTEGER")  # this day's own break pattern (blank = usual)
    for k, v in DEFAULTS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, json.dumps(v)))
    if not conn.execute("SELECT 1 FROM settings WHERE key='migrated_day_plan'").fetchone():
        # Release calendar added: start the test copy with the first two weeks of the season released.
        s0 = {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM settings")}
        day = dt.date.fromisoformat(s0.get("season_start", DEFAULTS["season_start"]))
        for _ in range(14):
            if logic.WEEKDAYS[day.weekday()] in s0.get("open_days", DEFAULTS["open_days"]):
                conn.execute("INSERT OR IGNORE INTO day_plan(date, live, released_at) VALUES (?, 1, ?)",
                             (day.isoformat(), dt.datetime.now(TZ).isoformat()))
            day += dt.timedelta(days=1)
        conn.execute("INSERT INTO settings(key, value) VALUES ('migrated_day_plan', 'true')")
    if not conn.execute("SELECT 1 FROM settings WHERE key='migrated_cutoff_48'").fetchone():
        # Nov 2026: reschedule cutoff moved from 24 to 48 hours so there's time to refill an open spot
        conn.execute("UPDATE settings SET value='48' WHERE key='reschedule_cutoff_hours' AND value='24'")
        conn.execute("INSERT INTO settings(key, value) VALUES ('migrated_cutoff_48', 'true')")
    row = conn.execute("SELECT value FROM settings WHERE key='policy_text'").fetchone()
    if row and json.loads(row[0]).startswith("Everyone age 2 and up who comes onto the farm needs a ticket,"):
        # earlier wording you hadn't edited yet: switch it to the one-ticket-per-group wording
        conn.execute("UPDATE settings SET value=? WHERE key='policy_text'", (json.dumps(DEFAULTS["policy_text"]),))
    conn.commit()
    conn.close()


def get_settings():
    s = dict(DEFAULTS)
    for row in db().execute("SELECT key, value FROM settings"):
        s[row["key"]] = json.loads(row["value"])
    return s


def save_setting(key, value):
    db().execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, json.dumps(value)))


def now():
    return dt.datetime.now(TZ).replace(microsecond=0)


def today():
    return now().date()


def stopped_dates():
    return {r["date"]: dict(r) for r in db().execute("SELECT * FROM stopped_days")}


def day_plan():
    return {r["date"]: dict(r) for r in db().execute("SELECT * FROM day_plan")}


def live_dates():
    return {r["date"] for r in db().execute("SELECT date FROM day_plan WHERE live = 1")}


def sfor(d, s):
    """Settings for one date, with that day's own opening hours and break pattern filled in."""
    p = day_plan().get(d)
    start, end, _ = logic.hours_for(d, s, p)
    out = dict(s, first_arrival=start, close_time=end)
    if p and p.get("gap_every") is not None:
        out["gap_every"] = int(p["gap_every"])
    return out


def cap_for(d, s):
    row = db().execute("SELECT max_on_farm FROM day_overrides WHERE date = ?", (d,)).fetchone()
    return int(row["max_on_farm"]) if row else int(s["max_on_farm"])


def active_bookings(d, exclude_id=None):
    """Bookings that take up room: paid ones, plus holds that haven't run out."""
    rows = db().execute(
        "SELECT * FROM bookings WHERE date = ? AND (status = 'paid' OR (status = 'held' AND hold_expires > ?))",
        (d, now().isoformat())).fetchall()
    return [dict(r) for r in rows if r["id"] != exclude_id]


def expire_holds():
    db().execute("UPDATE bookings SET status = 'expired' WHERE status = 'held' AND hold_expires <= ?", (now().isoformat(),))


def booking_by_code(code):
    row = db().execute("SELECT * FROM bookings WHERE code = ?", (code,)).fetchone()
    if not row:
        abort(404)
    return dict(row)


def paid_so_far(booking_id):
    row = db().execute("SELECT COALESCE(SUM(subtotal),0) AS sub, COALESCE(SUM(tax),0) AS tax, COALESCE(SUM(total),0) AS total "
                       "FROM payments WHERE booking_id = ?", (booking_id,)).fetchone()
    return dict(row)


def send(booking, channel, subject, body):
    """Stand-in for email and text messages: everything lands in Admin > Messages."""
    recipient = booking.get("email") if channel == "email" else booking.get("phone")
    if channel == "text" and not booking.get("sms_ok"):
        return
    db().execute("INSERT INTO outbox(booking_id, channel, recipient, subject, body, created_at) VALUES (?,?,?,?,?,?)",
                 (booking["id"], channel, recipient, subject, body, now().isoformat()))


def ticket_link(code):
    return url_for("ticket", code=code, _external=True)


def text_window(b, s):
    """Plain-ASCII date and window for text messages (fancy characters make a text cost double)."""
    start = b["window_start"]
    end = logic.from_min(logic.to_min(start) + int(s["window_minutes"]))
    return "{} {}-{}".format(nice_date(b["date"]), nice_time(start), nice_time(end)).replace("\u2013", "-")


def describe(b, s):
    t = "1 ticket for {} {}".format(b["tickets"], "person" if b["tickets"] == 1 else "people")
    if b.get("under2"):
        t += " + {} under 2".format(b["under2"])
    return t


@app.before_request
def housekeeping():
    expire_holds()


@app.context_processor
def inject():
    return {"S": get_settings(), "window_label": lambda start: window_label(start, get_settings())}


# ---------------------------------------------------------------- guest: book

@app.route("/")
def home():
    return redirect(url_for("book"))


@app.route("/book")
def book():
    s = get_settings()
    stopped = stopped_dates()
    biggest = max(1, int(s.get("max_group_size") or 25))
    tickets = max(1, min(biggest + 1, request.args.get("tickets", 4, type=int)))
    too_big = tickets > biggest  # one past the limit: show the "contact us" message instead of times
    under2 = max(0, min(10, request.args.get("under2", 0, type=int)))
    live = live_dates()
    dates = logic.open_dates(s, stopped, today(), live)
    chosen = request.args.get("date") or next((d["date"] for d in dates if d["open"]), None)
    hours, closed_reason = [], ""
    if chosen:
        ok, closed_reason = logic.day_status(chosen, s, stopped, today(), live)
        if ok and not too_big:
            hours = logic.slot_options(tickets, active_bookings(chosen), sfor(chosen, s), cap_for(chosen, s))
    lines, sub = logic.price_lines(tickets, s)
    tax, total = logic.tax_for(sub, s)
    return render_template("book.html", dates=dates, chosen=chosen, tickets=tickets, under2=under2,
                           hours=hours, closed_reason=closed_reason, lines=lines, sub=sub, tax=tax, total=total,
                           rule=logic.rule_text(s), too_big=too_big, biggest=biggest,
                           big_message=s["big_group_message"].replace("{max}", str(biggest)))


@app.post("/book/hold")
def hold():
    s = get_settings()
    d = request.form["date"]
    start = request.form["start"]
    tickets = max(1, int(request.form.get("tickets", 1)))
    under2 = max(0, int(request.form.get("under2", 0)))
    biggest = max(1, int(s.get("max_group_size") or 25))
    if tickets > biggest:
        return render_template("message.html", title="Let's talk first",
                               body=s["big_group_message"].replace("{max}", str(biggest)),
                               link=url_for("book", tickets=biggest), link_text="Back to booking"), 400
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    conn = db()
    conn.execute("BEGIN IMMEDIATE")  # one booking at a time, so two people can't grab the same room
    try:
        # Picking a new time replaces this browser's earlier unpaid hold, so going "back" never leaves a stray hold behind.
        mine = session.get("holds", [])
        if mine:
            conn.execute("UPDATE bookings SET status='released' WHERE status='held' AND code IN ({})".format(",".join("?" * len(mine))), mine)
        holding = conn.execute("SELECT COUNT(*) FROM bookings WHERE status='held' AND hold_expires > ? AND hold_ip = ?",
                               (now().isoformat(), ip)).fetchone()[0]
        if holding >= MAX_HOLDS_PER_VISITOR:
            # Stops one person (or a bot) from parking on a whole day's times without paying.
            conn.execute("ROLLBACK")
            flash("There are already {} unpaid holds from your internet connection. Please finish checking out, or wait a few minutes for them to expire.".format(holding))
            return redirect(url_for("book", date=d, tickets=tickets, under2=under2))
        ok, why = logic.day_status(d, s, stopped_dates(), today(), live_dates())
        if ok:
            ok, why = logic.fits(start, tickets, active_bookings(d), sfor(d, s), cap_for(d, s))
        if not ok:
            conn.execute("ROLLBACK")
            flash("Sorry, that time was just taken. Please pick another. ({})".format(why))
            return redirect(url_for("book", date=d, tickets=tickets, under2=under2))
        code = "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(8))
        conn.execute(
            "INSERT INTO bookings(code, date, window_start, tickets, under2, status, hold_expires, created_at, hold_ip) "
            "VALUES (?,?,?,?,?, 'held', ?, ?, ?)",
            (code, d, start, tickets, under2, (now() + dt.timedelta(minutes=int(s["hold_minutes"]))).isoformat(),
             now().isoformat(), ip))
        conn.execute("COMMIT")
        session["holds"] = [code]
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return redirect(url_for("checkout", code=code))


@app.route("/checkout/<code>", methods=["GET", "POST"])
def checkout(code):
    s = get_settings()
    b = booking_by_code(code)
    if b["status"] == "paid":
        return redirect(url_for("done", code=code))
    if b["status"] != "held":
        return render_template("message.html", title="Your hold ran out",
                               body="We held that time for {} minutes, and it's been released so others can book. "
                                    "Please pick a time again.".format(s["hold_minutes"]),
                               link=url_for("book", date=b["date"], tickets=b["tickets"]), link_text="Pick a time")
    errors = []
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        sms_ok = 1 if request.form.get("sms_ok") else 0
        if not name: errors.append("Please enter your name.")
        if "@" not in email: errors.append("Please enter a valid email so we can send your tickets.")
        if not request.form.get("agree"): errors.append("Please agree to the ticket policy.")
        if not errors:
            db().execute("UPDATE bookings SET name=?, email=?, phone=?, sms_ok=?, agreed_at=?, agreed_terms=? WHERE id=?",
                         (name, email, phone, sms_ok, now().isoformat(), "\n\n".join(logic.policy_paragraphs(s)), b["id"]))
            return redirect(url_for("pay", code=code))
        b.update(name=name, email=email, phone=phone, sms_ok=sms_ok)
    lines, sub = logic.price_lines(b["tickets"], s)
    tax, total = logic.tax_for(sub, s)
    expires = dt.datetime.fromisoformat(b["hold_expires"])
    return render_template("checkout.html", b=b, lines=lines, sub=sub, tax=tax, total=total, errors=errors,
                           policy=logic.policy_paragraphs(s),
                           seconds_left=max(0, int((expires - now()).total_seconds())))


@app.route("/pay/<code>", methods=["GET", "POST"])
def pay(code):
    """TEST STAND-IN FOR STRIPE CHECKOUT."""
    s = get_settings()
    b = booking_by_code(code)
    add = session.get("add_on", {}).get(code) if b["status"] == "paid" else None
    if b["status"] == "paid" and not add:
        return redirect(url_for("done", code=code))
    if b["status"] not in ("held", "paid"):
        return render_template("message.html", title="This time is no longer available",
                               body="Your hold was released before payment finished, so you weren't charged. Please pick a time again.",
                               link=url_for("book", date=b["date"], tickets=b["tickets"]), link_text="Pick a time")

    if add:
        new_tickets = b["tickets"] + add
        sub = round(logic.subtotal_for(new_tickets, s) - paid_so_far(b["id"])["sub"], 2)
        sub = max(sub, 0.0)
        label = "Add {} {} to your ticket".format(add, "person" if add == 1 else "people")
    else:
        sub = logic.subtotal_for(b["tickets"], s)
        label = "Farm Baby Playdate · {} · {}".format(nice_date(b["date"]), describe(b, s))
    tax, total = logic.tax_for(sub, s)

    error = None
    if request.method == "POST":
        card = "".join(ch for ch in request.form.get("card", "") if ch.isdigit())
        conn = db()
        conn.execute("BEGIN IMMEDIATE")
        fresh = dict(conn.execute("SELECT * FROM bookings WHERE id=?", (b["id"],)).fetchone())
        still_ok = fresh["status"] == "paid" if add else (fresh["status"] == "held" and fresh["hold_expires"] > now().isoformat())
        if add and still_ok:
            still_ok, _ = logic.fits(fresh["window_start"], new_tickets, active_bookings(fresh["date"], exclude_id=fresh["id"]),
                                     sfor(fresh["date"], s), cap_for(fresh["date"], s))
        if not still_ok:
            conn.execute("ROLLBACK")
            return render_template("message.html", title="This time is no longer available",
                                   body="Sorry, the farm stopped taking bookings for that time, or your hold ran out. You weren't charged.",
                                   link=url_for("book"), link_text="Pick another time")
        if card == TEST_CARD_DECLINE:
            conn.execute("ROLLBACK")
            error = "Your card was declined. (Test decline card.) Try 4242 4242 4242 4242."
        elif card != TEST_CARD_OK:
            conn.execute("ROLLBACK")
            error = "Test mode: use card 4242 4242 4242 4242 (any future date, any 3 digits)."
        else:
            stamp = now().isoformat()
            if add:
                conn.execute("UPDATE bookings SET tickets = ? WHERE id = ?", (new_tickets, b["id"]))
                kind, count = "add_on", add
            else:
                conn.execute("UPDATE bookings SET status='paid', paid_at=?, hold_expires=NULL WHERE id=?", (stamp, b["id"]))
                kind, count = "online", b["tickets"]
            conn.execute("INSERT INTO payments(booking_id, kind, tickets, subtotal, tax, total, method, created_at) "
                         "VALUES (?,?,?,?,?,?, 'test_card', ?)", (b["id"], kind, count, sub, tax, total, stamp))
            conn.execute("COMMIT")
            b = booking_by_code(code)
            when = "{}, arrival window {}".format(nice_date(b["date"], long=True), window_label(b["window_start"], s))
            manage = url_for("manage", code=code, _external=True)
            if add:
                session.get("add_on", {}).pop(code, None); session.modified = True
                send(b, "email", "People added to your Farm Baby Playdate ticket",
                     "You added {} and now have {}. Paid {}.\nYour ticket: {}".format(add, describe(b, s), money(total), ticket_link(code)))
                send(b, "text", None, "Gooseberry Bridge Farm: Your ticket now covers {}. Your ticket: {}".format(b["tickets"], ticket_link(code)))
            else:
                send(b, "email", "Your Farm Baby Playdate tickets",
                     "Hi {}! You're booked for {}, arrival window {}.\n{} · Paid {} (incl. {} tax).\n\n"
                     "Your ticket (one for your whole group), arrival details and the ticket policy you agreed to are here:\n{}\n\n"
                     "Show that page at the gate.".format(b["name"], nice_date(b["date"], long=True), window_label(b["window_start"], s),
                                                          describe(b, s), money(total), money(tax), ticket_link(code)))
                send(b, "text", None, "Gooseberry Bridge Farm: You're booked! Your group ticket & arrival details: {} Reply STOP to opt out".format(ticket_link(code)))
            return redirect(url_for("done", code=code))
    return render_template("pay.html", b=b, label=label, sub=sub, tax=tax, total=total, error=error, add=add)


@app.route("/done/<code>")
def done(code):
    s = get_settings()
    b = booking_by_code(code)
    if b["status"] != "paid":
        return redirect(url_for("checkout", code=code))
    return render_template("done.html", b=b, paid=paid_so_far(b["id"]), desc=describe(b, s))


@app.route("/t/<code>")
def ticket(code):
    """The page every text and email links to: tickets, arrival window, directions, and the terms they agreed to."""
    s = get_settings()
    b = booking_by_code(code)
    if b["status"] != "paid":
        return render_template("message.html", title="This ticket isn't active",
                               body="This booking was {}. If that's a surprise, please call or text the farm.".format(b["status"]),
                               link=url_for("book"), link_text="Book a playdate")
    agreed = dt.datetime.fromisoformat(b["agreed_at"]).strftime("%B %-d, %Y at %-I:%M %p") if b.get("agreed_at") else None
    terms = [p for p in (b.get("agreed_terms") or "").split("\n\n") if p.strip()] or logic.policy_paragraphs(s)
    start_dt = dt.datetime.fromisoformat(b["date"] + "T" + b["window_start"]).replace(tzinfo=TZ)
    can_change = bool(b["weather_moved"]) or (start_dt - now()) >= dt.timedelta(hours=int(s["reschedule_cutoff_hours"]))
    staff_url = url_for("staff_checkin", code=code, _external=True)
    return render_template("ticket.html", b=b, desc=describe(b, s), agreed=agreed, terms=terms,
                           qr=qr_svg(staff_url), staff_url=staff_url, can_change=can_change)


@app.route("/t/<code>.ics")
def ticket_ics(code):
    s = get_settings()
    b = booking_by_code(code)
    start = dt.datetime.fromisoformat(b["date"] + "T" + b["window_start"])
    end = start + dt.timedelta(minutes=int(s["stay_minutes"]))
    fmt = "%Y%m%dT%H%M%S"
    body = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Gooseberry Bridge Farm//Playdates//EN", "BEGIN:VEVENT",
        "UID:{}@gooseberrybridge".format(b["code"]), "DTSTAMP:" + now().astimezone(dt.timezone.utc).strftime(fmt) + "Z",
        "DTSTART;TZID=America/Chicago:" + start.strftime(fmt), "DTEND;TZID=America/Chicago:" + end.strftime(fmt),
        "SUMMARY:Farm Baby Playdate (arrive {})".format(window_label(b["window_start"], s).replace("\u2013", "-")),
        "LOCATION:177 Elm Grove Rd\\, Rogersville\\, MO 65742",
        "DESCRIPTION:{}. Your ticket is good for your arrival window only. Ticket: {}".format(describe(b, s), ticket_link(code)),
        "END:VEVENT", "END:VCALENDAR", ""])
    return Response(body, mimetype="text/calendar", headers={"Content-Disposition": "attachment; filename=playdate.ics"})


@app.route("/booking/<code>")
def manage(code):
    s = get_settings()
    b = booking_by_code(code)
    if b["status"] != "paid":
        return redirect(url_for("checkout", code=code))
    start_dt = dt.datetime.fromisoformat(b["date"] + "T" + b["window_start"]).replace(tzinfo=TZ)
    can_move = b["weather_moved"] or (start_dt - now()) >= dt.timedelta(hours=int(s["reschedule_cutoff_hours"]))
    move_date = request.args.get("move_date")
    hours, dates = [], []
    if can_move:
        dates = [d for d in logic.open_dates(s, stopped_dates(), today(), live_dates()) if d["open"]]
        if move_date:
            hours = logic.slot_options(b["tickets"], active_bookings(move_date, exclude_id=b["id"]), sfor(move_date, s), cap_for(move_date, s))
    add_preview = []
    for n in (1, 2, 3, 4):
        extra = round(logic.subtotal_for(b["tickets"] + n, s) - paid_so_far(b["id"])["sub"], 2)
        add_preview.append({"n": n, "price": max(extra, 0)})
    return render_template("manage.html", b=b, desc=describe(b, s), paid=paid_so_far(b["id"]), can_move=can_move,
                           dates=dates, move_date=move_date, hours=hours, add_preview=add_preview)


@app.post("/booking/<code>/add")
def add_tickets(code):
    s = get_settings()
    b = booking_by_code(code)
    n = max(1, min(20, int(request.form.get("add", 1))))
    biggest = max(1, int(s.get("max_group_size") or 25))
    if b["tickets"] + n > biggest:
        flash(s["big_group_message"].replace("{max}", str(biggest)))
        return redirect(url_for("manage", code=code))
    ok, why = logic.fits(b["window_start"], b["tickets"] + n, active_bookings(b["date"], exclude_id=b["id"]), sfor(b["date"], s), cap_for(b["date"], s))
    if not ok:
        flash("Sorry, there isn't room to add {} more people in your arrival window. Try moving to another time.".format(n))
        return redirect(url_for("manage", code=code))
    adds = session.get("add_on", {})
    adds[code] = n
    session["add_on"] = adds
    return redirect(url_for("pay", code=code))


@app.post("/booking/<code>/move")
def move(code):
    s = get_settings()
    b = booking_by_code(code)
    d, start = request.form["date"], request.form["start"]
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    ok, why = logic.day_status(d, s, stopped_dates(), today(), live_dates())
    if ok:
        ok, why = logic.fits(start, b["tickets"], active_bookings(d, exclude_id=b["id"]), sfor(d, s), cap_for(d, s))
    if not ok:
        conn.execute("ROLLBACK")
        flash("Sorry, that time isn't available any more. ({})".format(why))
        return redirect(url_for("manage", code=code, move_date=d))
    if request.form.get("confirm") != "1":
        # The brake pedal: show old vs. new and make them confirm before anything changes.
        conn.execute("ROLLBACK")
        return render_template("move_confirm.html", b=b, new_date=d, new_start=start, desc=describe(b, s))
    conn.execute("UPDATE bookings SET date=?, window_start=?, weather_moved=0, checked_in=0 WHERE id=?", (d, start, b["id"]))
    conn.execute("COMMIT")
    b = booking_by_code(code)
    when = "{}, arrival window {}".format(nice_date(d, long=True), window_label(start, s))
    send(b, "email", "Your playdate moved", "You're all set for {}.\nYour updated tickets: {}".format(when, ticket_link(code)))
    send(b, "text", None, "Gooseberry Bridge Farm: Your playdate moved to {}. Your ticket: {}".format(text_window(b, s), ticket_link(code)))
    flash("Moved! You're now booked for {}.".format(when))
    return redirect(url_for("manage", code=code))


# ---------------------------------------------------------------- admin

def admin_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*a, **kw)
    return wrapped


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        ip = request.remote_addr or ""
        recent = [t for t in LOGIN_ATTEMPTS.get(ip, []) if (now() - t).total_seconds() < 600]
        if len(recent) >= 5:
            flash("Too many wrong passwords. Please wait 10 minutes and try again.")
            return render_template("admin_login.html")
        if secrets.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            LOGIN_ATTEMPTS.pop(ip, None)
            session["admin"] = True
            nxt = request.args.get("next") or ""
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("admin_home"))
        LOGIN_ATTEMPTS[ip] = recent + [now()]
        flash("That password didn't work.")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("book"))


@app.route("/admin")
@admin_required
def admin_home():
    s = get_settings()
    stopped = stopped_dates()
    upcoming = logic.open_dates(s, {}, today(), live_dates(), limit=6)
    for d in upcoming:
        d["stopped"] = d["date"] in stopped
    day = request.args.get("date") or (upcoming[0]["date"] if upcoming else today().isoformat())
    cap = cap_for(day, s)
    bookings = [dict(r) for r in db().execute(
        "SELECT * FROM bookings WHERE date=? AND status IN ('paid','held') ORDER BY window_start, id", (day,))]
    active = [b for b in bookings if b["status"] == "paid" or b["hold_expires"] > now().isoformat()]
    stopped_list = []
    for d, row in sorted(stopped.items()):
        rows = db().execute("SELECT COUNT(*) AS groups, COALESCE(SUM(tickets),0) AS t FROM bookings WHERE date=? AND status='paid'", (d,)).fetchone()
        stopped_list.append({"date": d, "at": dt.datetime.fromisoformat(row["stopped_at"]).strftime("%-I:%M %p"),
                             "closed": row["closed"], "groups": rows["groups"], "tickets": rows["t"]})
    return render_template("admin_home.html", upcoming=upcoming, day=day, cap=cap, bookings=bookings,
                           tl=logic.timeline(active, sfor(day, s), cap), stopped_list=stopped_list,
                           totals={"groups": len([b for b in bookings if b["status"] == "paid"]),
                                   "tickets": sum(b["tickets"] for b in bookings if b["status"] == "paid"),
                                   "in": sum(b["tickets"] for b in bookings if b["checked_in"])})


@app.route("/admin/day")
@admin_required
def admin_day():
    """Calendar-style sheet of one day: who arrives when, how long they're here, and who's checked in."""
    s = get_settings()
    day = request.args.get("date") or (logic.open_dates(s, {}, today(), live_dates(), limit=1) or [{"date": today().isoformat()}])[0]["date"]
    s = sfor(day, s)
    cap = cap_for(day, s)
    bookings = [dict(r) for r in db().execute(
        "SELECT * FROM bookings WHERE date=? AND (status='paid' OR (status='held' AND hold_expires > ?)) ORDER BY window_start, id",
        (day, now().isoformat()))]
    length = max(5, int(s["window_minutes"]))
    stay = int(s["stay_minutes"])
    first = logic.to_min(s["first_arrival"])
    if bookings:
        first = min(first, min(logic.to_min(b["window_start"]) for b in bookings) // length * length)
    last_arrival = max([logic.to_min(w["start"]) for w in logic.windows_for(s)] or [first])
    end = max(last_arrival + stay, logic.to_min(s["close_time"]))
    rows = []
    t = first
    gaps = {w["start"] for w in logic.windows_for(s) if w["gap"]}
    while t < end:
        hhmm = logic.from_min(t)
        here = logic.on_farm_at(t, bookings, stay)
        rows.append({"label": nice_time(hhmm), "on_farm": here, "over": here > cap, "gap": hhmm in gaps,
                     "after": t > last_arrival, "hour": t % 60 == 0})
        t += length
    placed, lanes = logic.lanes_for(bookings, s)
    is_today = day == today().isoformat()
    clock = logic.to_min(now().strftime("%H:%M")) if is_today else None
    blocks = []
    for b, lane in placed:
        st = logic.to_min(b["window_start"])
        late = is_today and not b["checked_in"] and b["status"] == "paid" and clock is not None and clock > st + length
        blocks.append({"b": b, "lane": lane, "row": (st - first) // length + 2,
                       "span": max(1, stay // length), "late": late})
    days = logic.open_dates(s, stopped_dates(), today(), live_dates(), limit=60)
    idx = next((i for i, d in enumerate(days) if d["date"] == day), None)
    prev_day = days[idx - 1]["date"] if idx else None
    next_day = days[idx + 1]["date"] if idx is not None and idx + 1 < len(days) else None
    return render_template("admin_day.html", day=day, cap=cap, rows=rows, blocks=blocks, lanes=lanes,
                           bookings=bookings, prev_day=prev_day, next_day=next_day, is_today=is_today,
                           totals={"groups": len(bookings), "tickets": sum(b["tickets"] for b in bookings),
                                   "under2": sum(b["under2"] or 0 for b in bookings),
                                   "in": sum(b["tickets"] for b in bookings if b["checked_in"])})


def arrival_status(b, s):
    """On time, early or late, for a group arriving today."""
    if b["date"] != today().isoformat():
        return ("other_day", "This ticket is for {}, not today.".format(nice_date(b["date"], long=True)))
    clock = logic.to_min(now().strftime("%H:%M"))
    st = logic.to_min(b["window_start"])
    end = st + int(s["window_minutes"])
    if clock < st:
        return ("early", "{} min early. Their window opens at {}.".format(st - clock, nice_time(b["window_start"])))
    if clock > end:
        return ("late", "{} min after their window ended.".format(clock - end))
    return ("on_time", "Right on time.")


@app.route("/admin/checkin/<code>", methods=["GET", "POST"])
@admin_required
def staff_checkin(code):
    """What opens when you scan a guest's ticket QR with your phone camera."""
    s = get_settings()
    b = booking_by_code(code)
    if request.method == "POST" and b["status"] == "paid":
        db().execute("UPDATE bookings SET checked_in = ?, checked_in_at = ? WHERE id=?",
                     (0 if b["checked_in"] else 1, None if b["checked_in"] else now().isoformat(), b["id"]))
        b = booking_by_code(code)
        flash(("Checked in: {}, {}.".format(b["name"], describe(b, s))) if b["checked_in"] else "Check-in undone.")  # noqa
    kind, note = arrival_status(b, s)
    used_at = None
    if b["checked_in"] and b.get("checked_in_at") and request.method == "GET":
        used_at = dt.datetime.fromisoformat(b["checked_in_at"]).strftime("%-I:%M %p on %b %-d")
    return render_template("admin_checkin.html", b=b, desc=describe(b, s), kind=kind, note=note,
                           paid=paid_so_far(b["id"]), used_at=used_at)


@app.route("/admin/find")
@admin_required
def admin_find():
    """Find a group by name, phone or code, for guests who can't pull up their ticket."""
    q = request.args.get("q", "").strip()
    day = request.args.get("date") or today().isoformat()
    rows = []
    if q:
        like = "%{}%".format(q.replace("%", ""))
        digits = "".join(ch for ch in q if ch.isdigit())
        rows = [dict(r) for r in db().execute(
            "SELECT * FROM bookings WHERE status='paid' AND (name LIKE ? OR code LIKE ? OR email LIKE ? "
            "OR (? <> '' AND REPLACE(REPLACE(REPLACE(REPLACE(phone,'(',''),')',''),'-',''),' ','') LIKE ?)) "
            "ORDER BY (date = ?) DESC, date, window_start LIMIT 30",
            (like, like.upper(), like, digits, "%{}%".format(digits), day))]
    return render_template("admin_find.html", q=q, rows=rows, day=day)


@app.post("/admin/stop")
@admin_required
def admin_stop():
    d = request.form["date"]
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT OR IGNORE INTO stopped_days(date, stopped_at) VALUES (?,?)", (d, now().isoformat()))
    released = conn.execute("UPDATE bookings SET status='released' WHERE date=? AND status='held'", (d,)).rowcount
    conn.execute("COMMIT")
    flash("Bookings stopped for {}.{}".format(nice_date(d), " Released {} checkout(s) in progress.".format(released) if released else ""))
    return redirect(url_for("admin_home", date=d))


@app.post("/admin/resume")
@admin_required
def admin_resume():
    d = request.form["date"]
    db().execute("DELETE FROM stopped_days WHERE date=?", (d,))
    flash("Bookings reopened for {}.".format(nice_date(d)))
    return redirect(url_for("admin_home", date=d))


@app.post("/admin/pause")
@admin_required
def admin_pause():
    on = request.form.get("on") == "1"
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('paused_all', ?)", (json.dumps(on),))
    if on:
        conn.execute("UPDATE bookings SET status='released' WHERE status='held'")
    conn.execute("COMMIT")
    flash("All bookings paused." if on else "Bookings resumed.")
    return redirect(url_for("admin_home"))


@app.post("/admin/close")
@admin_required
def admin_close():
    """Weather closure: stop the day and invite everyone to pick a new day."""
    s = get_settings()
    d = request.form["date"]
    db().execute("INSERT OR IGNORE INTO stopped_days(date, stopped_at) VALUES (?,?)", (d, now().isoformat()))
    db().execute("UPDATE stopped_days SET closed=1 WHERE date=?", (d,))
    db().execute("UPDATE bookings SET status='released' WHERE date=? AND status='held'", (d,))
    rows = [dict(r) for r in db().execute("SELECT * FROM bookings WHERE date=? AND status='paid'", (d,))]
    for b in rows:
        db().execute("UPDATE bookings SET weather_moved=1 WHERE id=?", (b["id"],))
        link = url_for("manage", code=b["code"], _external=True)
        send(b, "email", "We're closed {} - let's pick a new day".format(nice_date(d)),
             "Hi {}, we're sorry! We have to close the farm on {}. Your ticket is still good. "
             "Pick any open day here: {}".format(b["name"], nice_date(d, long=True), link))
        send(b, "text", None, "Gooseberry Bridge Farm: Sorry, we have to close {}. Your ticket is still good - pick a new day: {}".format(nice_date(d), link))
    flash("{} closed. Messaged {} group(s) with a link to pick a new day.".format(nice_date(d), len(rows)))
    return redirect(url_for("admin_home", date=d))


@app.post("/admin/checkin/<int:bid>")
@admin_required
def admin_checkin(bid):
    db().execute("UPDATE bookings SET checked_in = 1 - checked_in, checked_in_at = CASE WHEN checked_in = 0 THEN ? ELSE NULL END WHERE id=?",
                 (now().isoformat(), bid))
    if request.form.get("back") == "day":
        return redirect(url_for("admin_day", date=request.form.get("date")))
    return redirect(url_for("admin_home", date=request.form.get("date")))  # noqa


@app.post("/admin/gate/<int:bid>")
@admin_required
def admin_gate(bid):
    """Extra guests who arrived without tickets: record the sale so headcount stays right."""
    s = get_settings()
    b = dict(db().execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone())
    n = max(1, int(request.form.get("n", 1)))
    method = request.form.get("method", "cash")
    sub = max(round(logic.subtotal_for(b["tickets"] + n, s) - paid_so_far(bid)["sub"], 2), 0)
    tax, total = logic.tax_for(sub, s)
    db().execute("UPDATE bookings SET tickets = tickets + ? WHERE id=?", (n, bid))
    db().execute("INSERT INTO payments(booking_id, kind, tickets, subtotal, tax, total, method, created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (bid, "gate", n, sub, tax, total, method, now().isoformat()))
    flash("Added {} {} to {}'s ticket at the gate: collect {}.".format(n, "person" if n == 1 else "people", b["name"], money(total)))
    if request.form.get("back") == "checkin":
        return redirect(url_for("staff_checkin", code=b["code"]))
    return redirect(url_for("admin_home", date=b["date"]))


@app.post("/admin/quick_add")
@admin_required
def admin_quick_add():
    """Test helper: drop a pretend group straight onto a window, rules or not, and say whether guests could have booked it."""
    s = get_settings()
    d, start = request.form["date"], request.form["start"]
    raw = request.form.get("tickets", "").replace(",", " ").split()
    sizes = [max(1, min(30, int(x))) for x in raw if x.isdigit()]
    if not sizes:
        flash("Type a group size (like 5), or several separated by spaces (like 5 2 8).")
        return redirect(url_for("admin_home", date=d))
    notes = []
    for n in sizes:
        ok, why = logic.fits(start, n, active_bookings(d), sfor(d, s), cap_for(d, s))
        stamp = now().isoformat()
        cur = db().execute(
            "INSERT INTO bookings(code, date, window_start, tickets, under2, name, status, created_at, paid_at, demo) "
            "VALUES (?,?,?,?,0,?,'paid',?,?,1)",
            ("QUICK" + secrets.token_hex(3).upper(), d, start, n, "Quick add ({})".format(n), stamp, stamp))
        sub = logic.subtotal_for(n, s)
        tax, total = logic.tax_for(sub, s)
        db().execute("INSERT INTO payments(booking_id, kind, tickets, subtotal, tax, total, method, created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (cur.lastrowid, "online", n, sub, tax, total, "test_card", stamp))
        notes.append("{} ({})".format(n, "guests could have booked this" if ok else "guests would NOT have been allowed: " + why.lower()))
    flash("Added at {}: {}.".format(nice_time(start), "; ".join(notes)))
    return redirect(url_for("admin_home", date=d))


@app.post("/admin/remove/<int:bid>")
@admin_required
def admin_remove(bid):
    """Remove a pretend booking (real ones are cancelled from the Bookings page instead)."""
    row = db().execute("SELECT date, demo FROM bookings WHERE id=?", (bid,)).fetchone()
    if row and row["demo"]:
        db().execute("DELETE FROM payments WHERE booking_id=?", (bid,))
        db().execute("DELETE FROM outbox WHERE booking_id=?", (bid,))
        db().execute("DELETE FROM bookings WHERE id=?", (bid,))
    target = "admin_day" if request.form.get("back") == "day" else "admin_home"
    return redirect(url_for(target, date=row["date"] if row else None))


@app.post("/admin/toggle_quick_add")
@admin_required
def admin_toggle_quick_add():
    s = get_settings()
    save_setting("show_quick_add", not s.get("show_quick_add", True))
    return redirect(url_for("admin_home", date=request.form.get("date")))


@app.post("/admin/remind")
@admin_required
def admin_remind():
    s = get_settings()
    d = request.form["date"]
    rows = [dict(r) for r in db().execute("SELECT * FROM bookings WHERE date=? AND status='paid'", (d,))]
    for b in rows:
        send(b, "text", None, "Gooseberry Bridge Farm: See you {}! Your ticket & arrival details: {}".format(text_window(b, s), ticket_link(b["code"])))
        send(b, "email", "See you soon at Gooseberry Bridge Farm",
             "See you {}, arrival window {}. {}\nYour tickets: {}".format(nice_date(d, long=True), window_label(b["window_start"], s),
                                                                         describe(b, s), ticket_link(b["code"])))
    flash("Sent {} reminder(s) for {} (see Messages).".format(len(rows), nice_date(d)))
    return redirect(url_for("admin_home", date=d))


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    s = get_settings()
    if request.method == "POST":
        f = request.form
        preset = f.get("preset")
        if preset in PRESETS:
            for k, v in PRESETS[preset]["values"].items():
                save_setting(k, v)
            flash("Loaded the '{}' prices. Adjust anything below, then save.".format(PRESETS[preset]["name"]))
            return redirect(url_for("admin_settings"))
        num_float = ["booking_fee", "price_per_ticket", "small_price", "package_price", "extra_price", "tax_rate"]
        num_int = ["small_max", "package_max", "window_minutes", "groups_per_window", "gap_every", "stay_minutes",
                   "max_on_farm", "max_group_size", "hold_minutes", "reschedule_cutoff_hours", "reminder_days"]
        for k in num_float:
            save_setting(k, max(0.0, float(f.get(k) or 0)))
        for k in num_int:
            save_setting(k, max(0, int(float(f.get(k) or 0))))
        for k in ["season_start", "season_end", "first_arrival", "close_time", "tax_mode"]:
            if f.get(k):
                save_setting(k, f.get(k))
        save_setting("open_days", [d for d in logic.WEEKDAYS if f.get("day_" + d)])
        save_setting("weekday_hours", {d: [f.get("wh_start_" + d, ""), f.get("wh_end_" + d, "")]
                                       for d in logic.WEEKDAYS if f.get("wh_start_" + d) and f.get("wh_end_" + d)})
        save_setting("max_group_size", max(1, int(float(f.get("max_group_size") or 1))))
        for k in ["policy_text", "policy_short", "big_group_message"]:
            if f.get(k, "").strip():
                save_setting(k, f.get(k).strip())
        flash("Saved. New bookings use these settings right away; existing bookings keep what they paid.")
        return redirect(url_for("admin_settings"))
    table = []
    for n in range(1, 11):
        sub = logic.subtotal_for(n, s)
        tax, total = logic.tax_for(sub, s)
        table.append({"n": n, "sub": sub, "tax": tax, "total": total, "per": sub / n})
    wins = logic.windows_for(s)
    return render_template("admin_settings.html", presets=PRESETS, table=table, weekdays=logic.WEEKDAYS,
                           rule=logic.rule_text(s), windows_per_day=len([w for w in wins if not w["gap"]]))


@app.route("/admin/calendar")
@admin_required
def admin_calendar():
    """Release calendar: which days guests can book, and each day's hours."""
    import calendar as pycal
    s = get_settings()
    plan = day_plan()
    stopped = stopped_dates()
    caps = {r["date"]: r["max_on_farm"] for r in db().execute("SELECT date, max_on_farm FROM day_overrides")}
    counts = {r["date"]: (r["g"], r["t"]) for r in db().execute(
        "SELECT date, COUNT(*) AS g, SUM(tickets) AS t FROM bookings WHERE status='paid' GROUP BY date")}
    first, last = logic.parse_date(s["season_start"]), logic.parse_date(s["season_end"])
    cal = pycal.Calendar(firstweekday=6)  # weeks start on Sunday
    months = []
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        weeks = []
        for week in cal.monthdatescalendar(y, m):
            row = []
            for day in week:
                d = day.isoformat()
                if day.month != m:
                    row.append(None); continue
                p = plan.get(d) or {}
                start, end, source = logic.hours_for(d, s, p)
                g, t = counts.get(d, (0, 0))
                row.append({
                    "date": d, "num": day.day, "live": bool(p.get("live")),
                    "usual": logic.WEEKDAYS[day.weekday()] in s["open_days"],
                    "in_season": s["season_start"] <= d <= s["season_end"], "past": day < today(),
                    "hours": logic.short_range(start, end), "custom": source == "custom",
                    "groups": g, "people": t or 0, "stopped": d in stopped, "note": p.get("note") or "",
                    "cap": caps.get(d), "own_breaks": p.get("gap_every") is not None,
                })
            weeks.append(row)
        months.append({"label": dt.date(y, m, 1).strftime("%B %Y"), "id": "m-{}-{:02d}".format(y, m), "weeks": weeks})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    edit = request.args.get("edit")
    editing = None
    if edit:
        p = plan.get(edit) or {}
        start, end, source = logic.hours_for(edit, s, p)
        ov = db().execute("SELECT max_on_farm FROM day_overrides WHERE date=?", (edit,)).fetchone()
        editing = {"date": edit, "live": bool(p.get("live")), "start": start, "end": end, "custom": source == "custom",
                   "note": p.get("note") or "", "weekday": logic.WEEKDAYS[logic.parse_date(edit).weekday()],
                   "cap": ov["max_on_farm"] if ov else None, "gap_every": p.get("gap_every"),
                   "groups": counts.get(edit, (0, 0))[0], "people": counts.get(edit, (0, 0))[1] or 0}
        editing["special"] = editing["custom"] or editing["cap"] is not None or editing["gap_every"] is not None
    live = sorted(d for d, p in plan.items() if p.get("live") and d >= today().isoformat())
    return render_template("admin_calendar.html", months=months, editing=editing,
                           live_count=len(live), last_live=live[-1] if live else None,
                           suggest_through=(today() + dt.timedelta(days=21)).isoformat()
                           if today().isoformat() >= s["season_start"] else
                           (logic.parse_date(s["season_start"]) + dt.timedelta(days=20)).isoformat())


@app.post("/admin/calendar/toggle")
@admin_required
def admin_calendar_toggle():
    d = request.form["date"]
    row = db().execute("SELECT live FROM day_plan WHERE date=?", (d,)).fetchone()
    if row:
        db().execute("UPDATE day_plan SET live = 1 - live, released_at = CASE WHEN live = 0 THEN ? ELSE released_at END WHERE date=?",
                     (now().isoformat(), d))
    else:
        db().execute("INSERT INTO day_plan(date, live, released_at) VALUES (?, 1, ?)", (d, now().isoformat()))
    live = db().execute("SELECT live FROM day_plan WHERE date=?", (d,)).fetchone()["live"]
    if not live:
        n = db().execute("SELECT COUNT(*) FROM bookings WHERE date=? AND status='paid'", (d,)).fetchone()[0]
        db().execute("UPDATE bookings SET status='released' WHERE date=? AND status='held'", (d,))
        flash("{} hidden from guests.{}".format(nice_date(d), " Its {} existing booking(s) still stand.".format(n) if n else ""))
    else:
        flash("{} is live. Guests can book it now.".format(nice_date(d)))
    return redirect(url_for("admin_calendar", edit=request.form.get("edit") or None) + "#" + "m-" + d[:7])


@app.post("/admin/calendar/day")
@admin_required
def admin_calendar_day():
    """Save one day's own hours, crowd limit, break pattern and note (or put it back to your usual settings)."""
    d = request.form["date"]
    note = request.form.get("note", "").strip()
    db().execute("INSERT OR IGNORE INTO day_plan(date, live) VALUES (?, 0)", (d,))
    if request.form.get("reset"):
        db().execute("UPDATE day_plan SET first_arrival=NULL, close_time=NULL, gap_every=NULL, note=? WHERE date=?", (note, d))
        db().execute("DELETE FROM day_overrides WHERE date=?", (d,))
        flash("{} is back to your usual settings. Nobody already booked was moved.".format(nice_date(d)))
        return redirect(url_for("admin_calendar", edit=d) + "#editor")
    s = get_settings()
    wd_start, wd_end, _ = logic.hours_for(d, s, None)
    start, end = request.form["first_arrival"], request.form["close_time"]
    if logic.to_min(end) <= logic.to_min(start):
        flash("The closing time has to be after the first arrival.")
        return redirect(url_for("admin_calendar", edit=d) + "#editor")
    if (start, end) == (wd_start, wd_end):
        start = end = None  # same as the weekday's hours: keep following them if you change the weekday later
    gap = request.form.get("gap_every", "").strip()
    gap = None if gap in ("", "usual") else max(0, int(gap))
    db().execute("UPDATE day_plan SET first_arrival=?, close_time=?, gap_every=?, note=? WHERE date=?", (start, end, gap, note, d))
    cap = request.form.get("max_on_farm", "").strip()
    if cap == "" or int(cap) == int(s["max_on_farm"]):
        db().execute("DELETE FROM day_overrides WHERE date=?", (d,))
    else:
        db().execute("INSERT OR REPLACE INTO day_overrides(date, max_on_farm, note) VALUES (?,?,?)", (d, max(0, int(cap)), note))
    flash("Saved {}. Nobody already booked was moved or cancelled.".format(nice_date(d)))
    return redirect(url_for("admin_calendar", edit=d) + "#editor")


@app.post("/admin/calendar/release")
@admin_required
def admin_calendar_release():
    """Release every usual open day from today (or the season start) through a date, in one go."""
    s = get_settings()
    through = logic.parse_date(request.form["through"])
    day = max(today(), logic.parse_date(s["season_start"]))
    end = min(through, logic.parse_date(s["season_end"]))
    n = 0
    while day <= end:
        if logic.WEEKDAYS[day.weekday()] in s["open_days"]:
            d = day.isoformat()
            db().execute("INSERT OR IGNORE INTO day_plan(date, live) VALUES (?, 0)", (d,))
            n += db().execute("UPDATE day_plan SET live=1, released_at=? WHERE date=? AND live=0", (now().isoformat(), d)).rowcount
        day += dt.timedelta(days=1)
    flash("Released {} more day(s) through {}.".format(n, nice_date(through)))
    return redirect(url_for("admin_calendar"))


@app.route("/admin/days")
@admin_required
def admin_days():
    """Day-by-day settings now live in the calendar's day editor."""
    return redirect(url_for("admin_calendar"))


@app.route("/admin/bookings")
@admin_required
def admin_bookings():
    status = request.args.get("status", "paid")
    q = "SELECT * FROM bookings" + (" WHERE status=?" if status != "all" else "") + " ORDER BY date, window_start"
    rows = [dict(r) for r in db().execute(q, (status,) if status != "all" else ())]
    for r in rows:
        r["paid"] = paid_so_far(r["id"])["total"]
    return render_template("admin_bookings.html", rows=rows, status=status)


@app.post("/admin/bookings/<int:bid>/cancel")
@admin_required
def admin_cancel(bid):
    db().execute("UPDATE bookings SET status='cancelled' WHERE id=?", (bid,))
    flash("Booking cancelled. (In the real app you'd choose whether to refund in Stripe.)")
    return redirect(url_for("admin_bookings"))


@app.route("/admin/messages")
@admin_required
def admin_messages():
    rows = [dict(r) for r in db().execute("SELECT * FROM outbox ORDER BY id DESC LIMIT 200")]
    return render_template("admin_messages.html", rows=rows)


@app.post("/admin/messages/clear")
@admin_required
def admin_messages_clear():
    db().execute("DELETE FROM outbox")
    return redirect(url_for("admin_messages"))


HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "square_history.json")


def square_history():
    """Past springs from Square, one row per visit day: groups, additional people, revenue before tax."""
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"note": "", "days": []}


def app_days(s):
    """This app's sales, one row per day paid, counted the same way as the Square history:
    groups = bookings paid, additional people = people beyond the package plus anyone added later or at the gate."""
    over = int(s.get("package_max") or 0)
    days = {}
    rows = db().execute("""SELECT p.kind, p.tickets, p.subtotal, p.created_at, b.date AS visit, b.demo
                           FROM payments p JOIN bookings b ON b.id = p.booking_id WHERE b.status = 'paid'""")
    for r in rows:
        d = r["created_at"][:10]
        if d[:4] < r["visit"][:4]:
            d = r["visit"][:4] + "-01-01"  # sold before the season's year (early presale or test data): count it at the start of that year
        row = days.setdefault(d, {"date": d, "groups": 0, "extra": 0, "revenue": 0.0, "demo": 0})
        if r["kind"] == "online":
            row["groups"] += 1
            row["extra"] += max(0, r["tickets"] - over) if over else 0
        else:
            row["extra"] += r["tickets"]
        row["revenue"] += r["subtotal"] or 0
        row["demo"] = max(row["demo"], r["demo"] or 0)
    return [days[k] for k in sorted(days)]


def build_report(s, through_md):
    """Every year side by side: season totals, month-by-month, and season-to-date through the same calendar day."""
    hist = square_history()
    days = [dict(d, source="square") for d in hist["days"]] + [dict(d, source="app") for d in app_days(s)]
    years = {}
    for d in days:
        y = d["date"][:4]
        yr = years.setdefault(y, {"year": y, "source": d["source"], "demo": False, "days": [],
                                  "total": {"groups": 0, "extra": 0, "revenue": 0.0},
                                  "todate": {"groups": 0, "extra": 0, "revenue": 0.0}, "months": {}})
        yr["demo"] = yr["demo"] or bool(d.get("demo"))
        yr["big_left_out"] = yr.get("big_left_out", 0) + (d.get("big_left_out") or 0)
        yr["nonplay_left_out"] = yr.get("nonplay_left_out", 0) + (d.get("nonplay_left_out") or 0)
        if not (d["groups"] or d["extra"]):
            continue  # a day with only left-out sales adds nothing to the counts
        yr["days"].append(d)
        m = yr["months"].setdefault(d["date"][:7], {"key": d["date"][:7], "groups": 0, "extra": 0, "revenue": 0.0, "days": []})
        m["days"].append(d)
        for k in ("groups", "extra", "revenue"):
            yr["total"][k] += d[k] or 0
            m[k] += d[k] or 0
            if d["date"][5:] <= through_md:
                yr["todate"][k] += d[k] or 0
    out = sorted(years.values(), key=lambda y: y["year"], reverse=True)
    for yr in out:
        yr["months"] = [yr["months"][k] for k in sorted(yr["months"])]
        for m in yr["months"]:
            m["label"] = dt.date(int(m["key"][:4]), int(m["key"][5:]), 1).strftime("%B")
    for i, yr in enumerate(out):
        prev = out[i + 1] if i + 1 < len(out) else None
        yr["vs"] = {}
        for k in ("groups", "extra", "revenue"):
            before = prev["todate"][k] if prev else 0
            yr["vs"][k] = None if not before else (yr["todate"][k] - before) / before * 100
        if prev:
            pm = {m["key"][5:]: m for m in prev["months"]}
            for m in yr["months"]:
                b = pm.get(m["key"][5:])
                m["vs"] = None if not b or not b["revenue"] else (m["revenue"] - b["revenue"]) / b["revenue"] * 100
    return out, hist["note"]


@app.route("/admin/reports")
@admin_required
def admin_reports():
    s = get_settings()
    season_year = s["season_start"][:4]
    t = today()
    through = request.args.get("through")
    if through:
        through_md = through[5:] if len(through) == 10 else through
    else:
        through_md = t.strftime("%m-%d")
    through_md = through_md if re.fullmatch(r"\d\d-\d\d", through_md or "") else t.strftime("%m-%d")
    full = through_md >= s["season_end"][5:]
    years, note = build_report(s, "12-31" if full else through_md)
    # chart: running totals by day of season, one line per year
    start_md, end_md = s["season_start"][5:], s["season_end"][5:]
    first_md = min(["03-01", start_md] + [d["date"][5:] for y in years for d in y["days"] if d["groups"] or d["extra"]])
    last_md = max([end_md] + [d["date"][5:] for y in years for d in y["days"]])
    axis = []
    day = dt.date(2001, int(first_md[:2]), int(first_md[3:]))
    while day.strftime("%m-%d") <= last_md:
        axis.append(day.strftime("%m-%d")); day += dt.timedelta(days=1)
    series = []
    for y in years:
        byday = {d["date"][5:]: d for d in y["days"]}
        run = {"groups": 0, "extra": 0, "revenue": 0}
        pts = {"groups": [], "extra": [], "revenue": []}
        last_day = max(byday) if byday else first_md
        for md in axis:
            if md > last_day:
                break
            d = byday.get(md)
            for k in run:
                run[k] += (d[k] or 0) if d else 0
                pts[k].append(round(run[k], 2))
        series.append({"year": y["year"], "app": y["source"] == "app", "pts": pts})
    rows = [dict(r) for r in db().execute("""
        SELECT b.date AS date, COUNT(DISTINCT b.id) AS groups, SUM(p.tickets) AS tickets,
               SUM(p.subtotal) AS sub, SUM(p.tax) AS tax, SUM(p.total) AS total,
               SUM(CASE WHEN p.kind='gate' THEN p.tickets ELSE 0 END) AS gate
        FROM payments p JOIN bookings b ON b.id = p.booking_id
        WHERE b.status IN ('paid')
        GROUP BY b.date ORDER BY b.date""")]
    tot = {k: sum((r[k] or 0) for r in rows) for k in ("groups", "tickets", "sub", "tax", "total", "gate")}
    through_label = dt.date(2001, int(through_md[:2]), int(through_md[3:])).strftime("%B ") + str(int(through_md[3:]))
    return render_template("admin_reports.html", rows=rows, tot=tot, years=years, note=note, full=full,
                           through_md=through_md, through_label=through_label,
                           through_value="{}-{}".format(season_year, through_md),
                           chart={"axis": axis, "series": series, "through": through_md, "full": full},
                           season_year=season_year)


@app.route("/admin/reports.csv")
@admin_required
def admin_reports_csv():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["code", "date", "arrival", "name", "email", "phone", "tickets", "under2", "status",
                "payment_kind", "method", "subtotal", "tax", "total", "paid_at"])
    for r in db().execute("""SELECT b.*, p.kind, p.method, p.subtotal, p.tax, p.total, p.created_at AS pat
                             FROM bookings b LEFT JOIN payments p ON p.booking_id=b.id ORDER BY b.date, b.window_start"""):
        w.writerow([r["code"], r["date"], r["window_start"], r["name"], r["email"], r["phone"], r["tickets"], r["under2"],
                    r["status"], r["kind"], r["method"], r["subtotal"], r["tax"], r["total"], r["pat"]])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=playdate-sales.csv"})


@app.post("/admin/demo")
@admin_required
def admin_demo():
    """Fill the first few open days with pretend bookings so the capacity rules have something to push against."""
    s = get_settings()
    rng = random.Random(7)
    names = ["Miller", "Johnson", "Garcia", "Smith", "Brown", "Davis", "Wilson", "Moore", "Taylor", "Anderson",
             "Thomas", "Jackson", "White", "Harris", "Martin", "Thompson", "Clark", "Lewis", "Walker", "Hall"]
    days = [d["date"] for d in logic.open_dates(s, stopped_dates(), today(), live_dates()) if d["open"]][:6]
    made = 0
    for d in days:
        cap = cap_for(d, s)
        sd = sfor(d, s)
        for w in logic.windows_for(sd):
            if w["gap"] or rng.random() > 0.7:
                continue
            n = rng.choice([2, 3, 4, 4, 5, 5, 5, 6, 7, 8])
            ok, _ = logic.fits(w["start"], n, active_bookings(d), sd, cap)
            if not ok:
                continue
            fam = rng.choice(names)
            code = "DEMO" + secrets.token_hex(3).upper()
            stamp = now().isoformat()
            cur = db().execute(
                "INSERT INTO bookings(code, date, window_start, tickets, under2, name, email, phone, sms_ok, status, created_at, paid_at, demo) "
                "VALUES (?,?,?,?,?,?,?,?,1,'paid',?,?,1)",
                (code, d, w["start"], n, rng.choice([0, 0, 0, 1]), "The {} family".format(fam),
                 "{}@example.com".format(fam.lower()), "(417) 555-01{:02d}".format(rng.randint(0, 99)), stamp, stamp))
            sub = logic.subtotal_for(n, s)
            tax, total = logic.tax_for(sub, s)
            db().execute("INSERT INTO payments(booking_id, kind, tickets, subtotal, tax, total, method, created_at) VALUES (?,?,?,?,?,?,?,?)",
                         (cur.lastrowid, "online", n, sub, tax, total, "test_card", stamp))
            made += 1
    flash("Added {} pretend bookings across the first {} open days.".format(made, len(days)))
    return redirect(url_for("admin_home"))


@app.post("/admin/demo/clear")
@admin_required
def admin_demo_clear():
    ids = [r["id"] for r in db().execute("SELECT id FROM bookings WHERE demo=1")]
    for i in ids:
        db().execute("DELETE FROM payments WHERE booking_id=?", (i,))
        db().execute("DELETE FROM outbox WHERE booking_id=?", (i,))
    db().execute("DELETE FROM bookings WHERE demo=1")
    flash("Removed {} pretend bookings.".format(len(ids)))
    return redirect(url_for("admin_home"))


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print("\n  Farm Baby Playdates is running.")
    print("  Guest booking page:  http://127.0.0.1:{}/book".format(port))
    print("  Farm admin:          http://127.0.0.1:{}/admin   (password: {})\n".format(port, ADMIN_PASSWORD))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=True)  # picks up code changes automatically
