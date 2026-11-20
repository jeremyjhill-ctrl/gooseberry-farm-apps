"""Gooseberry Bridge Farm shop: passes, bundles and products that Staci sets up herself.

Runs beside the playdate booking app (see wsgi.py) and shares its database, so a pass bought
here can pay for a playdate booking there (playdates/farm_passes.py holds the shared rules).
It does not share anything with Mollie's bakery.

Customer pages: shop home, sale pages, product pages, cart, checkout (TEST card stand-in until
the farm's Stripe account is hooked up), order confirmation, and each pass's own page with a QR.
Admin (/admin, same password as the playdate admin): storefront switches, products and bundles,
credit types, sale pages, orders, passes and hand-made codes, settings.

Settings (environment variables):
  ADMIN_PASSWORD        same password as the playdate admin
  SECRET_KEY            signs the login cookie (the shop uses its own cookie name)
  PLAYDATE_DB           on the Mac: the playdate database file (the shop's tables live in it)
  DATABASE_URL          on Render: the Postgres server (farm_apps database)
  BOOKING_URL, SHOP_URL optional: the public addresses, if they can't be worked out from the host
  R2_*                  Cloudflare R2 for product photos (same bucket as the bakery, "shop/" folder)
"""
import base64
import csv
import datetime as dt
import io
import json
import os
import re
import secrets
import sqlite3
import sys
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template, request, send_from_directory,
                   session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "playdates"))
import dbcompat  # noqa: E402
import farm_mail  # noqa: E402  (real email only when PLAYDATE_EMAILS=on, like the playdate messages)
import farm_auth as fa  # noqa: E402  (personal logins)
import farm_passes as fp  # noqa: E402

try:
    import qrcode
    import qrcode.image.svg
except ImportError:  # pragma: no cover
    qrcode = None

TEST_CARD_OK = "4242424242424242"
TEST_CARD_DECLINE = "4000000000000002"
HOLD_MINUTES = 30
MAX_PENDING_PER_VISITOR = 3

DEFAULT_SETTINGS = {
    "shop_name": "Gooseberry Bridge Farm Shop",
    "intro": "Passes, bundles and goodies from our little farm in Rogersville, Missouri.",
    "shop_hidden": True,       # starts hidden: customers see it only after Staci opens it (it's on the test card for now)
    "shop_hidden_message": "Our shop is closed for now. Check back soon, or follow us on Facebook and Instagram for the next sale.",
    "terms": ("All sales are final. Passes are good during the season shown on the pass and can't be exchanged for cash. "
              "Seasonal items like flower picking depend on the weather and may end early, so please use your credits accordingly. "
              "Playdates still need an arrival time: book online with your pass code when dates are released."),
    "tax_rate": None,          # blank = use the playdate app's sales tax rate
    "shipping_flat": 6.0,      # one flat shipping charge per order that has anything to ship
    "pickup_note": "We'll email you when your order is ready to pick up at the farm.",
    "home_state": "MO",
}


def money(x):
    x = float(x or 0)
    return "${:,.2f}".format(x) if round(x, 2) != int(round(x, 2)) else "${:,.0f}".format(x)


