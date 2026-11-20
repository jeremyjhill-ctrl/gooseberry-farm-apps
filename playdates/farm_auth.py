"""Personal logins for the farm apps: each person has an email and password, and the owner
ticks which admins they can use (Playdates, Farm shop, Bakery, Manage logins).

- Accounts live in the farm database (next to the bookings). Passwords are stored as one-way
  hashes. A login is a random token in a cookie ("farm_login"); the server keeps only a hash of
  it, so logging out (or the owner turning someone off) ends that login everywhere at once.
- Access is checked on every page, so a changed checkbox applies immediately.
- Switching over: each app keeps accepting its old shared password until at least one active
  person has a login for that app. Nobody is locked out on the day this ships.
- The farm apps (playdates + shop) share one login; on gooseberrybridge.com addresses the cookie
  covers every subdomain. The bakery runs on Mollie's own address and has its own login page
  with her branding (same accounts, nothing farm-branded).

Pages added to an app by `add_pages(app, ...)`, all under /account/:
  login, logout, forgot, reset/<token>, password (change my password), logins (owner only).
"""
import datetime as dt
import hashlib
import os
import secrets
import sqlite3
import time
from zoneinfo import ZoneInfo

from markupsafe import Markup, escape

import dbcompat

TZ = ZoneInfo("America/Chicago")
COOKIE = "farm_login"
LOGIN_DAYS = 30
APPS = [("playdates", "Playdates"), ("shop", "Farm shop"), ("bakery", "Bakery"), ("logins", "Manage logins")]

SCHEMA = """
CREATE TABLE IF NOT EXISTS farm_users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL,
  name TEXT NOT NULL,
  pw_hash TEXT NOT NULL,
  can_playdates INTEGER DEFAULT 0,
  can_shop INTEGER DEFAULT 0,
  can_bakery INTEGER DEFAULT 0,
  can_logins INTEGER DEFAULT 0,
  active INTEGER DEFAULT 1,
  must_change INTEGER DEFAULT 0,
  created_at TEXT, last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS farm_logins (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  created_at TEXT, expires_at TEXT
);
CREATE TABLE IF NOT EXISTS farm_resets (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  expires_at TEXT, used INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_farm_users_email ON farm_users(email)
"""


