"""Real email for the farm apps (playdates and the farm shop), and an honest record of what happened.

Every message the apps write lands in Admin > Messages (the `outbox` table), as it always has. When
real email is switched on for that part of the farm, it is also sent through mailer.py, and the
message's row records what happened:

  sent     it went out
  waiting  Resend's daily limit (or the farm's own, below) was reached, or Resend was busy or down:
           it goes out by itself later (after the limit resets in the evening, or 15 minutes on)
  failed   something trying again won't fix (a bad address, a bad key, the email service isn't set up):
           shown in red, with a Send again button
  off      real email is switched off for this part of the farm (test mode): it only shows in Messages
  console  no email service is connected on this computer (the Mac): it only shows in Messages

Switches:
  playdates  PLAYDATE_EMAILS=on in Render (off until playdates go live)
  shop       "Send shop emails for real" on the shop admin's Settings page

The farm's own daily limit (shop Settings, default 40) keeps the farm from using up the whole of
Resend's free 100 a day, which it shares with Pasture Keeper (whose password resets must always fit).
Resend's day runs midnight to midnight UTC, so this one does too.

Retries happen in the background, at most every 5 minutes, whenever someone is using the site.
"""
import datetime as dt
import json
import os
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo

import dbcompat
import mailer

DEFAULT_DAILY_LIMIT = 40
RETRY_MINUTES = 15
MAX_TRIES = 8
STATUSES = ("sent", "waiting", "failed", "off", "console")
CENTRAL = ZoneInfo("America/Chicago")

COLUMNS = (("email_status", "TEXT DEFAULT ''"), ("email_note", "TEXT DEFAULT ''"), ("email_tries", "INTEGER DEFAULT 0"),
           ("email_sent_at", "TEXT DEFAULT ''"), ("email_next_try", "TEXT DEFAULT ''"), ("email_kind", "TEXT DEFAULT ''"))

_mailer = {}


def _playdate_switch_env():
    return os.environ.get("PLAYDATE_EMAILS", "off").strip().lower() in ("on", "yes", "true", "1")


if _playdate_switch_env():
    # With playdate emails switched on, half-finished email settings stop the server at start (Render then keeps
    # the old version running) rather than letting confirmations quietly not arrive. mailer.build_mailer raises.
    _mailer["m"], _mailer["why"] = mailer.build_mailer("PLAYDATE_MAIL_FROM"), ""


def ensure_schema(con, add_column):
    try:
        con.execute("SELECT id FROM outbox WHERE 1=0").fetchall()
    except Exception:  # noqa: BLE001  the playdate app makes the outbox table; it adds these columns when it starts
        if hasattr(con, "rollback"):
            con.rollback()
        return
    for name, decl in COLUMNS:
        add_column(con, "outbox", name, decl)  # added Nov 21, 2026


def get_mailer():
    """The email service, built once. (None, why) if it's switched on but set up wrong."""
    if "m" not in _mailer:
        try:
            _mailer["m"], _mailer["why"] = mailer.build_mailer("PLAYDATE_MAIL_FROM"), ""
        except Exception as e:  # noqa: BLE001  half-set-up mail must not crash the site
            _mailer["m"], _mailer["why"] = None, str(e)
    return _mailer["m"], _mailer["why"]


def service_status():
    """For the Settings page: what kind of email service is connected."""
    m, why = get_mailer()
    if m is None:
        return "broken", "The email settings in Render are incomplete: " + why
    if m.kind == "resend":
        return "resend", "Connected to Resend."
    if m.kind == "memory":
        return "resend", "Connected (test stand-in)."
    return "console", "No email service is connected here (fine on the Mac): emails only show in Messages."


def utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.isoformat()


def next_reset(now=None):
    """Resend's daily limit resets at midnight UTC (6 pm Central in winter, 7 pm in summer). A few minutes' grace."""
    now = now or utcnow()
    return (now + dt.timedelta(days=1)).replace(hour=0, minute=3, second=0)


