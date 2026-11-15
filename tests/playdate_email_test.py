"""Playdate email check: with real email switched on, a booking sends its confirmation email;
switched off (the default), nothing is sent. Also: half-finished mail settings stop the server.

    python tests/playdate_email_test.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
results, failures = [], []


def check(name, ok, detail=""):
    results.append(name)
    if not ok:
        failures.append("{}: {}".format(name, detail))


if len(sys.argv) > 1 and sys.argv[1] == "--book":
    # Child run: book and pay once, then report what the mailer captured.
    work = tempfile.mkdtemp(prefix="pd-mail-")
    os.environ.update(PLAYDATE_DB=os.path.join(work, "p.db"), FARM_DATA_DIR=work, FARM_TEST_MODE="1")
    sys.path.insert(0, ROOT)
    import wsgi
    import farm_mail
    from werkzeug.test import Client
    c = Client(wsgi.application)
    html = c.get("/book?tickets=3").get_data(as_text=True)
    d, start = re.search(r'name="date" value="([^"]+)"><input type="hidden" name="start" value="([^"]+)">', html).groups()
    code = c.post("/book/hold", data={"date": d, "start": start, "tickets": 3, "under2": 0}).headers["Location"].rstrip("/").split("/")[-1]
    c.post("/checkout/" + code, data={"name": "Mail Test", "email": "family@example.com", "phone": "4175550100", "agree": "on"})
    c.post("/pay/" + code, data={"card": "4242424242424242"})
    print(json.dumps({"enabled": farm_mail.ENABLED, "sent": farm_mail.MAILER.sent if farm_mail.MAILER else []}))
    sys.exit(0)


def child(env_extra):
    env = dict(os.environ)
    for k in ("DATABASE_URL", "PLAYDATE_EMAILS", "MAIL_PROVIDER", "PLAYDATE_MAIL_FROM", "RESEND_API_KEY"):
        env.pop(k, None)
    env.update(env_extra)
    r = subprocess.run([sys.executable, __file__, "--book"], env=env, capture_output=True, text=True)
    return r


r = child({"PLAYDATE_EMAILS": "on", "MAIL_PROVIDER": "memory"})
out = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {"sent": []}
check("booking runs with email on", r.returncode == 0, r.stderr[-400:])
check("confirmation email sent to the family", [m["to"] for m in out["sent"]] == ["family@example.com"], out["sent"])
check("it's the ticket email", out["sent"] and "Farm Baby Playdate" in out["sent"][0]["subject"] and "/t/" in out["sent"][0]["text"])

r = child({})
out = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {}
check("email off by default: nothing sent", r.returncode == 0 and out.get("enabled") is False and out.get("sent") == [], r.stderr[-300:])

r = child({"PLAYDATE_EMAILS": "on", "MAIL_PROVIDER": "resend"})
check("half-set-up email stops the server instead of failing quietly", r.returncode != 0 and "RESEND_API_KEY" in r.stderr, r.stderr[-300:])

r = child({"PLAYDATE_EMAILS": "on", "MAIL_PROVIDER": "resend", "RESEND_API_KEY": "x", "PLAYDATE_MAIL_FROM": "Gooseberry Bridge Farm <hello@gooseberrybridge.com"})
check("a From address missing its > is caught at start", r.returncode != 0 and "PLAYDATE_MAIL_FROM" in r.stderr, r.stderr[-300:])

print(json.dumps({"passed": len(results) - len(failures), "total": len(results), "failures": failures}))
sys.exit(1 if failures else 0)