def db_path():
    # Same file the playdate app uses (wsgi.py sets PLAYDATE_DB; on Render everything is one Postgres database).
    return os.environ.get("PLAYDATE_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "playdates.db")


def connect():
    con = dbcompat.connect(db_path(), timeout=15, isolation_level=None)
    con.row_factory = sqlite3.Row
    return con


def ensure_schema():
    con = connect()
    try:
        con.executescript(SCHEMA)
    finally:
        con.close()


def now():
    return dt.datetime.now(TZ).replace(microsecond=0)


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(pw):
    from werkzeug.security import generate_password_hash
    return generate_password_hash(pw)


def check_password(pw_hash, pw):
    from werkzeug.security import check_password_hash
    try:
        return check_password_hash(pw_hash, pw)
    except Exception:  # noqa: BLE001
        return False


def password_problem(pw):
    if len(pw or "") < 10:
        return "Passwords need at least 10 characters. A few words strung together works well."
    return ""


# ---------------------------------------------------------------- reading accounts

def q(sql, *args):
    con = connect()
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def run(sql, *args):
    con = connect()
    try:
        cur = con.execute(sql, args)
        return cur.lastrowid
    finally:
        con.close()


def users():
    return q("SELECT * FROM farm_users ORDER BY active DESC, name, id")


def user_by_id(uid):
    r = q("SELECT * FROM farm_users WHERE id=?", uid)
    return r[0] if r else None


def user_by_email(email):
    r = q("SELECT * FROM farm_users WHERE email=?", (email or "").strip().lower())
    return r[0] if r else None


def accounts_for(app_key):
    """True once at least one active person can use this app: from then on it needs personal logins."""
    return bool(q("SELECT 1 AS x FROM farm_users WHERE active=1 AND can_{}=1".format(app_key)))


def any_accounts():
    return bool(q("SELECT 1 AS x FROM farm_users WHERE active=1"))


def owners():
    return q("SELECT * FROM farm_users WHERE active=1 AND can_logins=1")


# ---------------------------------------------------------------- logging in and out

LOGIN_TRIES = {}  # (ip) -> recent failure times


def too_many(ip):
    recent = [t for t in LOGIN_TRIES.get(ip, []) if time.time() - t < 600]
    LOGIN_TRIES[ip] = recent
    return len(recent) >= 8


def authenticate(email, password, ip):
    """Returns (user, error)."""
    if too_many(ip):
        return None, "Too many tries. Please wait 10 minutes and try again."
    u = user_by_email(email)
    if not u or not u["active"] or not check_password(u["pw_hash"], password or ""):
        LOGIN_TRIES.setdefault(ip, []).append(time.time())
        return None, "That email and password didn't match."
    LOGIN_TRIES.pop(ip, None)
    return u, ""


def start_login(user):
    token = secrets.token_urlsafe(32)
    run("INSERT INTO farm_logins(token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
        _hash(token), user["id"], now().isoformat(), (now() + dt.timedelta(days=LOGIN_DAYS)).isoformat())
    run("UPDATE farm_users SET last_login_at=? WHERE id=?", now().isoformat(), user["id"])
    return token


def end_login(token):
    if token:
        run("DELETE FROM farm_logins WHERE token_hash=?", _hash(token))


def end_all_logins(uid):
    run("DELETE FROM farm_logins WHERE user_id=?", uid)


def user_for_token(token):
    if not token:
        return None
    r = q("""SELECT u.* FROM farm_logins l JOIN farm_users u ON u.id = l.user_id
             WHERE l.token_hash = ? AND l.expires_at > ? AND u.active = 1""", _hash(token), now().isoformat())
    return r[0] if r else None


def current_user(request):
    """The person logged in on this request (cached for the request), or None."""
    from flask import g
    if "_farm_user" not in g:
        g._farm_user = user_for_token(request.cookies.get(COOKIE))
    return g._farm_user


def can(user, app_key):
    return bool(user and user.get("can_" + app_key))


def cookie_domain(request):
    if os.environ.get("FARM_COOKIE_DOMAIN"):
        return os.environ["FARM_COOKIE_DOMAIN"]
    host = request.host.split(":")[0].lower()
    if host.endswith(".gooseberrybridge.com"):
        return "gooseberrybridge.com"  # one login for booking. and shop.
    return None


def set_cookie(resp, request, token):
    secure = request.is_secure or request.headers.get("X-Forwarded-Proto", "") == "https"
    resp.set_cookie(COOKIE, token, max_age=LOGIN_DAYS * 86400, httponly=True, samesite="Lax", secure=secure,
                    domain=cookie_domain(request), path="/")
    return resp


def clear_cookie(resp, request):
    resp.delete_cookie(COOKIE, domain=cookie_domain(request), path="/")
    return resp


# ---------------------------------------------------------------- resets

def make_reset(user, hours=1):
    token = secrets.token_urlsafe(24)
    run("INSERT INTO farm_resets(token_hash, user_id, expires_at, used) VALUES (?,?,?,0)",
        _hash(token), user["id"], (now() + dt.timedelta(hours=hours)).isoformat())
    return token


def reset_user(token):
    r = q("""SELECT u.*, r.token_hash AS rh FROM farm_resets r JOIN farm_users u ON u.id = r.user_id
             WHERE r.token_hash = ? AND r.used = 0 AND r.expires_at > ? AND u.active = 1""", _hash(token or ""), now().isoformat())
    return r[0] if r else None


def set_password(uid, pw, must_change=False):
    run("UPDATE farm_users SET pw_hash=?, must_change=? WHERE id=?", hash_password(pw), 1 if must_change else 0, uid)


# ---------------------------------------------------------------- links between the admins

def admin_urls(request):
    """Where each admin lives, as seen from the address this request came in on."""
    host = request.host.split(":")[0].lower()
    root = request.host_url  # e.g. https://gooseberry-farm-apps.onrender.com/
    bakery_hosts = [h.strip() for h in os.environ.get("BAKERY_HOSTS", "").split(",") if h.strip()]
    if host.startswith("shop."):
        playdates = "https://booking." + host[len("shop."):] + "/admin"
        shop = root + "admin"
    elif host.startswith("booking."):
        playdates = root + "admin"
        shop = os.environ.get("SHOP_URL", "").rstrip("/") + "/admin" if os.environ.get("SHOP_URL") else root + "shop/admin"
    else:
        playdates = root + "admin"
        shop = root + "shop/admin"
    bakery = ("https://{}/#admin".format(bakery_hosts[0]) if bakery_hosts and host not in bakery_hosts else root + "bakery/#admin")
    return {"playdates": playdates, "shop": shop, "bakery": bakery}


def admin_bar(request, here, logins_url, account_url, logout_url, legacy=False):
    """The thin bar across the top of the farm admins: who's logged in and the admins they can use."""
    u = current_user(request)
    links = []
    urls = admin_urls(request)
    for key, label in APPS[:3]:
        if (u and can(u, key)) or (legacy and key in ("playdates", "shop")):  # the shared password covers both farm admins
            style = "color:#fff;font-weight:800;text-decoration:none" if key == here else "color:#D9E8C6"
            links.append('<a href="{}" style="{}">{}</a>'.format(escape(urls[key]), style, escape(label)))
    if u and can(u, "logins") or (legacy and not any_accounts()):
        style = "color:#fff;font-weight:800;text-decoration:none" if here == "logins" else "color:#D9E8C6"
        links.append('<a href="{}" style="{}">Logins</a>'.format(escape(logins_url), style))
    who = ('Signed in as <b>{}</b> · <a href="{}" style="color:#D9E8C6">My password</a> · '
           '<a href="{}" style="color:#D9E8C6">Log out</a>').format(escape(u["name"]), escape(account_url), escape(logout_url)) if u \
        else 'Signed in with the shared password · <a href="{}" style="color:#D9E8C6">Set up personal logins</a>'.format(escape(logins_url))
    return Markup('<div class="farmbar noprint" style="background:#1F4028;color:#fff;font-size:14px">'
                  '<div style="max-width:1100px;margin:0 auto;padding:7px 20px;display:flex;gap:10px 18px;flex-wrap:wrap;'
                  'justify-content:space-between;align-items:center"><span style="display:flex;gap:16px;flex-wrap:wrap">{}</span>'
                  '<span>{}</span></div></div>'.format(" ".join(links), who))


# ---------------------------------------------------------------- pages

SHELL = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} · {{ brand }}</title>
<style>
:root{--g:{{ color }};}
*{box-sizing:border-box}body{margin:0;background:#F7F5EE;color:#1E2A20;font-family:Nunito,system-ui,-apple-system,sans-serif;font-size:16px;line-height:1.45}
header{background:var(--g);color:#fff}header div{max-width:760px;margin:0 auto;padding:16px 20px;font-weight:800;font-size:20px}
main{max-width:760px;margin:0 auto;padding:24px 20px 60px;display:flex;flex-direction:column;gap:16px}
.card{background:#fff;border:1px solid #E2E4DA;border-radius:16px;padding:18px;display:flex;flex-direction:column;gap:12px}
.narrow{max-width:440px;margin:0 auto;width:100%}
h1{font-size:24px;margin:0}h2{font-size:19px;margin:0}
label{font-weight:700;font-size:14px;display:flex;flex-direction:column;gap:6px}
label.inline{flex-direction:row;align-items:center;gap:8px;font-weight:600}
input[type=text],input[type=email],input[type=password]{height:46px;border:1px solid #C9CEC0;border-radius:10px;padding:0 12px;font-size:16px;font-family:inherit;width:100%}
input[type=checkbox]{width:20px;height:20px;accent-color:var(--g);margin:0}
.btn{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:0 18px;border-radius:12px;border:2px solid var(--g);background:var(--g);color:#fff;font-weight:800;font-size:15px;text-decoration:none;cursor:pointer;font-family:inherit}
.btn.ghost{background:#fff;color:var(--g)}.btn.small{min-height:34px;padding:0 10px;font-size:13px}
.flash{border:2px solid var(--g);border-radius:12px;padding:10px 14px;font-weight:700;background:#fff}
.err{border-color:#B42318}.muted{color:#4F5A51;font-size:14px}a{color:var(--g)}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center}.between{justify-content:space-between}
.tag{display:inline-block;font-size:12px;font-weight:800;padding:2px 8px;border-radius:999px;background:#E8F0DD}
.tag.grey{background:#EEEFE8;color:#596158}
.code{font-family:ui-monospace,Menlo,monospace;font-weight:800;background:#FFF4D6;padding:2px 6px;border-radius:6px}
details summary{cursor:pointer;font-weight:700}
</style></head><body>
{{ bar }}
<header><div>{{ brand }}</div></header>
<main>
{% for m in messages %}<div class="flash {{ 'err' if m.startswith('!') }}" role="status">{{ m.lstrip('!') }}</div>{% endfor %}
{{ body }}
</main></body></html>"""

LOGIN = """<form method="post" class="card narrow">
  <h1>Log in</h1>
  <input type="hidden" name="next" value="{{ next }}">
  <label>Email <input type="email" name="email" autocomplete="username" value="{{ email }}" autofocus required></label>
  <label>Password <input type="password" name="password" autocomplete="current-password" required></label>
  <button class="btn">Log in</button>
  <a href="{{ forgot_url }}" class="muted">Forgot your password?</a>
  {% if not ready %}<p class="muted" style="margin:0">Personal logins aren't set up here yet. Use the <a href="{{ legacy_url }}">shared password</a> for now.</p>{% endif %}
</form>"""

FORGOT = """<form method="post" class="card narrow">
  <h1>Forgot your password?</h1>
  <p style="margin:0">Enter your email and we'll send a link to choose a new one. The link works for an hour.</p>
  <label>Email <input type="email" name="email" autocomplete="username" required autofocus></label>
  <button class="btn">Send me a link</button>
  <a href="{{ login_url }}" class="muted">Back to log in</a>
</form>"""

RESET = """<form method="post" class="card narrow">
  <h1>{{ heading }}</h1>
  {% if who %}<p style="margin:0">For <b>{{ who }}</b>.</p>{% endif %}
  {% if need_current %}<label>Current password <input type="password" name="current" autocomplete="current-password" required></label>{% endif %}
  <label>New password <input type="password" name="password" autocomplete="new-password" required minlength="10"></label>
  <label>New password again <input type="password" name="password2" autocomplete="new-password" required minlength="10"></label>
  <p class="muted" style="margin:0">At least 10 characters. A few words strung together is easy to remember and hard to guess.</p>
  <button class="btn">Save my password</button>
</form>"""

LOGINS = """<div class="row between"><h1>Logins</h1><a class="btn ghost small" href="{{ back_url }}">‹ Back to admin</a></div>
{% if setup %}
<section class="card" style="border:2px solid var(--g)">
  <h2>Start with your own login</h2>
  <p style="margin:0">You're signed in with the shared password. Make your personal login first: it's the owner login, with every box ticked. Then add Staci and Mollie below. Each app keeps taking its old shared password until someone has a personal login for it, so nobody is locked out while you set this up.</p>
</section>
{% endif %}
<section class="card">
  <h2>{{ 'Your login' if setup else 'Add a person' }}</h2>
  <form method="post" style="display:flex;flex-direction:column;gap:10px">
    <input type="hidden" name="action" value="add">
    <div class="row"><label style="flex:1;min-width:180px">Name <input type="text" name="name" required placeholder="{{ 'Jeremy' if setup else 'Staci' }}"></label>
      <label style="flex:2;min-width:220px">Email <input type="email" name="email" required></label></div>
    {% if setup %}
      <input type="hidden" name="can_playdates" value="1"><input type="hidden" name="can_shop" value="1"><input type="hidden" name="can_logins" value="1">
      <label class="inline"><input type="checkbox" name="can_bakery" value="1"> <span>Also let me into Mollie's bakery admin</span></label>
      <label>Your password <input type="password" name="password" autocomplete="new-password" required minlength="10"></label>
    {% else %}
      <div class="row">{% for key, label in apps %}<label class="inline"><input type="checkbox" name="can_{{ key }}" value="1" {{ 'checked' if key in ('playdates', 'shop') }}> <span>{{ label }}</span></label>{% endfor %}</div>
      <label>Temporary password (they'll choose their own the first time they log in) <input type="text" name="password" autocomplete="off" required minlength="10" value="{{ suggestion }}"></label>
    {% endif %}
    <button class="btn">{{ 'Make my login' if setup else 'Add this person' }}</button>
  </form>
</section>
{% if not setup %}
<section class="card">
  <h2>People</h2>
  {% for u in people %}
  <form method="post" class="card" style="padding:12px;gap:8px;{{ 'opacity:.6' if not u.active }}">
    <input type="hidden" name="action" value="save"><input type="hidden" name="id" value="{{ u.id }}">
    <div class="row between"><span><b>{{ u.name }}</b> <span class="muted">{{ u.email }}</span>{% if u.id == me.id %} <span class="tag">you</span>{% endif %}{% if not u.active %} <span class="tag grey">turned off</span>{% endif %}</span>
      <span class="muted">{{ 'Last logged in ' ~ u.last_login_at[:10] if u.last_login_at else 'Never logged in' }}</span></div>
    <div class="row">{% for key, label in apps %}<label class="inline"><input type="checkbox" name="can_{{ key }}" value="1" {{ 'checked' if u['can_' ~ key] }} {{ 'disabled' if key == 'logins' and u.id == me.id }}> <span>{{ label }}</span></label>{% endfor %}
      {% if u.id == me.id %}<input type="hidden" name="can_logins" value="1">{% endif %}</div>
    <div class="row">
      <button class="btn small">Save access</button>
      {% if u.id != me.id %}
        <button class="btn ghost small" name="action" value="{{ 'off' if u.active else 'on' }}">{{ 'Turn off' if u.active else 'Turn back on' }}</button>
        <button class="btn ghost small" name="action" value="reset">Email a password link</button>
        <details><summary class="muted">Set a temporary password</summary>
          <div class="row" style="margin-top:6px"><input type="text" name="temp" autocomplete="off" minlength="10" placeholder="at least 10 characters" style="width:220px">
          <button class="btn ghost small" name="action" value="temp">Set it</button></div></details>
      {% endif %}
    </div>
  </form>
  {% endfor %}
  <p class="muted" style="margin:0"><b>Playdates</b> and <b>Farm shop</b> share one login. <b>Bakery</b> is Mollie's admin on her own site, with her branding. <b>Manage logins</b> lets someone use this page. Turning someone off logs them out everywhere right away.</p>
</section>
{% endif %}"""

SUGGEST_WORDS = ["barn", "clover", "meadow", "fence", "kid", "lamb", "pail", "hay", "creek", "sprout", "gate", "acorn",
                 "maple", "pumpkin", "orchard", "honey", "biscuit", "puddle", "tractor", "bunny"]


def suggestion():
    return "-".join(secrets.choice(SUGGEST_WORDS) for _ in range(3)) + str(secrets.randbelow(90) + 10)


def add_pages(app, brand="Gooseberry Bridge Farm", color="#2F5D3A", farm=True, default_next="/admin",
              legacy_admin=lambda: False, legacy_login_endpoint=None, here="playdates", mailer_from="PLAYDATE_MAIL_FROM"):
    """Adds /account/... pages to a Flask app.

    farm: True for the farm apps (shows the admin bar and the Logins page); False for the bakery.
    legacy_admin: callable, True if this browser is signed in with the app's old shared password.
    """
    from flask import redirect, render_template_string, request, url_for

    mail_cache = {}

    def mailer():
        if "m" not in mail_cache:
            try:
                import mailer as mailer_mod
                mail_cache["m"] = mailer_mod.build_mailer(mailer_from)
            except Exception as e:  # noqa: BLE001  (half-set-up mail: don't crash, just can't email)
                app.logger.warning("account emails unavailable: %s", e)
                mail_cache["m"] = None
        return mail_cache["m"]

    def page(title, body_template, messages=(), **ctx):
        body = Markup(render_template_string(body_template, **ctx))
        bar = admin_bar(request, here if title != "Logins" else "logins", url_for("account_logins"), url_for("account_password"),
                        url_for("account_logout"), legacy=legacy_admin()) if farm and (current_user(request) or legacy_admin()) else ""
        return render_template_string(SHELL, title=title, brand=brand, color=color, body=body, messages=list(messages), bar=bar)

    def safe_next(nxt):
        nxt = nxt or ""
        return nxt if nxt.startswith("/") and not nxt.startswith("//") else (request.script_root + default_next)

    @app.route("/account/login", methods=["GET", "POST"], endpoint="account_login")
    def account_login():
        msgs, email = [], ""
        nxt = request.values.get("next", "")
        if request.method == "POST":
            email = request.form.get("email", "").strip()
            u, err = authenticate(email, request.form.get("password", ""), request.remote_addr or "")
            if u:
                token = start_login(u)
                dest = url_for("account_password", first=1, next=safe_next(nxt)) if u["must_change"] else safe_next(nxt)
                return set_cookie(redirect(dest), request, token)
            msgs.append("!" + err)
        ready = any_accounts()
        legacy_url = url_for(legacy_login_endpoint) if legacy_login_endpoint else ""
        return page("Log in", LOGIN, msgs, next=nxt, email=email, forgot_url=url_for("account_forgot"),
                    ready=ready or not legacy_url, legacy_url=legacy_url)

    @app.route("/account/logout", endpoint="account_logout")
    def account_logout():
        end_login(request.cookies.get(COOKIE))
        from flask import session
        for k in ("admin", "shop_admin", "bakery_admin"):
            session.pop(k, None)
        return clear_cookie(redirect(url_for("account_login")), request)

    @app.route("/account/forgot", methods=["GET", "POST"], endpoint="account_forgot")
    def account_forgot():
        if request.method == "POST":
            u = user_by_email(request.form.get("email", ""))
            if u and u["active"]:
                token = make_reset(u)
                link = url_for("account_reset", token=token, _external=True)
                m = mailer()
                if m:
                    m.send(u["email"], "Choose a new password",
                           "Hi {}!\n\nUse this link to choose a new password. It works for one hour:\n{}\n\n"
                           "If you didn't ask for this, you can ignore this email.".format(u["name"], link))
            return page("Check your email", "<section class='card narrow'><h1>Check your email</h1><p style='margin:0'>If that email "
                        "has a login here, we sent it a link to choose a new password. It works for one hour.</p>"
                        "<a href='{{ u }}'>Back to log in</a></section>", u=url_for("account_login"))
        return page("Forgot your password?", FORGOT, login_url=url_for("account_login"))

    @app.route("/account/reset/<token>", methods=["GET", "POST"], endpoint="account_reset")
    def account_reset(token):
        u = reset_user(token)
        if not u:
            return page("That link has expired", "<section class='card narrow'><h1>That link has expired</h1><p style='margin:0'>"
                        "Password links work once, for one hour. <a href='{{ u }}'>Ask for a new one</a>.</p></section>",
                        u=url_for("account_forgot"))
        msgs = []
        if request.method == "POST":
            pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
            err = password_problem(pw) or ("Those two passwords don't match." if pw != pw2 else "")
            if not err:
                set_password(u["id"], pw)
                run("UPDATE farm_resets SET used=1 WHERE token_hash=?", _hash(token))
                end_all_logins(u["id"])
                return set_cookie(redirect(safe_next("")), request, start_login(u))
            msgs.append("!" + err)
        return page("Choose a new password", RESET, msgs, heading="Choose a new password", who=u["email"], need_current=False)

    @app.route("/account/password", methods=["GET", "POST"], endpoint="account_password")
    def account_password():
        u = current_user(request)
        if not u:
            return redirect(url_for("account_login", next=request.script_root + request.full_path.rstrip("?")))
        first = bool(request.values.get("first")) or bool(u["must_change"])
        msgs = []
        if request.method == "POST":
            pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
            err = ""
            if not first and not check_password(u["pw_hash"], request.form.get("current", "")):
                err = "Your current password wasn't right."
            err = err or password_problem(pw) or ("Those two passwords don't match." if pw != pw2 else "")
            if not err:
                set_password(u["id"], pw)
                current = request.cookies.get(COOKIE)
                run("DELETE FROM farm_logins WHERE user_id=? AND token_hash != ?", u["id"], _hash(current or ""))  # other devices log out
                return redirect(safe_next(request.values.get("next")))
            msgs.append("!" + err)
        return page("Your password", RESET, msgs, heading="Choose your own password" if first else "Change my password",
                    who=u["email"], need_current=not first)

    if not farm:
        return

    @app.route("/account/logins", methods=["GET", "POST"], endpoint="account_logins")
    def account_logins():
        me = current_user(request)
        setup = not any_accounts()
        if not (can(me, "logins") or (setup and legacy_admin())):
            if me:
                return page("Logins", "<section class='card narrow'><h1>Not your page</h1><p style='margin:0'>Only someone with "
                            "<b>Manage logins</b> can add people or change access. To change your own password, use "
                            "<a href='{{ u }}'>My password</a>.</p></section>", u=url_for("account_password"))
            return redirect(url_for("account_login", next=request.script_root + request.path))
        msgs = []
        if request.method == "POST":
            f = request.form
            act = f.get("action")
            if act == "add":
                name, email, pw = f.get("name", "").strip(), f.get("email", "").strip().lower(), f.get("password", "")
                err = ("Please enter a name and email." if not name or "@" not in email else "") or password_problem(pw) \
                    or ("Someone already has that email." if user_by_email(email) else "")
                if err:
                    msgs.append("!" + err)
                else:
                    perms = {k: 1 if f.get("can_" + k) else 0 for k, _ in APPS}
                    if setup:
                        perms.update(playdates=1, shop=1, logins=1)
                    uid = run("""INSERT INTO farm_users(email, name, pw_hash, can_playdates, can_shop, can_bakery, can_logins,
                                 active, must_change, created_at) VALUES (?,?,?,?,?,?,?,1,?,?)""",
                              email, name, hash_password(pw), perms["playdates"], perms["shop"], perms["bakery"], perms["logins"],
                              0 if setup else 1, now().isoformat())
                    if setup:
                        resp = redirect(url_for("account_logins"))
                        return set_cookie(resp, request, start_login(user_by_id(uid)))
                    msgs.append("Added {}. Give them their email and the temporary password; they'll choose their own when they first "
                                "log in.".format(name))
            elif act in ("save", "off", "on", "reset", "temp") and me:
                uid = int(f.get("id", 0))
                u = user_by_id(uid)
                if not u:
                    msgs.append("!That person wasn't found.")
                elif act == "save":
                    perms = {k: 1 if f.get("can_" + k) else 0 for k, _ in APPS}
                    if uid == me["id"]:
                        perms["logins"] = 1  # you can't lock yourself out of this page
                    run("UPDATE farm_users SET can_playdates=?, can_shop=?, can_bakery=?, can_logins=? WHERE id=?",
                        perms["playdates"], perms["shop"], perms["bakery"], perms["logins"], uid)
                    msgs.append("Saved {}'s access.".format(u["name"]))
                elif act in ("off", "on"):
                    if uid == me["id"]:
                        msgs.append("!You can't turn off your own login.")
                    else:
                        run("UPDATE farm_users SET active=? WHERE id=?", 1 if act == "on" else 0, uid)
                        if act == "off":
                            end_all_logins(uid)
                        msgs.append("{} is turned {}.".format(u["name"], "back on" if act == "on" else "off and logged out everywhere"))
                elif act == "reset":
                    token = make_reset(u, hours=24)
                    link = url_for("account_reset", token=token, _external=True)
                    m = mailer()
                    sent = m.send(u["email"], "Choose a new password",
                                  "Hi {}!\n\nUse this link to choose a new password. It works for 24 hours:\n{}".format(u["name"], link)) if m else False
                    msgs.append("Emailed {} a link to choose a new password.".format(u["name"]) if sent else
                                "!Email isn't working here, so here's the link to send {} yourself (works for 24 hours): {}".format(u["name"], link))
                elif act == "temp":
                    pw = f.get("temp", "")
                    err = password_problem(pw)
                    if err:
                        msgs.append("!" + err)
                    else:
                        set_password(uid, pw, must_change=True)
                        end_all_logins(uid)
                        msgs.append("Set a temporary password for {}. They'll choose their own when they log in.".format(u["name"]))
        return page("Logins", LOGINS, msgs, setup=not any_accounts(), people=users(), me=current_user(request) or {"id": 0},
                    apps=APPS, suggestion=suggestion(), back_url=request.script_root + default_next)


# ---------------------------------------------------------------- hooking into each app

def attach_playdates(pd):
    """Playdate admin: personal logins once anyone has Playdates access; until then the shared password."""
    from flask import redirect, request, session, url_for

    app = pd.app
    ensure_schema()

    @app.before_request
    def farm_login_sync():
        # Keep the app's own "admin" flag in step with the personal login on every page.
        if accounts_for("playdates"):
            if can(current_user(request), "playdates"):
                session["admin"] = True
            else:
                session.pop("admin", None)

    legacy_login = app.view_functions["admin_login"]
    legacy_logout = app.view_functions["admin_logout"]

    def admin_login():
        if accounts_for("playdates"):
            return redirect(url_for("account_login", next=request.args.get("next") or url_for("admin_home")))
        return legacy_login()

    def admin_logout():
        if request.cookies.get(COOKIE):
            return redirect(url_for("account_logout"))
        return legacy_logout()

    app.view_functions["admin_login"] = admin_login
    app.view_functions["admin_logout"] = admin_logout

    def legacy():
        return bool(session.get("admin")) and not accounts_for("playdates")

    add_pages(app, default_next="/admin", legacy_admin=legacy, legacy_login_endpoint="admin_login", here="playdates")
    app.jinja_env.globals["farm_bar"] = lambda: admin_bar(request, "playdates", url_for("account_logins"), url_for("account_password"),
                                                          url_for("account_logout"), legacy=legacy())
