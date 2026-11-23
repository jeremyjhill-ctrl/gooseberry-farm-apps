"""Personal logins check: walks through setting up logins the way Jeremy would (owner first, then
Staci with playdates + shop, then Mollie with bakery only) and checks every lock along the way.

    python tests/login_test.py                    # SQLite, like the Mac
    DATABASE_URL=... python tests/login_test.py   # Postgres, like the live server

Uses a brand-new throwaway database every run.
"""
import json
import os
import re
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
os.environ["FARM_TEST_MODE"] = "1"
os.environ["MAIL_PROVIDER"] = "memory"
work = tempfile.mkdtemp(prefix="login-test-")
os.environ["PLAYDATE_DB"] = os.path.join(work, "playdates.db")
os.environ["FARM_DATA_DIR"] = work
os.environ["ADMIN_PASSWORD"] = "playdate"
os.environ["BAKERY_PASSWORD"] = "mollie-old-pw"
if os.environ.get("DATABASE_URL"):
    os.environ["FARM_DB_NAME"] = "logintest_" + os.path.basename(work).replace("-", "_").lower()
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "playdates"))

import mailer  # noqa: E402

SENT = []
mailer.MemoryMail.send = lambda self, to, subject, text, html=None: SENT.append({"to": to, "subject": subject, "text": text}) or True

import wsgi  # noqa: E402
import dbcompat  # noqa: E402
import farm_auth  # noqa: E402
from werkzeug.test import Client  # noqa: E402

results, failures = [], []


def check(name, ok, detail=""):
    results.append({"step": name, "ok": bool(ok)})
    if not ok:
        failures.append("{}: {}".format(name, detail))


def q(sql, *args):
    con = dbcompat.connect(os.environ["PLAYDATE_DB"])
    con.row_factory = sqlite3.Row
    out = [dict(r) for r in con.execute(sql, args)]
    con.close()
    return out


def body(r):
    return r.get_data(as_text=True)


def loc(r):
    return r.headers.get("Location", "")


def new():
    return Client(wsgi.application)


def login(client, email, pw, nxt="/admin"):
    return client.post("/account/login", data={"email": email, "password": pw, "next": nxt})


# ---------------------------------------------------------------- before anyone has a login: nothing changes
jer = new()
check("shared password still works for playdates", loc(jer.post("/admin/login", data={"password": "playdate"})).endswith("/admin"))
check("playdate admin opens", jer.get("/admin").status_code == 200)
h = body(jer.get("/admin"))
check("admin bar shows both farm admins", "/shop/admin" in h and "Playdates" in h and "Farm shop" in h, h[:400])
check("admin bar offers to set up logins", "Set up personal logins" in h)
check("admin bar hides the bakery from the shared password", "/bakery/#admin" not in h)
check("shared password still works for the shop", jer.post("/shop/admin/login", data={"password": "playdate"}).status_code == 302
      and jer.get("/shop/admin").status_code == 200)
mol = new()
check("Mollie's old password still works", mol.post("/bakery/api/admin/login", json={"password": "mollie-old-pw"}).status_code == 200
      and mol.get("/bakery/api/admin/state").status_code == 200)
check("bakery login screen asks for password only", new().get("/bakery/api/admin/login-mode").get_json() == {"accounts": False})
check("strangers can't open Logins", "/account/login" in loc(new().get("/account/logins")))

# ---------------------------------------------------------------- Jeremy makes the owner login
r = jer.get("/account/logins")
check("Logins page opens in setup mode", r.status_code == 200 and "Start with your own login" in body(r))
r = jer.post("/account/logins", data={"action": "add", "name": "Jeremy", "email": "Jeremy@Example.com", "password": "short"})
check("short passwords refused", "at least 10 characters" in body(r) and not q("SELECT id FROM farm_users"))
r = jer.post("/account/logins", data={"action": "add", "name": "Jeremy", "email": "Jeremy@Example.com", "password": "barn-clover-meadow",
                                      "can_bakery": "1", "can_playdates": "0"})
