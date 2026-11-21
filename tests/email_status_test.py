"""Email status check: every email the farm writes records what happened to it (sent / waiting / not sent /
email off), waiting ones go out by themselves later, staff can Send again, and nothing is lost by Clear.

    python tests/email_status_test.py                    # SQLite, like the Mac
    DATABASE_URL=... python tests/email_status_test.py   # Postgres, like the live server
"""
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
work = tempfile.mkdtemp(prefix="email-status-")
os.environ.update(PLAYDATE_DB=os.path.join(work, "p.db"), FARM_DATA_DIR=work, FARM_TEST_MODE="1",
                  MAIL_PROVIDER="memory", PLAYDATE_EMAILS="on", ADMIN_PASSWORD="playdate")
if os.environ.get("DATABASE_URL"):
    os.environ["FARM_DB_NAME"] = "emailtest_" + os.path.basename(work).replace("-", "_").lower()
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "playdates"))

import wsgi  # noqa: E402
import dbcompat  # noqa: E402
import farm_mail  # noqa: E402
from werkzeug.test import Client  # noqa: E402

results, failures = [], []


def check(name, ok, detail=""):
    results.append(name)
    if not ok:
        failures.append("{}: {}".format(name, detail))


def con():
    c = dbcompat.connect(os.environ["PLAYDATE_DB"], isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def q(sql, *args):
    c = con()
    out = [dict(r) for r in c.execute(sql, args)]
    c.close()
    return out


def body(r):
    return r.get_data(as_text=True)


M = farm_mail.get_mailer()[0]
staff = Client(wsgi.application)
staff.post("/admin/login", data={"password": "playdate"})
staff.post("/shop/admin/login", data={"password": "playdate"})


BOOKED = []
LAST_PAY = {}


def book(email):
    """A family books and pays: the app writes the confirmation email."""
    g = Client(wsgi.application)
    html = body(g.get("/book?tickets=3"))
    slots = re.findall(r'name="date" value="([^"]+)"><input type="hidden" name="start" value="([^"]+)">', html)
    d, start = slots[len(BOOKED) % len(slots)]  # each booking takes its own arrival time (one group per time)
    BOOKED.append(email)
    code = g.post("/book/hold", data={"date": d, "start": start, "tickets": 3, "under2": 0}).headers["Location"].rstrip("/").split("/")[-1]
    g.post("/checkout/" + code, data={"name": "Fam " + email, "email": email, "phone": "4175550100", "agree": "on"})
    r = g.post("/pay/" + code, data={"card": "4242424242424242"})
    LAST_PAY.update(status=r.status_code, where=r.headers.get("Location", ""), code=code)
    return q("SELECT * FROM outbox WHERE channel='email' AND recipient=? ORDER BY id DESC", email)[0]


# ---------------------------------------------------------------- sent
m1 = book("one@example.com")
check("a confirmation that goes out is marked sent", m1["email_status"] == "sent" and m1["email_sent_at"], m1)
h = body(staff.get("/admin/messages"))
check("Messages shows Sent", ">Sent<" in h and "sent for real" in h, h[:200])

# ---------------------------------------------------------------- Resend's daily limit: waits, goes out after the reset
M.refuse_with = ("limit", "Resend's daily email limit was reached.")
m2 = book("two@example.com")
M.refuse_with = None
nt = dt.datetime.fromisoformat(m2["email_next_try"])
check("refused for Resend's limit: waiting, not failed", m2["email_status"] == "waiting", m2)
check("it waits for Resend's midnight-UTC reset", nt.hour == 0 and nt.date() == (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).date(), nt)
h = body(staff.get("/admin/messages"))
check("Messages shows it waiting, with when, and Send again", "Waiting to send: tries again" in h and "Send again" in h)
c = con()
check("not retried before its time", farm_mail.retry_due(c) == 0 and q("SELECT email_status FROM outbox WHERE id=?", m2["id"])[0]["email_status"] == "waiting")
check("retried once its time comes, and sent", farm_mail.retry_due(c, now=nt + dt.timedelta(minutes=1)) == 1
      and q("SELECT email_status FROM outbox WHERE id=?", m2["id"])[0]["email_status"] == "sent")
check("a second retry run sends nothing twice", farm_mail.retry_due(c, now=nt + dt.timedelta(minutes=2)) == 0
      and [s["to"] for s in M.sent].count("two@example.com") == 1)
c.close()

# ---------------------------------------------------------------- Resend busy or down: tries again every 15 minutes, gives up after 8
M.refuse_with = ("retry", "The email service was busy or down (503).")
m3 = book("three@example.com")
check("a passing problem: waiting, 15 minutes on", m3["email_status"] == "waiting" and "Trying again shortly" in m3["email_note"], m3)
c = con()
t = dt.datetime.fromisoformat(m3["email_next_try"])
for i in range(10):
    farm_mail.retry_due(c, now=t + dt.timedelta(minutes=1 + 16 * i))
r3 = q("SELECT * FROM outbox WHERE id=?", m3["id"])[0]
check("after 8 tries it's marked not sent", r3["email_status"] == "failed" and r3["email_tries"] == farm_mail.MAX_TRIES, r3)
M.refuse_with = None
r = staff.post("/admin/messages/{}/send-again".format(m3["id"]))
check("Send again sends it once things are fixed", q("SELECT email_status FROM outbox WHERE id=?", m3["id"])[0]["email_status"] == "sent", body(r)[:200])
c.close()

# ---------------------------------------------------------------- a hard refusal (bad address): not sent straight away, with the reason
M.refuse_with = ("failed", "The email service refused it (422): invalid to address")
m4 = book("four@example.com")
M.refuse_with = None
check("a hard refusal is marked not sent, with the reason", m4["email_status"] == "failed" and "invalid to address" in m4["email_note"], m4)
h = body(staff.get("/admin/messages"))
check("Messages shows Not sent in red with the reason", 'tag red">Not sent' in h and "invalid to address" in h)

# ---------------------------------------------------------------- Clear keeps what still needs dealing with
M.refuse_with = ("failed", "bad address")
m5 = book("five@example.com")
M.refuse_with = None
staff.post("/admin/messages/clear")
left = {r["recipient"] for r in q("SELECT recipient FROM outbox")}
check("Clear keeps the not-sent ones, clears the rest", "five@example.com" in left and "four@example.com" in left
      and "one@example.com" not in left and "two@example.com" not in left, left)

# ---------------------------------------------------------------- the farm's own daily limit
staff.post("/shop/admin/settings/email", data={"email_daily_limit": "0"})
m6 = book("six@example.com")
check("past the farm's own limit, it waits (with the reason)", m6["email_status"] == "waiting" and "farm's daily email limit" in m6["email_note"], m6)
staff.post("/admin/messages/{}/send-again".format(m6["id"]))
check("Send again by hand isn't held by the farm's limit", q("SELECT email_status FROM outbox WHERE id=?", m6["id"])[0]["email_status"] == "sent")
staff.post("/shop/admin/settings/email", data={"email_daily_limit": "40"})

# ---------------------------------------------------------------- the shop's own switch
c = con()
c.execute("INSERT INTO outbox(booking_id, channel, recipient, subject, body, created_at) VALUES (NULL, 'email', 'buyer@example.com', 'Your order', 'Thanks', ?)",
          (dt.datetime.now().isoformat(),))
sid = c.execute("SELECT MAX(id) FROM outbox").fetchone()[0]
check("shop emails are off until switched on", farm_mail.deliver(c, sid, "shop") == "off"
      and "buyer@example.com" not in [s["to"] for s in M.sent])
c.close()
h = body(staff.get("/shop/admin/settings"))
check("shop Settings shows the email card, connected, with today's count", 'id="email"' in h and "Connected" in h and "Sent today: <b>" in h, h[-1500:])
h = body(staff.get("/shop/admin/emails"))
check("shop Emails page lists it as email off, with Send again", "buyer@example.com" in h and "Email off (test mode)" in h and "Send again" in h)
staff.post("/shop/admin/settings/email", data={"shop_emails": "1", "email_daily_limit": "40"})
c = con()
check("switched on, shop emails go out", farm_mail.deliver(c, sid, "shop") == "sent" and "buyer@example.com" in [s["to"] for s in M.sent])
c.close()
M.refuse_with = ("failed", "bad address")
c = con()
c.execute("INSERT INTO outbox(booking_id, channel, recipient, subject, body, created_at) VALUES (NULL, 'email', 'oops@example', 'Your order', 'Thanks', ?)",
          (dt.datetime.now().isoformat(),))
bad = c.execute("SELECT MAX(id) FROM outbox").fetchone()[0]
farm_mail.deliver(c, bad, "shop")
c.close()
M.refuse_with = None
h = body(staff.get("/shop/admin"))
check("shop Home warns about emails not sent", "1 email waiting or not sent" in h, h[:400])
staff.post("/shop/admin/emails/{}/send-again".format(bad))
check("shop Send again works", q("SELECT email_status FROM outbox WHERE id=?", bad)[0]["email_status"] == "sent"
      and "waiting or not sent" not in body(staff.get("/shop/admin")))

# ---------------------------------------------------------------- how Resend's real refusals are sorted
import mailer  # noqa: E402
check("Resend's daily limit (429 daily_quota_exceeded) means wait", mailer.classify_refusal(429, '{"name":"daily_quota_exceeded"}')[0] == "limit")
check("Resend's monthly limit means wait too", mailer.classify_refusal(429, '{"name":"monthly_quota_exceeded"}')[0] == "limit")
check("too many at once (429 rate limit) means try again soon", mailer.classify_refusal(429, '{"name":"rate_limit_exceeded"}')[0] == "retry")
check("Resend down (5xx) means try again soon", mailer.classify_refusal(503, "")[0] == "retry")
check("a bad address (422) means not sent", mailer.classify_refusal(422, '{"message":"Invalid `to` field"}')[0] == "failed")
check("a bad key (401/403) means not sent", mailer.classify_refusal(401, "")[0] == "failed" and mailer.classify_refusal(403, "")[0] == "failed")

# ---------------------------------------------------------------- strangers can't press Send again
stranger = Client(wsgi.application)
before = len(M.sent)
r1 = stranger.post("/admin/messages/{}/send-again".format(m5["id"]))
r2 = stranger.post("/shop/admin/emails/{}/send-again".format(m5["id"]))
check("Send again needs a login: nothing sent, sent to log in", len(M.sent) == before
      and q("SELECT email_status FROM outbox WHERE id=?", m5["id"])[0]["email_status"] == "failed"
      and "login" in r1.headers.get("Location", "") and "login" in r2.headers.get("Location", ""),
      (r1.status_code, r1.headers.get("Location"), r2.status_code, r2.headers.get("Location")))

# ---------------------------------------------------------------- two server workers reach for the same waiting email at once
M.refuse_with = ("limit", "limit")
m8 = book("eight@example.com")
M.refuse_with = None
due = dt.datetime.fromisoformat(m8["email_next_try"]) + dt.timedelta(minutes=1)


class WorkerB:
    """Worker B's connection. Right after B has looked up what's due, worker A claims the same email."""
    def __init__(self, real):
        self.real = real

    def execute(self, sql, args=()):
        out = self.real.execute(sql, args)
        if sql.strip().startswith("SELECT id, email_kind FROM outbox"):
            rows = out.fetchall()
            a = con()
            a.execute("UPDATE outbox SET email_status='sending', email_next_try=? WHERE id=?", (farm_mail.iso(due), m8["id"]))
            a.close()
            return type("R", (), {"fetchall": lambda self: rows})()
        return out

    def commit(self):
        if hasattr(self.real, "commit"):
            self.real.commit()


before = [s["to"] for s in M.sent].count("eight@example.com")
b = con()
sent_by_b = farm_mail.retry_due(WorkerB(b), now=due)
b.close()
check("two workers never send the same email twice", sent_by_b == 0 and [s["to"] for s in M.sent].count("eight@example.com") == before,
      (sent_by_b, q("SELECT email_status FROM outbox WHERE id=?", m8["id"])))

# ---------------------------------------------------------------- an email problem never breaks a booking
M.refuse_with = ("failed", "anything")
real_send = M.send_result
M.send_result = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
m7 = book("seven@example.com")
M.send_result = real_send
M.refuse_with = None
check("a crash while emailing doesn't stop the booking", m7 and LAST_PAY["status"] == 302 and "/done/" in LAST_PAY["where"]
      and q("SELECT status FROM bookings WHERE code=?", LAST_PAY["code"])[0]["status"] == "paid", LAST_PAY)

summary = {"database": "postgres" if os.environ.get("DATABASE_URL") else "sqlite", "passed": len(results) - len(failures),
           "total": len(results), "failures": failures}
print(json.dumps(summary, indent=1))
sys.exit(1 if failures else 0)