def shop_setting(con, key, default):
    try:
        r = con.execute("SELECT value FROM shop_settings WHERE key=?", (key,)).fetchone()
    except Exception:  # noqa: BLE001  (shop tables not made yet)
        return default
    if not r:
        return default
    try:
        return json.loads(r[0])
    except (ValueError, TypeError):
        return default


def switched_on(con, kind):
    if kind == "shop":
        return bool(shop_setting(con, "shop_emails", False))
    return _playdate_switch_env()


def daily_limit(con):
    try:
        return max(0, int(shop_setting(con, "email_daily_limit", DEFAULT_DAILY_LIMIT)))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_LIMIT


def sent_today(con, now=None):
    start = (now or utcnow()).replace(hour=0, minute=0, second=0)
    r = con.execute("SELECT COUNT(*) FROM outbox WHERE email_status='sent' AND email_sent_at >= ?", (iso(start),)).fetchone()
    return int(r[0])


def _set(con, oid, **cols):
    keys = list(cols)
    con.execute("UPDATE outbox SET {} WHERE id=?".format(", ".join("{}=?".format(k) for k in keys)), tuple(cols[k] for k in keys) + (oid,))
    if hasattr(con, "commit"):
        try:
            con.commit()
        except Exception:  # noqa: BLE001
            pass


def deliver(con, outbox_id, kind, by_hand=False, spare=0):
    """Send one Messages row as an email (if that part of the farm sends real email) and record what happened.

    Never raises. Returns the new status. by_hand: a person pressed Send again (skips the farm's own daily
    limit, but not Resend's). spare: for the farm's own notices (a new order), leave the day's last `spare`
    emails for customers; past that the notice isn't sent at all (not held: by the evening it's old news).
    """
    try:
        row = con.execute("SELECT * FROM outbox WHERE id=?", (outbox_id,)).fetchone()
        if not row:
            return None
        row = dict(row)
        if row.get("channel") != "email" or not row.get("recipient"):
            return None
        if not switched_on(con, kind) and not by_hand:
            _set(con, outbox_id, email_status="off", email_kind=kind,
                 email_note="Real email is switched off for {} (test mode), so this only shows here.".format(
                     "the shop" if kind == "shop" else "playdates"))
            return "off"
        m, why = get_mailer()
        if m is None:
            _set(con, outbox_id, email_status="failed", email_kind=kind, email_note=why)
            return "failed"
        now = utcnow()
        if spare and not by_hand and sent_today(con, now) >= daily_limit(con) - spare:
            _set(con, outbox_id, email_status="off", email_kind=kind,
                 email_note="Not sent: today's last {} emails are kept for customers. The order is on the Orders page.".format(spare))
            return "off"
        if not by_hand and sent_today(con, now) >= daily_limit(con):
            _set(con, outbox_id, email_status="waiting", email_kind=kind, email_next_try=iso(next_reset(now)),
                 email_note="Held: the farm's daily email limit ({}) was reached. It goes out after the evening reset.".format(daily_limit(con)))
            return "waiting"
        outcome, note = m.send_result(row["recipient"], row.get("subject") or "Gooseberry Bridge Farm", row["body"])
        tries = int(row.get("email_tries") or 0) + 1
        if outcome == "sent":
            _set(con, outbox_id, email_status="sent", email_kind=kind, email_tries=tries, email_sent_at=iso(now), email_note="", email_next_try="")
        elif outcome == "console":
            _set(con, outbox_id, email_status="console", email_kind=kind, email_tries=tries, email_note=note)
        elif outcome == "limit":
            _set(con, outbox_id, email_status="waiting", email_kind=kind, email_tries=tries, email_next_try=iso(next_reset(now)), email_note=note)
            outcome = "waiting"
        elif outcome == "retry" and tries < MAX_TRIES:
            _set(con, outbox_id, email_status="waiting", email_kind=kind, email_tries=tries,
                 email_next_try=iso(now + dt.timedelta(minutes=RETRY_MINUTES)), email_note=note + " Trying again shortly.")
            outcome = "waiting"
        else:
            _set(con, outbox_id, email_status="failed", email_kind=kind, email_tries=tries, email_note=note)
            outcome = "failed"
        return outcome
    except Exception as e:  # noqa: BLE001  an email problem must never break a booking or an order
        mailer._log("FARM MAIL problem with outbox {}: {}: {}".format(outbox_id, type(e).__name__, e))
        return None