u = q("SELECT * FROM farm_users")
check("owner login made", len(u) == 1 and u[0]["email"] == "jeremy@example.com", u)
check("owner gets every box", u and all(u[0][k] for k in ("can_playdates", "can_shop", "can_bakery", "can_logins")) and not u[0]["must_change"], u)
check("password stored as a hash", u and "barn-clover-meadow" not in u[0]["pw_hash"] and len(u[0]["pw_hash"]) > 40)
cookie = r.headers.get("Set-Cookie", "")
check("owner logged in right away", "farm_login=" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie, cookie)
check("only the token's hash is kept", len(q("SELECT token_hash FROM farm_logins")) == 1
      and re.search(r"farm_login=([^;]+)", cookie).group(1) not in q("SELECT token_hash FROM farm_logins")[0]["token_hash"])
check("playdate admin works with the personal login", jer.get("/admin").status_code == 200)
h = body(jer.get("/admin"))
check("bar shows who's signed in", "Signed in as <b>Jeremy</b>" in h)
check("bar links all three admins + Logins", all(x in h for x in ("/shop/admin", "/bakery/#admin", "/account/logins")), h[:600])
check("shop admin works with the same login", jer.get("/shop/admin").status_code == 200)

# ---------------------------------------------------------------- the shared password is retired for playdates + shop
old = new()
r = old.post("/admin/login", data={"password": "playdate"})
check("shared password no longer opens playdates", "/account/login" in loc(r) and old.get("/admin").status_code == 302, loc(r))
old.post("/shop/admin/login", data={"password": "playdate"})
check("shared password no longer opens the shop", old.get("/shop/admin").status_code == 302)
check("playdate login link goes to the personal login", "/account/login" in loc(new().get("/admin/login")))
check("shop login link goes to the personal login", "/account/login" in loc(new().get("/shop/admin/login")))
check("the old 'admin' cookie alone isn't enough", jer.get("/admin").status_code == 200)  # sanity, below uses a stale session
stale = new()
stale.post("/admin/login", data={"password": "playdate"})
check("someone logged in with the shared password is logged out", stale.get("/admin").status_code == 302)

# ---------------------------------------------------------------- Jeremy adds Staci and Mollie
r = jer.post("/account/logins", data={"action": "add", "name": "Staci", "email": "staci@example.com", "password": "pumpkin-gate-hay-42",
                                      "can_playdates": "1", "can_shop": "1"})
check("Staci added", "Added Staci" in body(r))
r = jer.post("/account/logins", data={"action": "add", "name": "Mollie", "email": "mollie@example.com", "password": "honey-biscuit-12",
                                      "can_bakery": "1"})
check("Mollie added", "Added Mollie" in body(r))
r = jer.post("/account/logins", data={"action": "add", "name": "Again", "email": "STACI@example.com", "password": "pumpkin-gate-hay-42"})
check("duplicate email refused", "already has that email" in body(r))
staci = q("SELECT * FROM farm_users WHERE email='staci@example.com'")[0]
mollie = q("SELECT * FROM farm_users WHERE email='mollie@example.com'")[0]
check("new people must choose their own password", staci["must_change"] and mollie["must_change"])
check("Staci's boxes", staci["can_playdates"] and staci["can_shop"] and not staci["can_bakery"] and not staci["can_logins"])
check("Mollie's boxes", mollie["can_bakery"] and not mollie["can_playdates"] and not mollie["can_shop"] and not mollie["can_logins"])

# ---------------------------------------------------------------- Staci logs in for the first time
st = new()
r = login(st, "staci@example.com", "wrong-password-here")
check("wrong password refused", r.status_code == 200 and "didn&#39;t match" in body(r))
r = login(st, " Staci@Example.com ", "pumpkin-gate-hay-42", "/shop/admin/orders")
check("first login asks her to choose a password", "/account/password" in loc(r) and "first=1" in loc(r), loc(r))
r = st.post(loc(r), data={"password": "my-own-goat-password", "password2": "my-own-goat-password"})
check("then sends her where she was going", loc(r).endswith("/shop/admin/orders"), loc(r))
check("her new password works", not q("SELECT must_change FROM farm_users WHERE id=?", staci["id"])[0]["must_change"]
      and login(new(), "staci@example.com", "my-own-goat-password").status_code == 302)