def create_app():
    data_dir = os.environ.get("FARM_DATA_DIR") or os.path.join(os.path.dirname(HERE), "data")
    uploads = os.path.join(data_dir, "shop_uploads")
    os.makedirs(uploads, exist_ok=True)
    db_path = os.environ.get("PLAYDATE_DB") or os.path.join(data_dir, "playdates.db")
    password = os.environ.get("ADMIN_PASSWORD", "playdate")
    r2 = None
    r2_bucket = os.environ.get("R2_BUCKET", "")
    if r2_bucket:
        import boto3
        endpoint = os.environ.get("R2_ENDPOINT") or "https://{}.r2.cloudflarestorage.com".format(os.environ["R2_ACCOUNT_ID"])
        r2 = boto3.client("s3", endpoint_url=endpoint, region_name="auto",
                          aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                          aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])

    app = Flask(__name__, static_folder="static", static_url_path="/static", template_folder="templates")
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.secret_key = (os.environ.get("SECRET_KEY") or "local-test-only-change-me") + ":shop"
    app.config.update(SESSION_COOKIE_NAME="shop_session", SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=dt.timedelta(days=30), MAX_CONTENT_LENGTH=12 * 1024 * 1024,
                      TEMPLATES_AUTO_RELOAD=True)
    login_tries = {}

    # ------------------------------------------------------------ database
    def connect():
        con = dbcompat.connect(db_path, timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        return con

    con = connect()
    fp.ensure_schema(con)
    con.close()
    fa.ensure_schema()

    def db():
        if "db" not in g:
            g.db = connect()
        return g.db

    @app.teardown_appcontext
    def close_db(exc):
        c = g.pop("db", None)
        if c is not None:
            c.close()

    def rows(sql, *args):
        return [dict(r) for r in db().execute(sql, args)]

    def row(sql, *args):
        r = db().execute(sql, args).fetchone()
        return dict(r) if r else None

    def now():
        return dt.datetime.now(fp.TZ).replace(microsecond=0)

    def settings():
        s = dict(DEFAULT_SETTINGS)
        for r in db().execute("SELECT key, value FROM shop_settings"):
            try:
                s[r["key"]] = json.loads(r["value"])
            except ValueError:
                s[r["key"]] = r["value"]
        if s.get("tax_rate") in (None, ""):
            pd = playdate_settings()
            s["tax_rate"] = float(pd.get("tax_rate", 6.475))
        return s

    def save_setting(key, value):
        db().execute("INSERT OR REPLACE INTO shop_settings(key, value) VALUES (?, ?)", (key, json.dumps(value)))

    def playdate_settings():
        out = {}
        for r in db().execute("SELECT key, value FROM settings"):
            try:
                out[r["key"]] = json.loads(r["value"])
            except ValueError:
                pass
        return out

    def save_playdate_setting(key, value):
        db().execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, json.dumps(value)))

    # ------------------------------------------------------------ helpers
    def is_admin():
        if fa.accounts_for("shop"):
            return fa.can(fa.current_user(request), "shop")
        return session.get("shop_admin") is True  # shared password, until someone has a personal login for the shop

    def admin_required(view):
        @wraps(view)
        def wrapped(*a, **kw):
            if not is_admin():
                return redirect(url_for("admin_login", next=request.script_root + request.full_path.rstrip("?")))
            return view(*a, **kw)
        return wrapped

    def booking_url(path="/book"):
        if os.environ.get("BOOKING_URL"):
            return os.environ["BOOKING_URL"].rstrip("/") + path
        host = request.host.split(":")[0]
        if host.startswith("shop."):
            return "https://booking." + host[len("shop."):] + path
        return request.host_url.rstrip("/") + path  # on the Mac: same server, booking app at the root

    def qr_svg(data):
        if not qrcode:
            return None
        img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
        return img.to_string(encoding="unicode").replace("<svg ", '<svg role="img" aria-label="Pass QR code" ', 1)

    def parse_when(text):
        """'2026-11-27T06:00' from a datetime-local box -> aware datetime (farm time)."""
        if not text:
            return None
        try:
            d = dt.datetime.fromisoformat(text)
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=fp.TZ)

    def nice_when(text):
        d = parse_when(text)
        if not d:
            return ""
        return d.strftime("%a %b ") + str(d.day) + ", " + d.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")

    def page_state(p):
        """'open' | 'upcoming' | 'ended' | 'hidden' for a sale page."""
        if not p:
            return "open"
        if p["hidden"] or p["archived"]:
            return "hidden"
        t = now()
        start, end = parse_when(p["starts_at"]), parse_when(p["ends_at"])
        if start and t < start:
            return "upcoming"
        if end and t >= end:
            return "ended"
        return "open"

    def product_credits(pid):
        return rows("""SELECT pc.credit_type_id, pc.qty, ct.name, ct.value, ct.booking_role, ct.at_farm, ct.available, ct.unavailable_note
                       FROM shop_product_credits pc JOIN shop_credit_types ct ON ct.id = pc.credit_type_id
                       WHERE pc.product_id = ? ORDER BY pc.id""", pid)

    def committed(pid):
        """Sold (paid) plus in someone's checkout right now."""
        r = db().execute("""SELECT COALESCE(SUM(l.qty), 0) FROM shop_order_lines l JOIN shop_orders o ON o.id = l.order_id
                            WHERE l.product_id = ? AND (o.status = 'paid' OR (o.status = 'pending' AND o.hold_expires > ?))""",
                         (pid, now().isoformat())).fetchone()
        return int(r[0] or 0)

    def sold(pid):
        r = db().execute("""SELECT COALESCE(SUM(l.qty), 0) FROM shop_order_lines l JOIN shop_orders o ON o.id = l.order_id
                            WHERE l.product_id = ? AND o.status = 'paid'""", (pid,)).fetchone()
        return int(r[0] or 0)

    def decorate(p):
        p = dict(p)
        p["credits"] = product_credits(p["id"]) if p["kind"] == "pass" else []
        p["left"] = None if p["stock_limit"] in (None, "") else max(0, int(p["stock_limit"]) - committed(p["id"]))
        p["page"] = row("SELECT * FROM shop_pages WHERE id=?", p["page_id"]) if p["page_id"] else None
        p["state"] = page_state(p["page"])
        p["buyable"] = (not p["hidden"] and not p["archived"] and p["state"] == "open" and (p["left"] is None or p["left"] > 0)
                        and not settings()["shop_hidden"])
        return p

    def visible_products(page_id=None, include_upcoming=False):
        q = "SELECT * FROM shop_products WHERE archived = 0 AND hidden = 0 AND "
        q += "page_id = ?" if page_id else "(page_id IS NULL OR page_id = 0)"
        q += " ORDER BY sort, id"
        out = [decorate(p) for p in rows(q, *([page_id] if page_id else []))]
        return [p for p in out if include_upcoming or p["state"] in ("open", "ended")]

    def cart():
        c = session.get("cart") or {}
        return {str(k): int(v) for k, v in c.items() if int(v) > 0}

    def cart_lines():
        lines = []
        for pid, qty in cart().items():
            p = row("SELECT * FROM shop_products WHERE id=?", int(pid))
            if not p:
                continue
            p = decorate(p)
            lines.append({"product": p, "qty": qty, "amount": round(float(p["price"]) * qty, 2)})
        return lines

    def totals(lines, ship_state=""):
        s = settings()
        sub = round(sum(l["amount"] for l in lines), 2)
        ships = any(l["product"]["fulfillment"] == "ship" for l in lines)
        shipping = round(float(s["shipping_flat"] or 0), 2) if ships else 0.0
        state = (ship_state or "").strip().upper()
        home = (s.get("home_state") or "MO").upper()
        taxable = 0.0
        for l in lines:
            p = l["product"]
            if not p["taxable"]:
                continue
            if p["fulfillment"] == "ship" and state and state != home:
                continue  # shipped out of state: no Missouri tax (mirrors how Square handled it)
            taxable += l["amount"]
        tax = round(taxable * float(s["tax_rate"]) / 100.0, 2)
        return {"sub": sub, "shipping": shipping, "tax": tax, "total": round(sub + shipping + tax, 2), "ships": ships}

    def housekeeping():
        db().execute("UPDATE shop_orders SET status='expired' WHERE status='pending' AND hold_expires < ?", (now().isoformat(),))

    def send_email(to, subject, body):
        """Same as playdate messages: a copy in Playdate admin > Messages, real email when PLAYDATE_EMAILS=on."""
        if not to:
            return
        db().execute("INSERT INTO outbox(booking_id, channel, recipient, subject, body, created_at) VALUES (NULL, 'email', ?, ?, ?, ?)",
                     (to, subject, body, now().isoformat()))
        farm_mail.deliver(to, subject, body)

    def save_photo(data_url):
        m = re.match(r"^data:image/(jpeg|png|webp);base64,(.+)$", data_url or "", re.S)
        if not m:
            return None
        name = secrets.token_hex(6) + "." + ("jpg" if m.group(1) == "jpeg" else m.group(1))
        data = base64.b64decode(m.group(2))
        if r2:
            r2.put_object(Bucket=r2_bucket, Key="shop/" + name, Body=data, ContentType="image/" + m.group(1))
        else:
            with open(os.path.join(uploads, name), "wb") as f:
                f.write(data)
        return name

    def plain(x):
        """120.0 -> 120, 12.5 -> 12.50, for number boxes in the admin."""
        from jinja2 import Undefined
        if x is None or isinstance(x, Undefined) or x == "":
            return ""  # blank on a brand-new product
        try:
            x = float(x)
        except (TypeError, ValueError):
            return ""
        return str(int(x)) if x == int(x) else "{:.2f}".format(x)

    app.jinja_env.filters["plain"] = plain
    app.jinja_env.globals.update(money=money, nice_day=fp.nice_day, nice_when=nice_when, pretty_code=fp.pretty_code,
                                 booking_url=booking_url, is_admin=is_admin)

    @app.context_processor
    def inject():
        return {"SS": settings(), "cart_count": sum(cart().values())}

    @app.before_request
    def before():
        housekeeping()
        public = request.endpoint in {"home", "page", "product", "cart_view", "cart_add", "cart_update", "checkout"}
        if public and settings()["shop_hidden"] and not is_admin():
            s = settings()
            return render_template("message.html", title="The shop is closed", body=s["shop_hidden_message"]), 200

    # ------------------------------------------------------------ customer pages
    @app.get("/")
    def home():
        pages = [dict(p, state=page_state(p)) for p in rows("SELECT * FROM shop_pages WHERE archived = 0 AND hidden = 0 ORDER BY sort, id")]
        pages = [p for p in pages if p["state"] in ("open", "upcoming")]
        for p in pages:
            p["products"] = visible_products(p["id"], include_upcoming=True)
        return render_template("home.html", pages=pages, products=visible_products())

    @app.get("/s/<slug>")
    def page(slug):
        p = row("SELECT * FROM shop_pages WHERE slug=? AND archived = 0", slug)
        if not p or (p["hidden"] and not is_admin()):
            abort(404)
        p["state"] = page_state(p) if not p["hidden"] else "hidden"
        products = [decorate(x) for x in rows("SELECT * FROM shop_products WHERE page_id=? AND archived=0 AND (hidden=0 OR ?=1) ORDER BY sort, id",
                                                p["id"], 1 if is_admin() else 0)]
        return render_template("page.html", p=p, products=products)

    @app.get("/p/<int:pid>")
    def product(pid):
        p = row("SELECT * FROM shop_products WHERE id=? AND archived=0", pid)
        if not p or (p["hidden"] and not is_admin()):
            abort(404)
        p = decorate(p)
        if p["state"] == "hidden" and not is_admin():
            abort(404)
        return render_template("product.html", p=p)

    @app.get("/photos/<name>")
    def photo(name):
        if not re.match(r"^[a-f0-9]+\.(jpg|png|webp)$", name):
            abort(404)
        if not r2:
            return send_from_directory(uploads, name, max_age=30 * 24 * 3600)
        try:
            obj = r2.get_object(Bucket=r2_bucket, Key="shop/" + name)
        except Exception:  # noqa: BLE001
            abort(404)
        return Response(obj["Body"].read(), mimetype=obj.get("ContentType") or "image/jpeg",
                        headers={"Cache-Control": "public, max-age=2592000"})

    @app.post("/cart/add")
    def cart_add():
        pid = request.form.get("product_id", type=int)
        qty = max(1, min(20, request.form.get("qty", 1, type=int) or 1))
        p = row("SELECT * FROM shop_products WHERE id=?", pid)
        if not p:
            abort(404)
        p = decorate(p)
        if not p["buyable"]:
            flash("Sorry, {} isn't available right now.".format(p["name"]))
            return redirect(url_for("product", pid=pid))
        c = cart()
        want = c.get(str(pid), 0) + qty
        if p["per_order_limit"]:
            want = min(want, int(p["per_order_limit"]))
        if p["left"] is not None:
            want = min(want, p["left"])
        c[str(pid)] = want
        session["cart"] = c
        flash("Added {} to your cart.".format(p["name"]))
        return redirect(url_for("cart_view"))

    @app.post("/cart/update")
    def cart_update():
        c = cart()
        for pid in list(c):
            v = request.form.get("qty_" + pid, type=int)
            if v is not None:
                c[pid] = max(0, min(20, v))
        session["cart"] = {k: v for k, v in c.items() if v > 0}
        return redirect(url_for("cart_view"))

    @app.get("/cart")
    def cart_view():
        lines = cart_lines()
        return render_template("cart.html", lines=lines, t=totals(lines))

    @app.route("/checkout", methods=["GET", "POST"])
    def checkout():
        lines = cart_lines()
        if not lines:
            return redirect(url_for("cart_view"))
        s = settings()
        f = request.form
        errors = []
        if request.method == "POST":
            name, email, phone = f.get("name", "").strip(), f.get("email", "").strip(), f.get("phone", "").strip()
            ship = {k: f.get("ship_" + k, "").strip() for k in ("name", "line1", "line2", "city", "state", "zip")}
            t = totals(lines, ship["state"])
            if not name:
                errors.append("Please enter your name.")
            if "@" not in email:
                errors.append("Please enter a valid email so we can send your order.")
            if t["ships"] and not (ship["line1"] and ship["city"] and ship["state"] and ship["zip"]):
                errors.append("Please fill in the shipping address.")
            if not f.get("agree"):
                errors.append("Please agree to the terms.")
            ip = request.remote_addr or ""
            con = db()
            if not errors:
                con.execute("BEGIN IMMEDIATE")
                try:
                    pending = con.execute("SELECT COUNT(*) FROM shop_orders WHERE status='pending' AND hold_ip=? AND hold_expires > ?",
                                          (ip, now().isoformat())).fetchone()[0]
                    if pending >= MAX_PENDING_PER_VISITOR:
                        errors.append("You have several checkouts open. Please finish one or wait a few minutes.")
                    for l in lines:
                        p = decorate(row("SELECT * FROM shop_products WHERE id=?", l["product"]["id"]))
                        if not p["buyable"] or (p["left"] is not None and p["left"] < l["qty"]):
                            errors.append("Sorry, {} just sold out or isn't on sale anymore.".format(p["name"]) if p["left"] == 0 or not p["buyable"]
                                          else "Only {} {} left.".format(p["left"], p["name"]))
                    if errors:
                        con.execute("ROLLBACK")
                    else:
                        code = fp.new_code(con, "shop_orders")
                        terms = terms_for(lines, s)
                        cur = con.execute("""INSERT INTO shop_orders(code, status, name, email, phone, ship_name, ship_line1, ship_line2,
                                ship_city, ship_state, ship_zip, subtotal, shipping, tax, total, agreed_terms, agreed_at, created_at,
                                hold_expires, hold_ip) VALUES (?, 'pending', ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                          (code, name, email, phone, ship["name"] or name, ship["line1"], ship["line2"], ship["city"],
                                           ship["state"].upper(), ship["zip"], t["sub"], t["shipping"], t["tax"], t["total"],
                                           terms, now().isoformat(), now().isoformat(),
                                           (now() + dt.timedelta(minutes=HOLD_MINUTES)).isoformat(), ip))
                        oid = cur.lastrowid
                        for l in lines:
                            p = l["product"]
                            gifts = [f.get("gift_{}_{}".format(p["id"], i), "").strip() for i in range(l["qty"])] if p["kind"] == "pass" else []
                            con.execute("""INSERT INTO shop_order_lines(order_id, product_id, kind, name, unit_price, qty, fulfillment, taxable, gift_for)
                                           VALUES (?,?,?,?,?,?,?,?,?)""", (oid, p["id"], p["kind"], p["name"], p["price"], l["qty"],
                                                                         p["fulfillment"], p["taxable"], json.dumps(gifts)))
                        con.execute("COMMIT")
                        return redirect(url_for("pay", code=code))
                except Exception:
                    con.execute("ROLLBACK")
                    raise
        t = totals(lines, f.get("ship_state", ""))
        return render_template("checkout.html", lines=lines, t=t, errors=errors, f=f, terms=terms_for(lines, s).split("\n\n"))

    def terms_for(lines, s):
        parts = [s["terms"]]
        for l in lines:
            p = l["product"]
            if p["kind"] == "pass" and (p["season_label"] or p["valid_until"]):
                bit = "{}: {}".format(p["name"], p["season_label"] or "")
                if p["valid_until"]:
                    bit += " (credits can be used through {})".format(fp.nice_day(p["valid_until"]))
                parts.append(bit.strip())
            if p["terms"]:
                parts.append("{}: {}".format(p["name"], p["terms"]))
        return "\n\n".join(x for x in parts if x)

    def order_by_code(code):
        o = row("SELECT * FROM shop_orders WHERE code=?", fp.clean_code(code))
        if not o:
            abort(404)
        o["lines"] = rows("SELECT * FROM shop_order_lines WHERE order_id=? ORDER BY id", o["id"])
        o["passes"] = rows("SELECT * FROM shop_passes WHERE order_id=? ORDER BY id", o["id"])
        return o

    @app.route("/pay/<code>", methods=["GET", "POST"])
    def pay(code):
        """TEST STAND-IN FOR STRIPE CHECKOUT (the farm's Stripe account goes here later)."""
        o = order_by_code(code)
        if o["status"] == "paid":
            return redirect(url_for("order", code=o["code"]))
        if o["status"] != "pending":
            return render_template("message.html", title="This checkout timed out",
                                   body="We held your items for {} minutes and they've been put back. You weren't charged.".format(HOLD_MINUTES),
                                   link=url_for("cart_view"), link_text="Back to my cart")
        error = None
        if request.method == "POST":
            card = "".join(ch for ch in request.form.get("card", "") if ch.isdigit())
            if card == TEST_CARD_DECLINE:
                error = "Your card was declined. (Test decline card.) Try 4242 4242 4242 4242."
            elif card != TEST_CARD_OK:
                error = "Test mode: use card 4242 4242 4242 4242 (any future date, any 3 digits)."
            else:
                con = db()
                con.execute("BEGIN IMMEDIATE")
                try:
                    fresh = dict(con.execute("SELECT * FROM shop_orders WHERE id=?", (o["id"],)).fetchone())
                    if fresh["status"] != "pending" or fresh["hold_expires"] < now().isoformat():
                        con.execute("ROLLBACK")
                        return redirect(url_for("pay", code=o["code"]))
                    con.execute("UPDATE shop_orders SET status='paid', paid_at=?, method='test_card', hold_expires=NULL WHERE id=?",
                                (now().isoformat(), o["id"]))
                    for l in o["lines"]:
                        if l["kind"] != "pass":
                            continue
                        p = row("SELECT * FROM shop_products WHERE id=?", l["product_id"]) or {}
                        credit_rows = [(c["credit_type_id"], c["qty"]) for c in rows(
                            "SELECT credit_type_id, qty FROM shop_product_credits WHERE product_id=? ORDER BY id", l["product_id"])]
                        gifts = json.loads(l["gift_for"] or "[]")
                        for i in range(int(l["qty"])):
                            fp.create_pass(con, l["name"], credit_rows, holder=o["name"], gift_for=(gifts[i] if i < len(gifts) else ""),
                                           email=o["email"], season_label=p.get("season_label", ""), valid_until=p.get("valid_until", ""),
                                           source="sale", order_id=o["id"], product_id=l["product_id"], by_whom="shop")
                    con.execute("COMMIT")
                except Exception:
                    con.execute("ROLLBACK")
                    raise
                session.pop("cart", None)
                o = order_by_code(o["code"])
                email_order(o)
                return redirect(url_for("order", code=o["code"]))
        return render_template("pay.html", o=o, error=error)

    def email_order(o):
        s = settings()
        lines = ["Hi {}! Thank you for your order.".format(o["name"]), ""]
        for l in o["lines"]:
            lines.append("{} × {}  {}".format(l["qty"], l["name"], money(l["unit_price"] * l["qty"])))
        lines.append("Total paid: {}".format(money(o["total"])))
        if o["passes"]:
            lines += ["", "Your pass{}: each one has a QR code to show at the farm.".format("es" if len(o["passes"]) > 1 else "")]
            for p in o["passes"]:
                who = " (for {})".format(p["gift_for"]) if p["gift_for"] else ""
                lines.append("  {}{}: {}".format(p["title"], who, url_for("pass_page", code=p["code"], _external=True)))
            lines += ["", "To book a playdate, pick a time on our booking page and enter your pass code at checkout:",
                      "  " + booking_url()]
        if any(l["fulfillment"] == "ship" for l in o["lines"]):
            lines += ["", "We'll ship to: {}, {} {}, {} {}".format(o["ship_name"], o["ship_line1"], o["ship_line2"] or "", o["ship_city"],
                                                                  o["ship_state"]).replace("  ", " ") + " " + (o["ship_zip"] or "")]
        if any(l["fulfillment"] == "pickup" for l in o["lines"]):
            lines += ["", s["pickup_note"]]
        lines += ["", "Order {} · {}".format(fp.pretty_code(o["code"]), url_for("order", code=o["code"], _external=True))]
        send_email(o["email"], "Your {} order".format(s["shop_name"]), "\n".join(lines))

    @app.get("/order/<code>")
    def order(code):
        o = order_by_code(code)
        if o["status"] != "paid":
            return redirect(url_for("pay", code=o["code"]))
        return render_template("order.html", o=o)

    # ------------------------------------------------------------ a pass
    def pass_or_404(code):
        p = fp.find_pass(db(), code)
        if not p:
            abort(404)
        return p

    @app.get("/pass/<code>")
    def pass_page(code):
        p = pass_or_404(code)
        if fp.clean_code(code) != code:
            return redirect(url_for("pass_page", code=p["code"]))
        con = db()
        cr = fp.credits(con, p["id"])
        can_book = any(c["booking_role"] and c["left"] > 0 and c["available"] for c in cr)
        return render_template("pass.html", p=p, credits=cr, history=fp.history(con, p["id"]) if is_admin() else [],
                               problem=fp.pass_problem(p), can_book=can_book,
                               qr=qr_svg(url_for("pass_page", code=p["code"], _external=True)))

    @app.get("/pass/<code>/print")
    def pass_print(code):
        p = pass_or_404(code)
        return render_template("pass_print.html", p=p, credits=fp.credits(db(), p["id"]),
                               qr=qr_svg(url_for("pass_page", code=p["code"], _external=True)))

    @app.post("/pass/<code>/use")
    @admin_required
    def pass_use(code):
        p = pass_or_404(code)
        con = db()
        con.execute("BEGIN IMMEDIATE")
        err = fp.use_at_farm(con, p, request.form.get("credit_type_id", type=int), request.form.get("qty", 1, type=int) or 1,
                             session.get("shop_name_used") or "staff")
        con.execute("ROLLBACK" if err else "COMMIT")
        flash(err or "Ticked off. {}".format(request.form.get("label", "")).strip())
        return redirect(url_for("pass_page", code=p["code"]))

    @app.post("/pass/<code>/undo/<int:use_id>")
    @admin_required
    def pass_undo(code, use_id):
        p = pass_or_404(code)
        con = db()
        con.execute("BEGIN IMMEDIATE")
        err = fp.undo_use(con, p, use_id, "staff")
        con.execute("ROLLBACK" if err else "COMMIT")
        flash(err or "Undone: the credit is back on the pass.")
        return redirect(url_for("pass_page", code=p["code"]))

    @app.post("/pass/<code>/edit")
    @admin_required
    def pass_edit(code):
        p = pass_or_404(code)
        f = request.form
        void = 1 if f.get("void") else 0
        db().execute("UPDATE shop_passes SET holder=?, gift_for=?, note=?, valid_until=?, season_label=?, void=? WHERE id=?",
                     (f.get("holder", "").strip(), f.get("gift_for", "").strip(), f.get("note", "").strip(),
                      f.get("valid_until", "").strip(), f.get("season_label", "").strip(), void, p["id"]))
        flash("Pass saved." + (" It's turned off and can't be used." if void else ""))
        return redirect(url_for("pass_page", code=p["code"]))

    # ------------------------------------------------------------ admin
    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if fa.accounts_for("shop"):
            return redirect(url_for("account_login", next=request.args.get("next") or url_for("admin_home")))
        if request.method == "POST":
            ip = request.remote_addr or ""
            recent = [t for t in login_tries.get(ip, []) if (now() - t).total_seconds() < 600]
            if len(recent) >= 5:
                flash("Too many wrong passwords. Please wait 10 minutes and try again.")
                return render_template("admin/login.html")
            if secrets.compare_digest(request.form.get("password", ""), password):
                login_tries.pop(ip, None)
                session.permanent = True
                session["shop_admin"] = True
                nxt = request.args.get("next") or ""
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("admin_home"))
            login_tries[ip] = recent + [now()]
            flash("That password didn't work.")
        return render_template("admin/login.html")

    @app.get("/admin/logout")
    def admin_logout():
        session.pop("shop_admin", None)
        if request.cookies.get(fa.COOKIE):
            return redirect(url_for("account_logout"))
        return redirect(url_for("home"))

    def legacy_admin():
        return session.get("shop_admin") is True and not fa.accounts_for("shop")

    fa.add_pages(app, default_next="/admin", legacy_admin=legacy_admin, legacy_login_endpoint="admin_login", here="shop")
    app.jinja_env.globals["farm_bar"] = lambda: fa.admin_bar(request, "shop", url_for("account_logins"), url_for("account_password"),
                                                             url_for("account_logout"), legacy=legacy_admin())

    @app.get("/admin")
    @admin_required
    def admin_home():
        pd = playdate_settings()
        paid = rows("SELECT * FROM shop_orders WHERE status='paid' ORDER BY id DESC")
        return render_template("admin/home.html", pd=pd, recent=paid[:8], orders_count=len(paid),
                               revenue=round(sum(o["subtotal"] for o in paid), 2),
                               passes_sold=row("SELECT COUNT(*) AS n FROM shop_passes WHERE source='sale'")["n"],
                               passes_made=row("SELECT COUNT(*) AS n FROM shop_passes WHERE source='made'")["n"],
                               outstanding=fp.outstanding(db()),
                               to_ship=row("""SELECT COUNT(DISTINCT o.id) AS n FROM shop_orders o JOIN shop_order_lines l ON l.order_id=o.id
                                              WHERE o.status='paid' AND o.fulfilled_at IS NULL AND l.fulfillment IN ('ship','pickup')""")["n"],
                               booking_hidden_default=fp.BOOKING_HIDDEN_MESSAGE)

    @app.post("/admin/visibility")
    @admin_required
    def admin_visibility():
        f = request.form
        which = f.get("which")
        hide = f.get("hidden") == "1"
        if which == "shop":
            save_setting("shop_hidden", hide)
            if f.get("message", "").strip():
                save_setting("shop_hidden_message", f["message"].strip())
            flash("The shop is hidden from customers (you can still see it while logged in)." if hide else "The shop is open to customers.")
        elif which == "booking":
            save_playdate_setting("booking_hidden", hide)
            if f.get("message", "").strip():
                save_playdate_setting("booking_hidden_message", f["message"].strip())
            flash("The playdate booking page is hidden from guests." if hide else "The playdate booking page is visible to guests.")
        return redirect(url_for("admin_home"))

    # ---- products
    @app.get("/admin/products")
    @admin_required
    def admin_products():
        items = [decorate(p) for p in rows("SELECT * FROM shop_products WHERE archived=0 ORDER BY page_id, sort, id")]
        for p in items:
            p["sold"] = sold(p["id"])
        pages = {p["id"]: p for p in rows("SELECT * FROM shop_pages WHERE archived=0")}
        return render_template("admin/products.html", items=items, pages=pages)

    def credit_types(include_archived=False):
        q = "SELECT * FROM shop_credit_types" + ("" if include_archived else " WHERE archived=0") + " ORDER BY sort, id"
        return rows(q)

    @app.route("/admin/products/new", methods=["GET", "POST"])
    @app.route("/admin/products/<int:pid>", methods=["GET", "POST"])
    @admin_required
    def admin_product(pid=None):
        p = row("SELECT * FROM shop_products WHERE id=?", pid) if pid else None
        if pid and not p:
            abort(404)
        kind = (p or {}).get("kind") or (request.args.get("kind") if request.args.get("kind") in ("pass", "item") else "pass")
        errors = []
        if request.method == "POST":
            f = request.form

            def num(name, cast=float):
                v = f.get(name, "").strip().replace("$", "").replace(",", "")
                if v == "":
                    return None
                try:
                    return cast(v)
                except ValueError:
                    errors.append("{} should be a number.".format(name.replace("_", " ").capitalize()))
                    return None
            name = f.get("name", "").strip()
            price = num("price")
            if not name:
                errors.append("Please give it a name.")
            if price is None or price < 0:
                errors.append("Please enter a price.")
            credit_rows = []
            if kind == "pass":
                for i in range(8):
                    ct, q = f.get("ct_{}".format(i), type=int), f.get("cq_{}".format(i), type=int)
                    if ct and q and q > 0:
                        credit_rows.append((ct, q))
                if not credit_rows:
                    errors.append("A pass needs at least one credit (for example 2 × Playdate).")
            fields = {
                "kind": kind, "name": name, "description": f.get("description", "").strip(), "price": price or 0,
                "value_price": num("value_price"), "stock_limit": num("stock_limit", int), "per_order_limit": num("per_order_limit", int),
                "hidden": 1 if f.get("hidden") else 0, "page_id": f.get("page_id", type=int) or None,
                "fulfillment": "pass" if kind == "pass" else (f.get("fulfillment") if f.get("fulfillment") in ("pickup", "ship", "none") else "pickup"),
                "taxable": 1 if f.get("taxable") else 0, "season_label": f.get("season_label", "").strip(),
                "valid_until": f.get("valid_until", "").strip(), "terms": f.get("terms", "").strip(),
                "sort": num("sort", int) or 0,
            }
            if f.get("photo_data", "").startswith("data:"):
                saved = save_photo(f["photo_data"])
                if saved:
                    fields["photo"] = saved
            elif f.get("remove_photo"):
                fields["photo"] = ""
            if not errors:
                if p:
                    sets = ", ".join("{}=?".format(k) for k in fields)
                    db().execute("UPDATE shop_products SET {} WHERE id=?".format(sets), list(fields.values()) + [p["id"]])
                    pid = p["id"]
                else:
                    fields["created_at"] = now().isoformat()
                    cur = db().execute("INSERT INTO shop_products({}) VALUES ({})".format(", ".join(fields), ", ".join("?" * len(fields))),
                                       list(fields.values()))
                    pid = cur.lastrowid
                db().execute("DELETE FROM shop_product_credits WHERE product_id=?", (pid,))
                for ct, q in credit_rows:
                    db().execute("INSERT INTO shop_product_credits(product_id, credit_type_id, qty) VALUES (?,?,?)", (pid, ct, q))
                flash("Saved {}.".format(name) + (" It's hidden from customers." if fields["hidden"] else ""))
                return redirect(url_for("admin_products"))
            p = dict(p or {}, **{k: v for k, v in fields.items()})
            p["credits"] = [{"credit_type_id": ct, "qty": q} for ct, q in credit_rows]
        else:
            p = dict(p) if p else {"kind": kind, "taxable": 1, "hidden": 1, "fulfillment": "pass" if kind == "pass" else "pickup",
                                   "season_label": "Good during the {} season".format(now().year + 1) if kind == "pass" else "",
                                   "valid_until": "{}-12-31".format(now().year + 1) if kind == "pass" else ""}
            p["credits"] = product_credits(p["id"]) if p.get("id") else []
        return render_template("admin/product.html", p=p, kind=kind, errors=errors, types=credit_types(),
                               pages=rows("SELECT * FROM shop_pages WHERE archived=0 ORDER BY sort, id"),
                               sold_count=sold(p["id"]) if p.get("id") else 0)

    @app.post("/admin/products/<int:pid>/toggle")
    @admin_required
    def admin_product_toggle(pid):
        p = row("SELECT * FROM shop_products WHERE id=?", pid) or abort(404)
        db().execute("UPDATE shop_products SET hidden=? WHERE id=?", (0 if p["hidden"] else 1, pid))
        flash("{} is now {}.".format(p["name"], "visible to customers" if p["hidden"] else "hidden"))
        return redirect(request.form.get("next") or url_for("admin_products"))

    @app.post("/admin/products/<int:pid>/copy")
    @admin_required
    def admin_product_copy(pid):
        p = row("SELECT * FROM shop_products WHERE id=?", pid) or abort(404)
        fields = {k: v for k, v in p.items() if k not in ("id",)}
        fields.update(name="Copy of " + p["name"], hidden=1, created_at=now().isoformat())
        cur = db().execute("INSERT INTO shop_products({}) VALUES ({})".format(", ".join(fields), ", ".join("?" * len(fields))), list(fields.values()))
        for c in rows("SELECT credit_type_id, qty FROM shop_product_credits WHERE product_id=?", pid):
            db().execute("INSERT INTO shop_product_credits(product_id, credit_type_id, qty) VALUES (?,?,?)", (cur.lastrowid, c["credit_type_id"], c["qty"]))
        flash("Made a hidden copy. Change what you need, then un-hide it.")
        return redirect(url_for("admin_product", pid=cur.lastrowid))

    @app.post("/admin/products/<int:pid>/remove")
    @admin_required
    def admin_product_remove(pid):
        p = row("SELECT * FROM shop_products WHERE id=?", pid) or abort(404)
        if sold(pid):
            db().execute("UPDATE shop_products SET archived=1, hidden=1 WHERE id=?", (pid,))
        else:
            db().execute("DELETE FROM shop_product_credits WHERE product_id=?", (pid,))
            db().execute("DELETE FROM shop_products WHERE id=?", (pid,))
        flash("Removed {}. Orders and passes already sold aren't affected.".format(p["name"]))
        return redirect(url_for("admin_products"))

    # ---- credit types
    @app.route("/admin/credits", methods=["GET", "POST"])
    @admin_required
    def admin_credits():
        if request.method == "POST":
            f = request.form
            cid = f.get("id", type=int)
            name = f.get("name", "").strip()
            if not name:
                flash("Please give the credit a name.")
                return redirect(url_for("admin_credits", edit=cid or "new"))
            try:
                value = float((f.get("value") or "0").replace("$", ""))
            except ValueError:
                value = 0.0
            role = f.get("booking_role") if f.get("booking_role") in ("package", "person") else ""
            vals = (name, value, role, 1 if f.get("at_farm") else 0, 1 if f.get("available") else 0, f.get("unavailable_note", "").strip())
            if cid:
                db().execute("UPDATE shop_credit_types SET name=?, value=?, booking_role=?, at_farm=?, available=?, unavailable_note=? WHERE id=?",
                             vals + (cid,))
            else:
                db().execute("INSERT INTO shop_credit_types(name, value, booking_role, at_farm, available, unavailable_note, sort, created_at) "
                             "VALUES (?,?,?,?,?,?,?,?)", vals + (99, now().isoformat()))
            flash("Saved {}.".format(name))
            return redirect(url_for("admin_credits"))
        types = credit_types()
        for t in types:
            t["on_passes"] = row("SELECT COUNT(DISTINCT pass_id) AS n FROM shop_pass_credits WHERE credit_type_id=?", t["id"])["n"]
        edit = request.args.get("edit")
        current = None
        if edit and edit != "new":
            current = next((t for t in types if str(t["id"]) == edit), None)
        return render_template("admin/credits.html", types=types, edit=edit, current=current,
                               outstanding={o["id"]: o for o in fp.outstanding(db())})

    @app.post("/admin/credits/<int:cid>/season")
    @admin_required
    def admin_credit_season(cid):
        t = row("SELECT * FROM shop_credit_types WHERE id=?", cid) or abort(404)
        db().execute("UPDATE shop_credit_types SET available=? WHERE id=?", (0 if t["available"] else 1, cid))
        flash("{}: {}".format(t["name"], "back in season" if not t["available"] else "marked out of season. Passes show it, but it can't be used."))
        return redirect(url_for("admin_credits"))

    @app.post("/admin/credits/<int:cid>/remove")
    @admin_required
    def admin_credit_remove(cid):
        t = row("SELECT * FROM shop_credit_types WHERE id=?", cid) or abort(404)
        db().execute("UPDATE shop_credit_types SET archived=1 WHERE id=?", (cid,))
        flash("{} won't be offered for new bundles. Passes that already have it keep it.".format(t["name"]))
        return redirect(url_for("admin_credits"))

    # ---- sale pages
    def slugify(text):
        s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
        return s[:60] or "sale"

    @app.route("/admin/pages", methods=["GET", "POST"])
    @admin_required
    def admin_pages():
        if request.method == "POST":
            f = request.form
            pid = f.get("id", type=int)
            title = f.get("title", "").strip()
            if not title:
                flash("Please give the page a title.")
                return redirect(url_for("admin_pages", edit=pid or "new"))
            slug = slugify(f.get("slug") or title)
            clash = row("SELECT id FROM shop_pages WHERE slug=? AND id != ? AND archived=0", slug, pid or 0)
            if clash:
                slug = slug + "-" + secrets.token_hex(2)
            vals = (title, slug, f.get("intro", "").strip(), f.get("starts_at", "").strip(), f.get("ends_at", "").strip(),
                    1 if f.get("hidden") else 0, f.get("sort", type=int) or 0)
            if pid:
                db().execute("UPDATE shop_pages SET title=?, slug=?, intro=?, starts_at=?, ends_at=?, hidden=?, sort=? WHERE id=?", vals + (pid,))
            else:
                db().execute("INSERT INTO shop_pages(title, slug, intro, starts_at, ends_at, hidden, sort, created_at) VALUES (?,?,?,?,?,?,?,?)",
                             vals + (now().isoformat(),))
            flash("Saved {}.".format(title))
            return redirect(url_for("admin_pages"))
        pages = [dict(p, state=page_state(p) if not p["hidden"] else "hidden",
                      count=row("SELECT COUNT(*) AS n FROM shop_products WHERE page_id=? AND archived=0", p["id"])["n"])
                 for p in rows("SELECT * FROM shop_pages WHERE archived=0 ORDER BY sort, id")]
        edit = request.args.get("edit")
        current = next((p for p in pages if str(p["id"]) == edit), None) if edit and edit != "new" else None
        return render_template("admin/pages.html", pages=pages, edit=edit, current=current)

    @app.post("/admin/pages/<int:pid>/toggle")
    @admin_required
    def admin_page_toggle(pid):
        p = row("SELECT * FROM shop_pages WHERE id=?", pid) or abort(404)
        db().execute("UPDATE shop_pages SET hidden=? WHERE id=?", (0 if p["hidden"] else 1, pid))
        flash("{} is now {}.".format(p["title"], "visible" if p["hidden"] else "hidden"))
        return redirect(url_for("admin_pages"))

    @app.post("/admin/pages/<int:pid>/remove")
    @admin_required
    def admin_page_remove(pid):
        p = row("SELECT * FROM shop_pages WHERE id=?", pid) or abort(404)
        db().execute("UPDATE shop_pages SET archived=1, hidden=1 WHERE id=?", (pid,))
        db().execute("UPDATE shop_products SET page_id=NULL WHERE page_id=?", (pid,))
        flash("Removed the page {}. Its products are still in your product list (now on the main shop page).".format(p["title"]))
        return redirect(url_for("admin_pages"))

    # ---- orders
    @app.get("/admin/orders")
    @admin_required
    def admin_orders():
        status = request.args.get("status", "paid")
        q = "SELECT * FROM shop_orders" + ("" if status == "all" else " WHERE status=?") + " ORDER BY id DESC LIMIT 300"
        orders = rows(q, *([] if status == "all" else [status]))
        for o in orders:
            o["lines"] = rows("SELECT * FROM shop_order_lines WHERE order_id=?", o["id"])
            o["needs"] = sorted({l["fulfillment"] for l in o["lines"] if l["fulfillment"] in ("ship", "pickup")})
        return render_template("admin/orders.html", orders=orders, status=status)

    @app.get("/admin/orders/<code>")
    @admin_required
    def admin_order(code):
        o = order_by_code(code)
        return render_template("admin/order.html", o=o)

    @app.post("/admin/orders/<code>/fulfilled")
    @admin_required
    def admin_order_fulfilled(code):
        o = order_by_code(code)
        undo = request.form.get("undo") == "1"
        db().execute("UPDATE shop_orders SET fulfilled_at=?, fulfilled_by=? WHERE id=?", (None if undo else now().isoformat(), "staff", o["id"]))
        if not undo and request.form.get("tell") == "1":
            ships = any(l["fulfillment"] == "ship" for l in o["lines"])
            send_email(o["email"], "Your {} order {}".format(settings()["shop_name"], "has shipped" if ships else "is ready"),
                       "Hi {}! Your order {} {}.\n\n{}".format(o["name"], fp.pretty_code(o["code"]),
                                                              "is on its way" if ships else "is ready to pick up at the farm",
                                                              url_for("order", code=o["code"], _external=True)))
        flash("Marked {}.".format("not done yet" if undo else "done"))
        return redirect(url_for("admin_order", code=o["code"]))

    @app.get("/admin/orders.csv")
    @admin_required
    def admin_orders_csv():
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["Order", "Paid", "Name", "Email", "Phone", "Item", "Qty", "Each", "Fulfillment", "Ship to", "Order subtotal", "Shipping", "Tax", "Total", "Done"])
        for o in rows("SELECT * FROM shop_orders WHERE status='paid' ORDER BY id"):
            ship_to = ", ".join(x for x in [o["ship_name"], o["ship_line1"], o["ship_line2"], o["ship_city"], o["ship_state"], o["ship_zip"]] if x) \
                if o["ship_line1"] else ""
            for l in rows("SELECT * FROM shop_order_lines WHERE order_id=?", o["id"]):
                w.writerow([fp.pretty_code(o["code"]), (o["paid_at"] or "")[:16].replace("T", " "), o["name"], o["email"], o["phone"],
                            l["name"], l["qty"], l["unit_price"], l["fulfillment"], ship_to, o["subtotal"], o["shipping"], o["tax"],
                            o["total"], "yes" if o["fulfilled_at"] else ""])
        return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": 'attachment; filename="farm shop orders.csv"'})

    # ---- passes and hand-made codes
    @app.get("/admin/passes")
    @admin_required
    def admin_passes():
        q = request.args.get("q", "").strip()
        sql = "SELECT * FROM shop_passes"
        args = []
        if q:
            like = "%" + q + "%"
            sql += " WHERE code LIKE ? OR holder LIKE ? OR gift_for LIKE ? OR email LIKE ? OR note LIKE ? OR title LIKE ?"
            args = [like.replace("-", "")] + [like] * 5
        sql += " ORDER BY id DESC LIMIT 300"
        passes = rows(sql, *args)
        for p in passes:
            p["credits"] = fp.credits(db(), p["id"])
            p["problem"] = fp.pass_problem(p)
        return render_template("admin/passes.html", passes=passes, q=q)

    @app.route("/admin/passes/new", methods=["GET", "POST"])
    @admin_required
    def admin_pass_new():
        types = credit_types()
        if request.method == "POST":
            f = request.form
            credit_rows = []
            for i in range(8):
                ct, q = f.get("ct_{}".format(i), type=int), f.get("cq_{}".format(i), type=int)
                if ct and q and q > 0:
                    credit_rows.append((ct, q))
            title = f.get("title", "").strip() or "Gift pass"
            if not credit_rows:
                flash("Add at least one credit (for example 1 × Playdate).")
                return render_template("admin/pass_new.html", types=types, f=f)
            con = db()
            con.execute("BEGIN IMMEDIATE")
            p = fp.create_pass(con, title, credit_rows, holder=f.get("holder", "").strip(), gift_for=f.get("gift_for", "").strip(),
                               email=f.get("email", "").strip(), season_label=f.get("season_label", "").strip(),
                               valid_until=f.get("valid_until", "").strip(), note=f.get("note", "").strip(), source="made", by_whom="staff")
            con.execute("COMMIT")
            flash("Code {} is ready. Print the certificate or send the link.".format(fp.pretty_code(p["code"])))
            return redirect(url_for("pass_page", code=p["code"]))
        return render_template("admin/pass_new.html", types=types,
                               f={"season_label": "Good during the {} season".format(now().year + 1), "valid_until": "{}-12-31".format(now().year + 1),
                                  "ct_0": str(types[0]["id"]) if types else "", "cq_0": "1"})

    @app.route("/admin/scan", methods=["GET", "POST"])
    @admin_required
    def admin_scan():
        code = fp.clean_code(request.values.get("code", ""))
        if code:
            if fp.find_pass(db(), code):
                return redirect(url_for("pass_page", code=code))
            if row("SELECT 1 AS x FROM bookings WHERE code=?", code):
                return redirect(booking_url("/admin/checkin/" + code))
            flash("No pass or playdate ticket has the code {}.".format(fp.pretty_code(code)))
        return render_template("admin/scan.html")

    @app.route("/admin/settings", methods=["GET", "POST"])
    @admin_required
    def admin_settings():
        if request.method == "POST":
            f = request.form
            for k in ("shop_name", "intro", "terms", "pickup_note", "shop_hidden_message"):
                save_setting(k, f.get(k, "").strip() or DEFAULT_SETTINGS[k])
            for k in ("shipping_flat", "tax_rate"):
                v = f.get(k, "").strip().replace("$", "").replace("%", "")
                try:
                    save_setting(k, float(v) if v else (None if k == "tax_rate" else 0.0))
                except ValueError:
                    flash("{} should be a number.".format(k.replace("_", " ").capitalize()))
            flash("Shop settings saved.")
            return redirect(url_for("admin_settings"))
        return render_template("admin/settings.html", pd_tax=playdate_settings().get("tax_rate", 6.475),
                               own_tax=row("SELECT value FROM shop_settings WHERE key='tax_rate'"))

    @app.errorhandler(404)
    def not_found(e):
        return render_template("message.html", title="We couldn't find that", body="That page isn't here. It may have been taken down.",
                               link=url_for("home"), link_text="Go to the shop"), 404

    return app