def retry_due(con, now=None, limit=25):
    """Send the waiting emails whose time has come. Each one is claimed first, so two workers never send it twice."""
    now = now or utcnow()
    stale = iso(now - dt.timedelta(minutes=10))
    rows = con.execute("""SELECT id, email_kind FROM outbox WHERE channel='email'
                          AND ((email_status='waiting' AND email_next_try <= ?) OR (email_status='sending' AND email_next_try <= ?))
                          ORDER BY id LIMIT ?""", (iso(now), stale, limit)).fetchall()
    done = 0
    for r in rows:
        oid, kind = r[0], r[1] or "playdates"
        # Claim it: only if it's still waiting, or was claimed by a worker that stopped more than 10 minutes ago.
        # (A claim sets email_next_try to now, so a fresh claim by another worker doesn't match here.)
        cur = con.execute("""UPDATE outbox SET email_status='sending', email_next_try=? WHERE id=?
                              AND (email_status='waiting' OR (email_status='sending' AND email_next_try <= ?))""", (iso(now), oid, stale))
        if hasattr(con, "commit"):
            con.commit()
        if getattr(cur, "rowcount", 1) != 1:
            continue  # another worker got it
        deliver(con, oid, kind)
        done += 1
    return done


# ---------------------------------------------------------------- retrying in the background

_last_kick = {"t": 0.0}
_kick_lock = threading.Lock()


def _db_path():
    return os.environ.get("PLAYDATE_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "playdates.db")


def _retry_now():
    con = None
    try:
        con = dbcompat.connect(_db_path(), timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        retry_due(con)
    except Exception as e:  # noqa: BLE001
        mailer._log("FARM MAIL retry run failed: {}: {}".format(type(e).__name__, e))
    finally:
        if con is not None:
            con.close()


def kick():
    """Called on every page: at most every 5 minutes, send any waiting emails whose time has come (in the background)."""
    if os.environ.get("MAIL_PROVIDER", "console").lower() in ("console", "memory"):
        return  # nothing real to retry (and the tests run retries by hand)
    with _kick_lock:
        if time.time() - _last_kick["t"] < 300:
            return
        _last_kick["t"] = time.time()
    threading.Thread(target=_retry_now, daemon=True).start()


# ---------------------------------------------------------------- for the pages

def describe(row):
    """A short label and colour for a Messages row's email status."""
    s = (row.get("email_status") or "") if hasattr(row, "get") else ""
    if row.get("channel") != "email":
        return None
    if s == "sent":
        return {"label": "Sent", "cls": "", "note": ""}
    if s in ("waiting", "sending"):
        try:
            when = dt.datetime.fromisoformat(row.get("email_next_try") or "").astimezone(CENTRAL)
            when = when.strftime("%a %b %-d, %-I:%M %p").replace(":00 ", " ")
        except ValueError:
            when = ""
        return {"label": "Waiting to send", "cls": "amber", "note": row.get("email_note") or "", "when": when}
    if s == "failed":
        return {"label": "Not sent", "cls": "red", "note": row.get("email_note") or ""}
    if s == "off":
        return {"label": "Email off (test mode)", "cls": "grey", "note": row.get("email_note") or ""}
    if s == "console":
        return {"label": "Not emailed (no email service here)", "cls": "grey", "note": ""}
    return {"label": "", "cls": "grey", "note": ""}