check("Staci can use playdates", st.get("/admin").status_code == 200)
check("Staci can use the shop", st.get("/shop/admin/products").status_code == 200)
h = body(st.get("/admin"))
check("Staci's bar has no bakery or Logins", "/bakery/#admin" not in h and "/account/logins" not in h and "/shop/admin" in h)
r = st.get("/account/logins")
check("Staci can't open Logins", "Not your page" in body(r))
r = st.post("/account/logins", data={"action": "save", "id": str(staci["id"]), "can_playdates": "1", "can_shop": "1", "can_logins": "1"})
check("Staci can't give herself Manage logins", not q("SELECT can_logins FROM farm_users WHERE id=?", staci["id"])[0]["can_logins"])
check("Staci can't reach the bakery admin", st.get("/bakery/api/admin/state").status_code == 401)
r = st.post("/bakery/api/admin/login", json={"email": "staci@example.com", "password": "my-own-goat-password"})
check("Staci can't log in to the bakery", r.status_code in (401, 403), r.status_code)

# ---------------------------------------------------------------- Mollie: bakery only, on her own site
check("bakery login screen now asks for email", new().get("/bakery/api/admin/login-mode").get_json() == {"accounts": True})
old_m = new()
check("Mollie's old shared password is retired", old_m.post("/bakery/api/admin/login", json={"password": "mollie-old-pw"}).status_code == 401)
check("someone logged in with the old bakery password is logged out", mol.get("/bakery/api/admin/state").status_code == 401)
m = new()
r = m.post("/bakery/api/admin/login", json={"email": "mollie@example.com", "password": "honey-biscuit-12"})
check("Mollie logs in to the bakery", r.status_code == 200 and r.get_json().get("mustChange") is True, r.get_data(as_text=True))
check("bakery admin opens for Mollie", m.get("/bakery/api/admin/state").status_code == 200)
r = m.get("/bakery/account/password?first=1&next=/bakery/%23admin")
check("Mollie's password page is in her branding", "Mollie&#39;s Farmhouse Bakery" in body(r) and "Gooseberry" not in body(r))
r = m.post("/bakery/account/password", data={"first": "1", "next": "/bakery/", "password": "mollies-own-pw-1", "password2": "mollies-own-pw-1"})
check("Mollie chooses her own password", r.status_code == 302 and not q("SELECT must_change FROM farm_users WHERE id=?", mollie["id"])[0]["must_change"])
check("Mollie can't use playdates", m.get("/admin").status_code == 302)
check("Mollie can't use the shop", m.get("/shop/admin").status_code == 302)
check("bakery has no Logins page", m.get("/bakery/account/logins").status_code == 404)
check("Mollie's bakery login says nothing about the farm", "Gooseberry" not in body(new().get("/bakery/account/forgot")))
r = m.post("/bakery/api/admin/logout")
check("bakery log out ends the login", r.status_code == 200 and m.get("/bakery/api/admin/state").status_code == 401)

# ---------------------------------------------------------------- forgot password
SENT.clear()
r = new().post("/account/forgot", data={"email": "nobody@example.com"})
check("forgot: same answer for unknown emails", "If that email" in body(r) and not SENT)
r = new().post("/account/forgot", data={"email": "staci@example.com"})
check("forgot: emails a link", len(SENT) == 1 and SENT[0]["to"] == "staci@example.com" and "/account/reset/" in SENT[0]["text"], SENT)
link = re.search(r"(/account/reset/[\w-]+)", SENT[0]["text"]).group(1) if SENT else "/account/reset/x"
fresh = new()
r = fresh.post(link, data={"password": "brand-new-pw-123", "password2": "nope-different"})
check("reset: mismatched passwords refused", "don&#39;t match" in body(r))
r = fresh.post(link, data={"password": "brand-new-pw-123", "password2": "brand-new-pw-123"})
check("reset: saves and logs her in", r.status_code == 302 and fresh.get("/admin").status_code == 200)
check("reset: logs out her other devices", st.get("/admin").status_code == 302)
check("reset: the link only works once", "expired" in body(new().get(link)))
check("reset: old password gone", login(new(), "staci@example.com", "my-own-goat-password").status_code == 200)
SENT.clear()
new().post("/bakery/account/forgot", data={"email": "mollie@example.com"})
check("bakery forgot link stays on the bakery", SENT and "/bakery/account/reset/" in SENT[0]["text"], SENT)

# ---------------------------------------------------------------- changing a password while logged in
r = fresh.post("/account/password", data={"current": "wrong", "password": "another-pw-456", "password2": "another-pw-456"})
check("change password needs the current one", "wasn&#39;t right" in body(r))
r = fresh.post("/account/password", data={"current": "brand-new-pw-123", "password": "another-pw-456", "password2": "another-pw-456"})
check("change password works", r.status_code == 302 and login(new(), "staci@example.com", "another-pw-456").status_code == 302)

# ---------------------------------------------------------------- owner controls
r = jer.post("/account/logins", data={"action": "save", "id": str(staci["id"]), "can_playdates": "1"})
check("owner untick takes effect right away", fresh.get("/shop/admin").status_code == 302 and fresh.get("/admin").status_code == 200)
jer.post("/account/logins", data={"action": "save", "id": str(staci["id"]), "can_playdates": "1", "can_shop": "1"})
check("owner re-tick takes effect right away", fresh.get("/shop/admin").status_code == 200)
me = q("SELECT id FROM farm_users WHERE email='jeremy@example.com'")[0]["id"]
jer.post("/account/logins", data={"action": "save", "id": str(me), "can_playdates": "1"})
check("owner can't lock himself out of Logins", q("SELECT can_logins FROM farm_users WHERE id=?", me)[0]["can_logins"] == 1)
r = jer.post("/account/logins", data={"action": "off", "id": str(me)})
check("owner can't turn himself off", "can&#39;t turn off your own" in body(r) and q("SELECT active FROM farm_users WHERE id=?", me)[0]["active"])
jer.post("/account/logins", data={"action": "off", "id": str(staci["id"])})
check("turning someone off logs them out everywhere", fresh.get("/admin").status_code == 302 and not q("SELECT 1 AS x FROM farm_logins WHERE user_id=?", staci["id"]))
check("turned-off people can't log in", login(new(), "staci@example.com", "another-pw-456").status_code == 200)
jer.post("/account/logins", data={"action": "on", "id": str(staci["id"])})
check("turn back on", login(new(), "staci@example.com", "another-pw-456").status_code == 302)
SENT.clear()
r = jer.post("/account/logins", data={"action": "reset", "id": str(mollie["id"])})
check("owner can email a password link", "Emailed Mollie" in body(r) and SENT and SENT[0]["to"] == "mollie@example.com")
r = jer.post("/account/logins", data={"action": "temp", "id": str(mollie["id"]), "temp": "temporary-pw-99"})
check("owner can set a temporary password", "temporary password" in body(r) and q("SELECT must_change FROM farm_users WHERE id=?", mollie["id"])[0]["must_change"])
r = jer.get("/account/logins")
check("Logins page lists everyone", all(x in body(r) for x in ("jeremy@example.com", "staci@example.com", "mollie@example.com")))

# ---------------------------------------------------------------- one login for everything on the farm's address
from urllib.parse import unquote  # noqa: E402
os.environ["BAKERY_HOSTS"] = "orders.molliesbakery.test"
jer.post("/account/logins", data={"action": "save", "id": str(me), "can_playdates": "1", "can_shop": "1", "can_bakery": "1", "can_logins": "1"})
h = body(jer.get("/admin"))
check("the bar's Bakery link stays on the farm address (no second login)", "http://localhost/bakery/#admin" in h and "molliesbakery.test" not in h,
      re.findall(r'href="([^"]*bakery[^"]*)"', h))
check("the bakery admin opens with the farm login", jer.get("/bakery/api/admin/state").status_code == 200)
bk = body(jer.get("/bakery/"))
check("the bakery's page on the farm address shows staff the farm bar", "Signed in as <b>Jeremy</b>" in bk and "/shop/admin" in bk)
check("...customers don't see it", "farmbar" not in body(new().get("/bakery/")))
own = Client(wsgi.application)
r = own.post("/api/admin/login", json={"email": "jeremy@example.com", "password": "barn-clover-meadow"}, base_url="http://bakery.molliesbakery.test")
check("on Mollie's own address the same account works", r.status_code == 200 and own.get("/api/admin/state", base_url="http://bakery.molliesbakery.test").status_code == 200,
      (r.status_code, body(r)[:200]))
check("...and her pages there carry no farm bar", "farmbar" not in body(own.get("/", base_url="http://bakery.molliesbakery.test")))
r = new().post("/account/login", data={"email": "mollie@example.com", "password": "temporary-pw-99"})
check("Mollie logging in on the farm address lands in the bakery admin", "/bakery/#admin" in unquote(loc(r)), loc(r))
r = new().post("/account/login", data={"email": "staci@example.com", "password": "another-pw-456"})
check("Staci lands in the playdate admin", loc(r).endswith("/admin"), loc(r))
jer.post("/account/logins", data={"action": "add", "name": "Sam", "email": "sam@example.com", "password": "shop-only-pw-77", "can_shop": "1"})
r = new().post("/account/login", data={"email": "sam@example.com", "password": "shop-only-pw-77"})
check("someone with only the shop box lands in the shop admin", "/shop/admin" in unquote(loc(r)), loc(r))

# ---------------------------------------------------------------- deleting a login
jer.post("/account/logins", data={"action": "add", "name": "Helper", "email": "helper@example.com", "password": "helper-temp-pw-1", "can_playdates": "1"})
helper = q("SELECT * FROM farm_users WHERE email='helper@example.com'")[0]
hp = new()
login(hp, "helper@example.com", "helper-temp-pw-1")
hp.post("/account/password", data={"first": "1", "next": "/admin", "password": "helper-own-pw-22", "password2": "helper-own-pw-22"})
check("(setup) the helper is logged in", hp.get("/admin").status_code == 200)
check("the Logins page offers Delete", "Delete…" in body(jer.get("/account/logins")))
r = jer.post("/account/logins", data={"action": "delete", "id": str(helper["id"])})
check("delete needs the confirm box", q("SELECT id FROM farm_users WHERE id=?", helper["id"]) and "Tick the box" in body(r))
r = new().post("/account/logins", data={"action": "delete", "id": str(helper["id"]), "confirm_delete": "1"})
check("someone not logged in can't delete", q("SELECT id FROM farm_users WHERE id=?", helper["id"]))
r = jer.post("/account/logins", data={"action": "delete", "id": str(helper["id"]), "confirm_delete": "1"})
check("the owner deletes a login", not q("SELECT id FROM farm_users WHERE id=?", helper["id"]) and "Deleted Helper" in body(r))
check("...which logs them out everywhere", hp.get("/admin").status_code == 302 and not q("SELECT 1 AS x FROM farm_logins WHERE user_id=?", helper["id"]))
check("...and they can't log in again", login(new(), "helper@example.com", "helper-own-pw-22").status_code == 200)
r = jer.post("/account/logins", data={"action": "add", "name": "Helper", "email": "helper@example.com", "password": "helper-again-pw-3"})
check("the email can be used again for a new login", len(q("SELECT id FROM farm_users WHERE email='helper@example.com'")) == 1)
r = jer.post("/account/logins", data={"action": "delete", "id": str(me), "confirm_delete": "1"})
check("the owner can't delete himself", q("SELECT id FROM farm_users WHERE id=?", me) and "delete your own" in body(r))
st2 = new()
login(st2, "staci@example.com", "another-pw-456")
r = st2.post("/account/logins", data={"action": "delete", "id": str(mollie["id"]), "confirm_delete": "1"})
check("someone without Manage logins can't delete", q("SELECT id FROM farm_users WHERE id=?", mollie["id"]))

# ---------------------------------------------------------------- log out, lockout, redirects
r = jer.get("/admin/logout")
check("playdate log out goes through the personal log out", "/account/logout" in loc(r))
r = jer.get(loc(r))
check("log out clears the login", "farm_login=;" in r.headers.get("Set-Cookie", "") and jer.get("/admin").status_code == 302)
check("login page hides the shared-password hint once logins exist", "shared password" not in body(new().get("/account/login")))
r = login(new(), "x@example.com", "pw-whatever", "//evil.example.com/")
check("login never sends people to another site", not loc(new().post("/account/login", data={"email": "jeremy@example.com", "password": "barn-clover-meadow", "next": "//evil.example.com"})).startswith("//"))
farm_auth.LOGIN_TRIES.clear()
lock = new()
for _ in range(8):
    lock.post("/account/login", data={"email": "jeremy@example.com", "password": "wrong-wrong-wrong"})
r = lock.post("/account/login", data={"email": "jeremy@example.com", "password": "barn-clover-meadow"})
check("too many wrong tries pauses logins", "Too many tries" in body(r))
farm_auth.LOGIN_TRIES.clear()

# ---------------------------------------------------------------- the booking page and customer pages are untouched
check("customers still book without logging in", new().get("/book").status_code == 200)
check("shop customer page untouched", new().get("/shop/").status_code in (200, 503))
check("bakery customer page untouched", new().get("/bakery/").status_code == 200)

summary = {"database": "postgres" if os.environ.get("DATABASE_URL") else "sqlite", "passed": len(results) - len(failures),
           "total": len(results), "failures": failures}
print(json.dumps(summary, indent=1))
sys.exit(1 if failures else 0)
