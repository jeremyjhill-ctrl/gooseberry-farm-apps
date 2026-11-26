"""Gooseberry Bridge Farm shop: passes, bundles and products that Staci sets up herself.

Runs beside the playdate booking app (see wsgi.py) and shares its database, so a pass bought
here can pay for a playdate booking there (playdates/farm_passes.py holds the shared rules).
It does not share anything with Mollie's bakery.

Customer pages: shop home, sale pages, product pages, cart, checkout (Stripe Checkout when the farm's
Stripe key is set, otherwise a TEST card stand-in), order confirmation, and each pass's own page with a QR.
Admin (/admin, same password as the playdate admin): storefront switches, products and bundles,
credit types, sale pages, orders, passes and hand-made codes, settings.

Settings (environment variables):
  ADMIN_PASSWORD        same password as the playdate admin
  SECRET_KEY            signs the login cookie (the shop uses its own cookie name)
  PLAYDATE_DB           on the Mac: the playdate database file (the shop's tables live in it)
  DATABASE_URL          on Render: the Postgres server (farm_apps database)
  R2_*                  Cloudflare R2 for product photos (same bucket as the bakery, "shop/" folder)
  FARM_STRIPE_SECRET_KEY      the farm's Stripe secret key (sk_test_... first, sk_live_... for real sales).
                              Blank = the test card stand-in (how it runs on the Mac).
  FARM_STRIPE_WEBHOOK_SECRET  from the farm's Stripe webhook for <site>/shop/stripe/webhook (whsec_...)
  SHIPPO_API_KEY              shipping labels through Shippo (shippo_test_... practice, shippo_live_... real postage)
"""
import base64
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template, request, send_from_directory,
                   session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "playdates"))
import dbcompat  # noqa: E402
import farm_mail  # noqa: E402  (real email when the shop's switch is on; records sent / waiting / not sent)
import farm_auth as fa  # noqa: E402  (personal logins)
import farm_build  # noqa: E402  (the "Build 1a2b3c4" line at the bottom of each page)
import farm_passes as fp  # noqa: E402
import farm_shippo  # noqa: E402  (shipping labels)

try:
    import qrcode
    import qrcode.image.svg
except ImportError:  # pragma: no cover
    qrcode = None

TEST_CARD_OK = "4242424242424242"
TEST_CARD_DECLINE = "4000000000000002"
HOLD_MINUTES = 30
STRIPE_PAGE_MINUTES = 35   # how long Stripe's payment page stays open (Stripe allows 30 minutes to 24 hours)
STRIPE_GRACE_MINUTES = 10  # items stay held this much longer, so a payment made at the last moment still finds its order
STRIPE_EVENTS = ("checkout.session.completed", "checkout.session.expired",
                 "checkout.session.async_payment_succeeded", "checkout.session.async_payment_failed")
PROCESSING_DAYS = 14       # a bank payment that's still clearing keeps its items held this long
MAX_PENDING_PER_VISITOR = 3
MAX_QUESTIONS = 3      # questions for the buyer per product (e.g. how to inscribe a book)
MAX_ANSWER = 500       # characters per answer

DEFAULT_SETTINGS = {
    "shop_name": "Gooseberry Bridge Farm Shop",
    "intro": "Passes, bundles and goodies from our little farm in Rogersville, Missouri.",
    "shop_hidden": True,       # starts hidden: customers see it only after Staci opens it (it's on the test card for now)
    "shop_hidden_message": "Our shop is closed for now. Check back soon, or follow us on Facebook and Instagram for the next sale.",
    "terms": ("All sales are final. Passes are good during the season shown on the pass and can't be exchanged for cash. "
              "Seasonal items like flower picking depend on the weather and may end early, so please use your credits accordingly. "
              "Playdates still need an arrival time: book online with your pass code when dates are released."),
    "tax_rate": None,          # blank = use the playdate app's sales tax rate
    "shipping_flat": 6.0,      # shipping for an item with no shipping profile (see Settings > Shipping)
    "ship_extra_item": 2.0,    # each extra item shipped in the same order adds this (the highest item's charge counts in full)
    "free_ship_over": None,    # free shipping when the shipped items total at least this ($); blank = never
    "pickup_note": "We'll email you when your order is ready to pick up at the farm.",
    "home_state": "MO",
    "shop_emails": False,      # send shop emails (receipts with pass links) for real; off = they only show in Messages
    "email_daily_limit": farm_mail.DEFAULT_DAILY_LIMIT,  # leaves room in Resend's free 100 a day for Pasture Keeper
    "ship_from": {"name": "Gooseberry Bridge Farm", "street1": "", "street2": "", "city": "Rogersville", "state": "MO",
                  "zip": "", "phone": "", "email": ""},  # the return address on shipping labels (email: Shippo requires one)
    "label_size": "PDF_4x6",   # PDF_4x6 for a 4x6 label printer, PDF for plain 8.5x11 paper
}
LABEL_SIZES = {"PDF_4x6": "4×6 label printer", "PDF": "Plain paper (8.5×11)"}


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

    stripe_key = os.environ.get("FARM_STRIPE_SECRET_KEY", "").strip()
    webhook_secret = os.environ.get("FARM_STRIPE_WEBHOOK_SECRET", "").strip()
    stripe = None
    if stripe_key:
        import stripe as stripe_lib  # only needed once real payments are on
        stripe = stripe_lib
    stripe_mode = "off" if not stripe_key else ("live" if "_live_" in stripe_key else "test")

    app = Flask(__name__, static_folder="static", static_url_path="/static", template_folder="templates")
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.secret_key = (os.environ.get("SECRET_KEY") or "local-test-only-change-me") + ":shop"
    app.config.update(SESSION_COOKIE_NAME="shop_session", SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=dt.timedelta(days=30), MAX_CONTENT_LENGTH=12 * 1024 * 1024,
                      # Photos arrive as text inside the form; Flask 3.1 otherwise refuses form fields over 500 KB.
                      MAX_FORM_MEMORY_SIZE=12 * 1024 * 1024,
                      TEMPLATES_AUTO_RELOAD=True)

    @app.errorhandler(413)
    def too_big(e):
        return render_template("message.html", title="That was too big to save",
                               body="Usually that's a very large photo. Try a smaller picture (or a screenshot of it) and "
                                    "save again. Nothing was changed."), 413
    login_tries = {}

    # ------------------------------------------------------------ database
    def connect():
        con = dbcompat.connect(db_path, timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        return con

    con = connect()
    fp.ensure_schema(con)
    if stripe_mode != "live" and not con.execute("SELECT 1 FROM shop_settings WHERE key='orders_tagged_test'").fetchone():
        # Everything ordered before the shop took real money was a test: tag it once (Nov 21, 2026).
        con.execute("UPDATE shop_orders SET test=1")
        con.execute("INSERT OR REPLACE INTO shop_settings(key, value) VALUES ('orders_tagged_test', 'true')")
        if hasattr(con, "commit"):
            con.commit()
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
        return request.host_url.rstrip("/") + path  # booking lives on the same address as the shop

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
                        and (not settings()["shop_hidden"] or is_admin()))  # staff can test-buy while it's hidden
        p["ship_profile"] = ship_profile(p.get("ship_profile_id"))
        p["ship_charge"] = ship_charge(p)
        return p

    def ship_profiles(include_archived=False):
        return rows("SELECT * FROM shop_ship_profiles" + ("" if include_archived else " WHERE archived=0") + " ORDER BY sort, id")

    def ship_profile(pid):
        if not pid:
            return None
        cache = g.setdefault("_ship_profiles", {})
        if pid not in cache:
            cache[pid] = row("SELECT * FROM shop_ship_profiles WHERE id=? AND archived=0", pid)
        return cache[pid]

    def ship_charge(p):
        """What shipping one of this product costs the buyer: its profile's price, or the standard charge."""
        prof = ship_profile(p.get("ship_profile_id")) if hasattr(p, "get") else None
        return round(float(prof["price"] if prof else (settings()["shipping_flat"] or 0)), 2)

    def visible_products(page_id=None, include_upcoming=False):
        q = "SELECT * FROM shop_products WHERE archived = 0 AND hidden = 0 AND "
        q += "page_id = ?" if page_id else "(page_id IS NULL OR page_id = 0)"
        q += " ORDER BY sort, id"
        out = [decorate(p) for p in rows(q, *([page_id] if page_id else []))]
        return [p for p in out if include_upcoming or p["state"] in ("open", "ended")]

    CART_KEY = re.compile(r"^\d+(:[0-9a-f]{8})?$")  # "12", or "12:1a2b3c4d" when the buyer answered the item's questions

    def cart():
        c = session.get("cart") or {}
        return {str(k): int(v) for k, v in c.items() if CART_KEY.match(str(k)) and int(v) > 0}

    def cart_answers():
        return session.get("cart_answers") or {}

    def pid_of(key):
        return int(str(key).split(":")[0])

    def cart_lines():
        lines = []
        answers = cart_answers()
        for key, qty in cart().items():
            p = row("SELECT * FROM shop_products WHERE id=?", pid_of(key))
            if not p:
                continue
            p = decorate(p)
            lines.append({"key": key, "product": p, "qty": qty, "amount": round(float(p["price"]) * qty, 2),
                          "answers": answers.get(key, [])})
        return lines

    def questions(p):
        """The product's questions for the buyer: [{label, long, required}], at most 3."""
        try:
            got = json.loads((p.get("custom_fields") if hasattr(p, "get") else "") or "[]")
            return [{"label": str(x.get("label", ""))[:200], "long": bool(x.get("long")), "required": bool(x.get("required"))}
                    for x in got if str(x.get("label", "")).strip()][:MAX_QUESTIONS]
        except (ValueError, TypeError, AttributeError):
            return []

    def questions_from_form(f):
        qs = []
        for i in range(MAX_QUESTIONS):
            label = f.get("q_label_{}".format(i), "").strip()[:200]
            if label:
                qs.append({"label": label, "long": f.get("q_long_{}".format(i)) == "1", "required": bool(f.get("q_req_{}".format(i)))})
        return json.dumps(qs) if qs else ""

    def answers_of(l):
        """A cart line's or order line's answers: [{q, a}]."""
        a = l.get("answers") if hasattr(l, "get") else None
        if isinstance(a, list):
            return a
        try:
            return json.loads(a or "[]")
        except (ValueError, TypeError):
            return []

    def how_for(p, how):
        """How this item gets to the buyer: items that can be shipped or picked up follow the buyer's choice."""
        if p["fulfillment"] == "both":
            return how if how in ("ship", "pickup") else ""
        return p["fulfillment"]

    def totals(lines, ship_state="", how=""):
        s = settings()
        sub = round(sum(l["amount"] for l in lines), 2)
        choice = any(l["product"]["fulfillment"] == "both" for l in lines)
        ship_always = any(l["product"]["fulfillment"] == "ship" for l in lines)
        ships = any(how_for(l["product"], how) == "ship" for l in lines)
        shipping = shipping_for(lines, how) if ships else 0.0
        state = (ship_state or "").strip().upper()
        home = (s.get("home_state") or "MO").upper()
        taxable = 0.0
        for l in lines:
            p = l["product"]
            if not p["taxable"]:
                continue
            if how_for(p, how) == "ship" and state and state != home:
                continue  # shipped out of state: no Missouri tax (mirrors how Square handled it)
            taxable += l["amount"]
        tax = round(taxable * float(s["tax_rate"]) / 100.0, 2)
        return {"sub": sub, "shipping": shipping, "tax": tax, "total": round(sub + shipping + tax, 2), "ships": ships,
                "choice": choice, "ship_always": ship_always, "how": how if choice else "",
                "flat": shipping_for(lines, "ship"), "free_over": s.get("free_ship_over")}

    def shipping_for(lines, how):
        """The buyer's shipping: the highest item's charge in full, plus the add-on for every other item shipped,
        and nothing once the shipped items reach the free-shipping amount."""
        s = settings()
        charges, value = [], 0.0
        for l in lines:
            if how_for(l["product"], how) == "ship":
                charges += [l["product"]["ship_charge"]] * int(l["qty"])
                value += l["amount"]
        if not charges:
            return 0.0
        free = s.get("free_ship_over")
        if free not in (None, "") and value >= float(free):
            return 0.0
        return round(max(charges) + float(s.get("ship_extra_item") or 0) * (len(charges) - 1), 2)

    def housekeeping():
        db().execute("UPDATE shop_orders SET status='expired' WHERE status='pending' AND hold_expires < ?", (now().isoformat(),))

    def send_email(to, subject, body):
        """Same as playdate messages: a copy in Playdate admin > Messages, real email when PLAYDATE_EMAILS=on."""
        if not to:
            return
        cur = db().execute("INSERT INTO outbox(booking_id, channel, recipient, subject, body, created_at) VALUES (NULL, 'email', ?, ?, ?, ?)",
                           (to, subject, body, now().isoformat()))
        farm_mail.deliver(db(), cur.lastrowid, "shop")  # records sent / waiting / not sent on the message

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

    def photo_view(p):
        """How a product photo sits in its box: fit 'fill' (cover, drag + zoom) or 'whole' (nothing cut off)."""
        v = {"fit": "fill", "x": 50.0, "y": 50.0, "zoom": 1.0}
        try:
            got = json.loads((p.get("photo_view") if hasattr(p, "get") else "") or "{}")
            if got.get("fit") in ("fill", "whole"):
                v["fit"] = got["fit"]
            v["x"] = min(100.0, max(0.0, float(got.get("x", 50))))
            v["y"] = min(100.0, max(0.0, float(got.get("y", 50))))
            v["zoom"] = min(4.0, max(1.0, float(got.get("zoom", 1))))
        except (ValueError, TypeError, AttributeError):
            pass
        return v

    def photo_view_from_form(f):
        def n(k, lo, hi, d):
            try:
                return round(min(hi, max(lo, float(f.get(k, d)))), 2)
            except (TypeError, ValueError):
                return d
        return json.dumps({"fit": "whole" if f.get("photo_fit") == "whole" else "fill",
                           "x": n("photo_x", 0, 100, 50), "y": n("photo_y", 0, 100, 50), "zoom": n("photo_zoom", 1, 4, 1)})

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
                                 booking_url=booking_url, is_admin=is_admin, photo_view=photo_view,
                                 questions=questions, answers_of=answers_of,
                                 build_footer=farm_build.footer)

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
        return render_template("home.html", pages=pages, products=visible_products(),
                               booking_open=not playdate_settings().get("booking_hidden"))

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
        answers, missing = [], []
        for i, q in enumerate(questions(p)):
            a = request.form.get("answer_{}".format(i), "").strip()[:MAX_ANSWER]
            if q["required"] and not a:
                missing.append(q["label"])
            if a:
                answers.append({"q": q["label"], "a": a})
        if missing:
            flash("Please answer: {}".format("; ".join(missing)))
            return redirect(url_for("product", pid=pid))
        # The same item with different answers (say, two books inscribed differently) is its own cart line.
        key = str(pid) if not answers else "{}:{}".format(
            pid, hashlib.sha1(json.dumps(answers, sort_keys=True).encode()).hexdigest()[:8])
        c = cart()
        others = sum(n for k, n in c.items() if pid_of(k) == pid and k != key)
        want = c.get(key, 0) + qty
        if p["per_order_limit"]:
            want = min(want, int(p["per_order_limit"]) - others)
        if p["left"] is not None:
            want = min(want, p["left"] - others)
        if want <= 0:
            flash("You already have as many {} in your cart as you can buy.".format(p["name"]))
            return redirect(url_for("cart_view"))
        c[key] = want
        session["cart"] = c
        if answers:
            saved = cart_answers()
            saved[key] = answers
            session["cart_answers"] = saved
        flash("Added {} to your cart.".format(p["name"]))
        return redirect(url_for("cart_view"))

    @app.post("/cart/update")
    def cart_update():
        c = cart()
        for key in list(c):
            v = request.form.get("qty_" + key, type=int)
            if v is not None:
                p = row("SELECT per_order_limit FROM shop_products WHERE id=?", pid_of(key))
                c[key] = max(0, min(int(p["per_order_limit"]) if p and p["per_order_limit"] else 20, v))
        session["cart"] = {k: v for k, v in c.items() if v > 0}
        session["cart_answers"] = {k: v for k, v in cart_answers().items() if k in session["cart"]}
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
            how = f.get("how", "")
            t = totals(lines, ship["state"], how)
            if t["choice"] and how not in ("ship", "pickup"):
                errors.append("Please choose whether to have it shipped or pick it up at the farm.")
            if not t["ships"]:
                ship = {k: "" for k in ship}  # picking up: don't keep an address they may have started typing
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
                    wanted = {}
                    for l in lines:
                        wanted[l["product"]["id"]] = wanted.get(l["product"]["id"], 0) + l["qty"]
                    for pid, n in wanted.items():
                        p = decorate(row("SELECT * FROM shop_products WHERE id=?", pid))
                        if not p["buyable"] or (p["left"] is not None and p["left"] < n):
                            errors.append("Sorry, {} just sold out or isn't on sale anymore.".format(p["name"]) if p["left"] == 0 or not p["buyable"]
                                          else "Only {} {} left.".format(p["left"], p["name"]))
                        elif p["per_order_limit"] and n > int(p["per_order_limit"]):
                            errors.append("You can buy up to {} {} per order.".format(p["per_order_limit"], p["name"]))
                    if errors:
                        con.execute("ROLLBACK")
                    else:
                        code = fp.new_code(con, "shop_orders")
                        terms = terms_for(lines, s)
                        cur = con.execute("""INSERT INTO shop_orders(code, status, name, email, phone, ship_name, ship_line1, ship_line2,
                                ship_city, ship_state, ship_zip, subtotal, shipping, tax, total, agreed_terms, agreed_at, created_at,
                                hold_expires, hold_ip, test) VALUES (?, 'pending', ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                          (code, name, email, phone, ship["name"] or name, ship["line1"], ship["line2"], ship["city"],
                                           ship["state"].upper(), ship["zip"], t["sub"], t["shipping"], t["tax"], t["total"],
                                           terms, now().isoformat(), now().isoformat(),
                                           (now() + dt.timedelta(minutes=HOLD_MINUTES)).isoformat(), ip,
                                           0 if stripe_mode == "live" else 1))  # no real money yet: a test order
                        oid = cur.lastrowid
                        for l in lines:
                            p = l["product"]
                            gifts = [f.get("gift_{}_{}".format(l["key"], i), "").strip() for i in range(l["qty"])] if p["kind"] == "pass" else []
                            con.execute("""INSERT INTO shop_order_lines(order_id, product_id, kind, name, unit_price, qty, fulfillment, taxable, gift_for,
                                           answers) VALUES (?,?,?,?,?,?,?,?,?,?)""", (oid, p["id"], p["kind"], p["name"], p["price"], l["qty"],
                                                                         how_for(p, how), p["taxable"], json.dumps(gifts),
                                                                         json.dumps(l["answers"]) if l["answers"] else ""))
                        con.execute("COMMIT")
                        return redirect(url_for("pay", code=code))
                except Exception:
                    con.execute("ROLLBACK")
                    raise
        state = f.get("ship_state", "")
        t = totals(lines, state, f.get("how", ""))
        both_ways = {h: totals(lines, state, h) for h in ("ship", "pickup")} if t["choice"] else {}
        choosers = [l["product"]["name"] for l in lines if l["product"]["fulfillment"] == "both"]
        return render_template("checkout.html", lines=lines, t=t, both_ways=both_ways, choosers=choosers, errors=errors, f=f,
                               terms=terms_for(lines, s).split("\n\n"))

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

    # ------------------------------------------------------------ paying
    def cents(x):
        return int(round(float(x or 0) * 100))

    def sdict(obj):
        """Stripe's library hands back its own objects (not dicts, so .get() fails on them); this makes plain dicts."""
        return obj.to_dict() if hasattr(obj, "to_dict") else obj

    def finish_order(oid, method, payment="", amount=None):
        """Marks an order paid, makes its passes and emails the receipt: exactly once, however many times it's called
        (Stripe's message and the buyer's return can arrive together). Returns True only for the call that did it."""
        con = db()
        con.execute("BEGIN IMMEDIATE")
        try:
            fresh = dict(con.execute("SELECT * FROM shop_orders WHERE id=?", (oid,)).fetchone())
            if fresh["status"] == "paid":
                con.execute("ROLLBACK")
                return False
            notes = []
            if method == "test_card" and (fresh["status"] != "pending" or (fresh["hold_expires"] or "") < now().isoformat()):
                con.execute("ROLLBACK")
                return False
            if fresh["status"] != "pending":
                # Money was taken, so the order stands, but its items may have gone back on sale in the meantime.
                notes.append("Paid after the checkout hold ran out: check that the items are still in stock.")
            if amount is not None and int(amount) != cents(fresh["total"]):
                notes.append("Stripe charged {} but the order total is {}.".format(money(int(amount) / 100.0), money(fresh["total"])))
            con.execute("""UPDATE shop_orders SET status='paid', paid_at=?, method=?, hold_expires=NULL, stripe_payment=?,
                           amount_paid=?, pay_note=? WHERE id=?""",
                        (now().isoformat(), method, payment or "", amount, " ".join(notes), oid))
            for l in [dict(r) for r in con.execute("SELECT * FROM shop_order_lines WHERE order_id=? ORDER BY id", (oid,))]:
                if l["kind"] != "pass":
                    continue
                p = row("SELECT * FROM shop_products WHERE id=?", l["product_id"]) or {}
                credit_rows = [(c["credit_type_id"], c["qty"]) for c in rows(
                    "SELECT credit_type_id, qty FROM shop_product_credits WHERE product_id=? ORDER BY id", l["product_id"])]
                gifts = json.loads(l["gift_for"] or "[]")
                for i in range(int(l["qty"])):
                    fp.create_pass(con, l["name"], credit_rows, holder=fresh["name"], gift_for=(gifts[i] if i < len(gifts) else ""),
                                   email=fresh["email"], season_label=p.get("season_label", ""), valid_until=p.get("valid_until", ""),
                                   source="sale", order_id=oid, product_id=l["product_id"], by_whom="shop")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        email_order(order_by_code(fresh["code"]))
        return True

    def settle_with_stripe(o):
        """Asks Stripe about the order's payment page and finishes the order if it's been paid.
        Returns Stripe's session (a dict) or None if there isn't one or Stripe couldn't be reached."""
        sid = o.get("stripe_session") or ""
        if not stripe or not sid.startswith("cs_") or o["status"] == "paid":
            return None
        try:
            cs = sdict(stripe.checkout.Session.retrieve(sid, api_key=stripe_key))
        except Exception:  # noqa: BLE001
            app.logger.exception("Stripe lookup failed for order %s", o["code"])
            return None
        if cs.get("payment_status") == "paid":
            finish_order(o["id"], "stripe", cs.get("payment_intent") or "", cs.get("amount_total"))
        elif cs.get("status") == "complete":
            note_processing(o["id"])
        return cs

    def note_processing(oid):
        """The buyer finished Stripe's page with a payment that takes days to clear (a bank payment): keep their items
        held while it clears. Stripe's async_payment_succeeded message then finishes the order."""
        db().execute("UPDATE shop_orders SET hold_expires=?, pay_note=? WHERE id=? AND status='pending'",
                     ((now() + dt.timedelta(days=PROCESSING_DAYS)).isoformat(),
                      "Payment is still clearing (a bank payment). The passes and receipt go out when Stripe confirms it.", oid))

    def processing_page(o):
        return render_template("message.html", title="Your payment is processing",
                               body="Thanks, {}! Your bank payment is on its way, which can take a few business days. "
                                    "We'll email your receipt{} as soon as it clears.".format(
                                        o["name"].split(" ")[0], " and passes" if any(l["kind"] == "pass" for l in o["lines"]) else ""))

    def stripe_lines(o):
        items = []
        for l in o["lines"]:
            product = {"name": l["name"]}
            said = "; ".join("{}: {}".format(x["q"], x["a"]) for x in answers_of(l))
            if said:
                product["description"] = said[:400]
            items.append({"quantity": int(l["qty"]), "price_data": {"currency": "usd", "unit_amount": cents(l["unit_price"]),
                                                                    "product_data": product}})
        if o["shipping"]:
            items.append({"quantity": 1, "price_data": {"currency": "usd", "unit_amount": cents(o["shipping"]),
                                                        "product_data": {"name": "Shipping"}}})
        if o["tax"]:
            items.append({"quantity": 1, "price_data": {"currency": "usd", "unit_amount": cents(o["tax"]),
                                                        "product_data": {"name": "Sales tax"}}})
        if sum(x["quantity"] * x["price_data"]["unit_amount"] for x in items) != cents(o["total"]):
            # Rounding never should, but if the pieces don't add up exactly, charge the order total as one line.
            items = [{"quantity": 1, "price_data": {"currency": "usd", "unit_amount": cents(o["total"]),
                                                    "product_data": {"name": "Order {}".format(fp.pretty_code(o["code"]))}}}]
        return items

    def pay_with_stripe(o):
        cs = settle_with_stripe(o)
        o = order_by_code(o["code"])
        if o["status"] == "paid":
            return redirect(url_for("order", code=o["code"]))
        if cs and cs.get("status") == "open" and cs.get("url"):
            return redirect(cs["url"])  # their payment page is still open: back to it, never a second one
        if cs and cs.get("status") == "complete" and o["status"] == "pending":
            return processing_page(o)  # paid with something that takes days to clear: never a second payment page
        if o["status"] != "pending":
            return timed_out()
        if (o.get("stripe_session") or "").startswith("cs_") and cs is None:
            return could_not_start(o)  # couldn't ask Stripe about their open page: never risk a second one
        if cents(o["total"]) == 0:
            finish_order(o["id"], "free")
            return redirect(url_for("order", code=o["code"]))
        if cents(o["total"]) < 50:
            return render_template("message.html", title="That's too small to pay by card",
                                   body="Card payments need to be at least 50¢. Please add something to your order.",
                                   link=url_for("cart_view"), link_text="Back to my cart")
        # Claim the order so two clicks (or two tabs) can't open two payment pages for it. A claim older than
        # 30 seconds belonged to a request that died part way, so it's fair game.
        old = o.get("stripe_session") or ""
        if old.startswith("starting:") and time.time() - float(old.split(":")[1] or 0) < 30:
            for _ in range(15):  # another click is opening it right now: wait for it, then follow it
                time.sleep(0.3)
                sid = (row("SELECT stripe_session FROM shop_orders WHERE id=?", o["id"]) or {}).get("stripe_session") or ""
                if sid.startswith("cs_"):
                    return redirect(url_for("pay", code=o["code"]))
            return could_not_start(o)
        mine = "starting:{:.3f}".format(time.time())
        if db().execute("UPDATE shop_orders SET stripe_session=? WHERE id=? AND status='pending' AND COALESCE(stripe_session, '')=?",
                        (mine, o["id"], old)).rowcount != 1:
            return redirect(url_for("pay", code=o["code"]))  # someone else just claimed it: start over and follow theirs
        page_ends = now() + dt.timedelta(minutes=STRIPE_PAGE_MINUTES)
        try:
            cs = sdict(stripe.checkout.Session.create(
                api_key=stripe_key,
                mode="payment",
                customer_email=o["email"],
                client_reference_id=o["code"],
                line_items=stripe_lines(o),
                metadata={"order": o["code"], "app": "farm-shop"},
                payment_intent_data={"description": "{} order {}: {}".format(settings()["shop_name"], fp.pretty_code(o["code"]), o["name"]),
                                     "metadata": {"order": o["code"], "app": "farm-shop"}},
                expires_at=int(page_ends.timestamp()),
                success_url=url_for("order", code=o["code"], _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
                cancel_url=url_for("pay_cancelled", code=o["code"], _external=True),
            ))
        except Exception as e:  # noqa: BLE001
            app.logger.exception("Stripe checkout failed for order %s", o["code"])
            db().execute("UPDATE shop_orders SET stripe_session=? WHERE id=? AND stripe_session=?", (old, o["id"], mine))
            return could_not_start(o, getattr(e, "user_message", None) or str(e))
        held = (page_ends + dt.timedelta(minutes=STRIPE_GRACE_MINUTES)).isoformat()
        db().execute("UPDATE shop_orders SET stripe_session=?, hold_expires=? WHERE id=?", (cs["id"], held, o["id"]))
        return redirect(cs["url"])

    def timed_out():
        return render_template("message.html", title="This checkout timed out",
                               body="We held your items for {} minutes and they've been put back. You weren't charged.".format(HOLD_MINUTES),
                               link=url_for("cart_view"), link_text="Back to my cart")

    def could_not_start(o, reason=""):
        body = "Nothing was charged. Please try again in a minute."
        if reason and is_admin():  # staff see Stripe's own reason; customers don't
            body += "\n\nFor staff, Stripe said: " + str(reason)[:600]
        return render_template("message.html", title="We couldn't open the payment page", body=body,
                               link=url_for("pay", code=o["code"]), link_text="Try again")

    @app.route("/pay/<code>", methods=["GET", "POST"])
    def pay(code):
        """Stripe Checkout when the farm's key is set; otherwise the TEST card stand-in."""
        o = order_by_code(code)
        if o["status"] == "paid":
            return redirect(url_for("order", code=o["code"]))
        if stripe:
            return pay_with_stripe(o)
        if o["status"] != "pending":
            return timed_out()
        error = None
        if request.method == "POST":
            card = "".join(ch for ch in request.form.get("card", "") if ch.isdigit())
            if card == TEST_CARD_DECLINE:
                error = "Your card was declined. (Test decline card.) Try 4242 4242 4242 4242."
            elif card != TEST_CARD_OK:
                error = "Test mode: use card 4242 4242 4242 4242 (any future date, any 3 digits)."
            else:
                finish_order(o["id"], "test_card")
                session.pop("cart", None)
                session.pop("cart_answers", None)
                return redirect(url_for("order", code=o["code"]))
        return render_template("pay.html", o=o, error=error)

    @app.get("/pay/<code>/cancelled")
    def pay_cancelled(code):
        """The buyer backed out of Stripe's page: close it and put the items back on sale. Their cart is still here."""
        o = order_by_code(code)
        if stripe and o["status"] == "pending" and (o.get("stripe_session") or "").startswith("cs_"):
            try:
                stripe.checkout.Session.expire(o["stripe_session"], api_key=stripe_key)
                db().execute("UPDATE shop_orders SET status='cancelled', hold_expires=NULL WHERE id=? AND status='pending'", (o["id"],))
            except Exception:  # noqa: BLE001  already paid or already closed: ask Stripe which
                settle_with_stripe(o)
            if order_by_code(code)["status"] == "paid":
                return redirect(url_for("order", code=o["code"]))
        flash("No payment was made. Your cart is just as you left it.")
        return redirect(url_for("cart_view"))

    @app.post("/stripe/webhook")
    def stripe_webhook():
        """Stripe's messages about the farm's payment pages. Set it up in Stripe for the four STRIPE_EVENTS."""
        if not stripe or not webhook_secret:
            return "Stripe isn't set up on this server.", 400
        try:
            event = sdict(stripe.Webhook.construct_event(request.get_data(), request.headers.get("Stripe-Signature", ""), webhook_secret))
        except Exception:  # noqa: BLE001  not really from Stripe (or the secret is wrong)
            return "Signature check failed.", 400
        cs = (event.get("data") or {}).get("object") or {}
        code = (cs.get("metadata") or {}).get("order") or cs.get("client_reference_id") or ""
        o = row("SELECT * FROM shop_orders WHERE code=?", code) if code else None
        if not o:
            return {"received": True, "note": "not a farm shop order"}
        kind = event.get("type")
        if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded") and cs.get("payment_status") == "paid":
            finish_order(o["id"], "stripe", cs.get("payment_intent") or "", cs.get("amount_total"))
        elif kind == "checkout.session.completed":
            note_processing(o["id"])
        elif kind == "checkout.session.async_payment_failed" and o["status"] == "pending":
            db().execute("UPDATE shop_orders SET status='cancelled', hold_expires=NULL, pay_note=? WHERE id=? AND status='pending'",
                         ("The buyer's bank payment didn't go through, so nothing was paid and the items went back on sale.", o["id"]))
        elif event.get("type") == "checkout.session.expired" and o["status"] == "pending" and o.get("stripe_session") == cs.get("id"):
            db().execute("UPDATE shop_orders SET status='expired', hold_expires=NULL WHERE id=? AND status='pending'", (o["id"],))
        return {"received": True}

    def email_order(o):
        s = settings()
        lines = ["Hi {}! Thank you for your order.".format(o["name"]), ""]
        for l in o["lines"]:
            lines.append("{} × {}  {}".format(l["qty"], l["name"], money(l["unit_price"] * l["qty"])))
            for a in answers_of(l):
                lines.append("    {}: {}".format(a["q"], a["a"]))
        lines.append("Total paid: {}".format(money(o["total"])))
        if o["passes"]:
            lines += ["", "Your pass{}: each one has a QR code to show at the farm.".format("es" if len(o["passes"]) > 1 else "")]
            for p in o["passes"]:
                who = " (for {})".format(p["gift_for"]) if p["gift_for"] else ""
                lines.append("  {}{}: {}".format(p["title"], who, url_for("pass_page", code=p["code"], _external=True)))
            if playdate_settings().get("booking_hidden"):
                lines += ["", "Playdate booking opens soon. When it does, pick a time on our booking page and enter your pass code at checkout:",
                          "  " + booking_url()]
            else:
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
            settle_with_stripe(o)  # back from Stripe's page: don't wait for Stripe's message
            o = order_by_code(code)
        if o["status"] != "paid":
            return redirect(url_for("pay", code=o["code"]))
        if request.args.get("session_id") and request.args.get("session_id") == o.get("stripe_session"):
            session.pop("cart", None)  # this buyer just paid: empty their cart
            session.pop("cart_answers", None)
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
                               all_types=credit_types() if is_admin() else [],
                               problem=fp.pass_problem(p), can_book=can_book,
                               booking_open=not playdate_settings().get("booking_hidden"),
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

    def staff_name():
        u = fa.current_user(request)
        return ((u.get("name") or u.get("email")) if u else "") or "staff"

    @app.post("/pass/<code>/adjust")
    @admin_required
    def pass_adjust(code):
        """Set any credit's count on a pass (a playdate booked by phone, a mistake, a goodwill extra), logged with who and why."""
        p = pass_or_404(code)
        f = request.form
        wanted = {}
        for c in fp.credits(db(), p["id"]):
            v = f.get("left_{}".format(c["credit_type_id"]), "").strip()
            if v != "":
                try:
                    wanted[c["credit_type_id"]] = int(v)
                except ValueError:
                    flash("Counts should be whole numbers.")
                    return redirect(url_for("pass_page", code=p["code"]) + "#adjust")
        con = db()
        con.execute("BEGIN IMMEDIATE")
        try:
            changed, err = fp.adjust_credits(con, p, wanted, f.get("reason", ""), staff_name(),
                                             add_type=f.get("add_type", type=int), add_qty=f.get("add_qty", 0, type=int) or 0)
            con.execute("ROLLBACK" if err or not changed else "COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        flash(err or ("Saved: " + "; ".join(changed) + "." if changed else "Nothing changed."))
        return redirect(url_for("pass_page", code=p["code"]) + ("#adjust" if err else ""))

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
        paid = rows("SELECT * FROM shop_orders WHERE status='paid' AND test=0 ORDER BY id DESC")
        return render_template("admin/home.html", pd=pd, recent=paid[:8], orders_count=len(paid),
                               revenue=round(sum(o["subtotal"] for o in paid), 2),
                               passes_sold=row("SELECT COUNT(*) AS n FROM shop_passes WHERE source='sale' AND COALESCE(order_id, 0) NOT IN "
                                               "(SELECT id FROM shop_orders WHERE test=1)")["n"],
                               passes_made=row("SELECT COUNT(*) AS n FROM shop_passes WHERE source='made'")["n"],
                               outstanding=fp.outstanding(db()),
                               to_ship=row("""SELECT COUNT(DISTINCT o.id) AS n FROM shop_orders o JOIN shop_order_lines l ON l.order_id=o.id
                                              WHERE o.status='paid' AND o.test=0 AND o.fulfilled_at IS NULL AND l.fulfillment IN ('ship','pickup')""")["n"],
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
                "one_of_a_kind": 1 if f.get("one_of_a_kind") and kind != "pass" else 0,
                "hidden": 1 if f.get("hidden") else 0, "page_id": f.get("page_id", type=int) or None,
                "fulfillment": "pass" if kind == "pass" else {(True, True): "both", (True, False): "ship", (False, True): "pickup",
                                                              (False, False): "none"}[(bool(f.get("can_ship")), bool(f.get("can_pickup")))],
                "taxable": 1 if f.get("taxable") else 0, "season_label": f.get("season_label", "").strip(),
                "valid_until": f.get("valid_until", "").strip(), "terms": f.get("terms", "").strip(),
                "sort": num("sort", int) or 0, "photo_view": photo_view_from_form(f), "custom_fields": questions_from_form(f),
            }
            if kind != "pass":
                lb, oz = num("ship_lb"), num("ship_oz")
                fields["ship_weight_oz"] = round((lb or 0) * 16 + (oz or 0), 2) if (lb or oz) else None
                dims = [num("box_" + d) for d in "lwh"]
                if any(dims) and not all(d and d > 0 for d in dims):
                    errors.append("Please fill in all three box measurements (length, width, height), or leave them all blank.")
                fields["ship_box"] = "x".join("{:g}".format(d) for d in dims) if all(dims) else ""
                fields["ship_profile_id"] = f.get("ship_profile_id", type=int) or None
            if fields["one_of_a_kind"]:
                fields["stock_limit"], fields["per_order_limit"] = 1, 1  # just the one
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
        return render_template("admin/product.html", p=p, kind=kind, errors=errors, types=credit_types(), profiles=ship_profiles(),
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
        if status == "test":
            orders = rows("SELECT * FROM shop_orders WHERE test=1 ORDER BY id DESC LIMIT 300")
        else:
            q = "SELECT * FROM shop_orders WHERE test=0" + ("" if status == "all" else " AND status=?") + " ORDER BY id DESC LIMIT 300"
            orders = rows(q, *([] if status == "all" else [status]))
        for o in orders:
            o["lines"] = rows("SELECT * FROM shop_order_lines WHERE order_id=?", o["id"])
            o["needs"] = sorted({l["fulfillment"] for l in o["lines"] if l["fulfillment"] in ("ship", "pickup")})
        return render_template("admin/orders.html", orders=orders, status=status,
                               tests=row("SELECT COUNT(*) AS n FROM shop_orders WHERE test=1")["n"])

    def delete_orders(where, *args):
        """Removes orders for good, with their lines, passes (and those passes' credits and history) and labels.
        Only ever called for test orders."""
        ids = [r["id"] for r in rows("SELECT id FROM shop_orders WHERE test=1 AND " + where, *args)]
        con = db()
        con.execute("BEGIN IMMEDIATE")
        try:
            for oid in ids:
                for p in rows("SELECT id FROM shop_passes WHERE order_id=?", oid):
                    for t in ("shop_pass_credits", "shop_pass_uses", "shop_pass_holds"):
                        con.execute("DELETE FROM {} WHERE pass_id=?".format(t), (p["id"],))
                    con.execute("DELETE FROM shop_passes WHERE id=?", (p["id"],))
                for t in ("shop_order_lines", "shop_labels"):
                    con.execute("DELETE FROM {} WHERE order_id=?".format(t), (oid,))
                con.execute("DELETE FROM shop_orders WHERE id=? AND test=1", (oid,))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        return len(ids)

    @app.post("/admin/orders/delete-tests")
    @admin_required
    def admin_orders_delete_tests():
        n = delete_orders("1=1")
        flash("Deleted {} test order{}.".format(n, "" if n == 1 else "s") if n else "There were no test orders.")
        return redirect(url_for("admin_orders"))

    @app.post("/admin/orders/<code>/delete")
    @admin_required
    def admin_order_delete(code):
        o = order_by_code(code)
        if not o.get("test"):
            flash("Only test orders can be deleted. Refund a real order in Stripe instead.")
            return redirect(url_for("admin_order", code=o["code"]))
        delete_orders("id=?", o["id"])
        flash("Deleted test order {}.".format(fp.pretty_code(o["code"])))
        return redirect(url_for("admin_orders", status="test"))

    @app.get("/admin/orders/<code>")
    @admin_required
    def admin_order(code):
        o = order_by_code(code)
        if o["status"] != "paid" and settle_with_stripe(o):
            o = order_by_code(code)
        dash = ""
        if (o.get("stripe_payment") or "").startswith("pi_"):
            dash = "https://dashboard.stripe.com/{}payments/{}".format("test/" if stripe_mode == "test" else "", o["stripe_payment"])
        return render_template("admin/order.html", o=o, stripe_link=dash, labels=order_labels(o), label_problem=label_ready(o),
                               label_mode=farm_shippo.mode())

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

    # ---- shipping labels (Shippo)
    def parse_box(text):
        try:
            dims = [float(x) for x in str(text or "").lower().replace("×", "x").split("x")]
            return tuple(dims) if len(dims) == 3 and all(d > 0 for d in dims) else None
        except ValueError:
            return None

    def num_or_none(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def order_labels(o):
        return rows("SELECT * FROM shop_labels WHERE order_id=? ORDER BY id", o["id"])

    def shipping_lines(o):
        return [l for l in o["lines"] if l["fulfillment"] == "ship"]

    def parcel_guess(o):
        """The package for this order from its products: weights add up; the biggest box wins (the product's own box,
        else its shipping profile's box or USPS flat-rate box). Returns dict: weight (oz or None), box (L,W,H or None),
        template ('' or a USPS flat-rate token), prefer (a preferred label service or ''), missing (names of items with no
        weight), nobox (names of items with no box)."""
        out = {"weight": 0.0, "box": None, "template": "", "prefer": "", "missing": [], "nobox": [], "profiles": []}
        biggest = 0.0
        services = set()
        for l in shipping_lines(o):
            p = row("SELECT name, ship_weight_oz, ship_box, ship_profile_id FROM shop_products WHERE id=?", l["product_id"]) or {}
            prof = ship_profile(p.get("ship_profile_id"))
            services.add((prof or {}).get("service") or "")
            name = "{}: {}".format(l["name"], prof["name"] if prof else "standard shipping (no profile)")
            if name not in out["profiles"]:
                out["profiles"].append(name)
            if not p.get("ship_weight_oz"):
                out["missing"].append(l["name"])
            out["weight"] += float(p.get("ship_weight_oz") or 0) * int(l["qty"])
            own = parse_box(p.get("ship_box"))
            if own:
                box, template = own, ""
            elif prof and prof.get("template") in farm_shippo.TEMPLATES:
                box, template = farm_shippo.TEMPLATES[prof["template"]][1:], prof["template"]
            else:
                box, template = (parse_box(prof.get("box")) if prof else None), ""
            if not box:
                out["nobox"].append(l["name"])
                continue
            size = box[0] * box[1] * box[2]
            if size > biggest:
                biggest = size
                out.update(box=box, template=template)
        # A preferred service only counts when every item in the package prefers it: Media Mail is only allowed when
        # everything in the box is a book or other media, so a book shipped with a hat must not suggest it.
        out["prefer"] = services.pop() if len(services) == 1 else ""
        out["weight"] = round(out["weight"], 2) or None
        return out

    def ship_to(o):
        a = {"name": o["ship_name"] or o["name"], "street1": o["ship_line1"], "street2": o["ship_line2"] or "",
             "city": o["ship_city"], "state": o["ship_state"], "zip": o["ship_zip"], "country": "US"}
        if o.get("phone"):
            a["phone"] = o["phone"]
        return a

    def farm_email():
        """The farm's own email address, from the email setup (PLAYDATE_MAIL_FROM, e.g. "Farm <hello@...>")."""
        m = re.search(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", os.environ.get("PLAYDATE_MAIL_FROM", ""))
        return m.group(0) if m else ""

    def ship_from():
        s = settings()["ship_from"] or {}
        out = dict({k: (s.get(k) or "") for k in ("name", "street1", "street2", "city", "state", "zip", "phone", "email")}, country="US")
        out["email"] = out["email"] or farm_email()  # Shippo won't buy a label without the sender's email
        return out

    def ship_from_problem():
        s = ship_from()
        if not (s["street1"] and s["city"] and s["state"] and s["zip"]):
            return "Add the farm's return address in Settings > Shipping labels first."
        if not s["email"]:
            return "Add the farm's email to the return address in Settings > Shipping labels first (Shippo requires one)."
        return ""

    def label_ready(o):
        """Why this order can't have a label right now ('' when it can)."""
        if farm_shippo.mode() == "off":
            return "Shipping labels aren't set up yet (Settings > Shipping labels)."
        if o["status"] != "paid":
            return "Only paid orders get labels."
        if not shipping_lines(o) or not o["ship_line1"]:
            return "Nothing in this order is being shipped."
        return ship_from_problem()

    @app.route("/admin/orders/<code>/label", methods=["GET", "POST"])
    @admin_required
    def admin_label(code):
        """Prices for this order's package (preferred service first, then cheapest); one tap buys the label."""
        o = order_by_code(code)
        problem = label_ready(o)
        guess = parcel_guess(o)
        f = request.values
        weight = guess["weight"]
        if f.get("lb") or f.get("oz"):  # staff changed the weight on this page
            try:
                weight = round(float(f.get("lb") or 0) * 16 + float(f.get("oz") or 0), 2) or None
            except ValueError:
                weight = None
        template, box = guess["template"], guess["box"]
        if "template" in f or f.get("box"):  # staff picked a flat-rate box, or typed their own box, on this page
            template = f.get("template") if f.get("template") in farm_shippo.TEMPLATES else ""
            box = farm_shippo.TEMPLATES[template][1:] if template else parse_box(f.get("box"))
        rates, error = [], ""
        if not problem and weight and box:
            try:
                _, rates = farm_shippo.rates(ship_from(), ship_to(o), weight, box, template=template, prefer=guess["prefer"])
            except farm_shippo.ShippoError as e:
                error = str(e)
        return render_template("admin/label.html", o=o, problem=problem, rates=rates, error=error, weight=weight, box=box,
                               template=template, templates=farm_shippo.TEMPLATES, missing=guess["missing"], nobox=guess["nobox"],
                               profiles=guess["profiles"],
                               labels=[x for x in order_labels(o) if x["status"] == "bought"],
                               box_text="x".join("{:g}".format(d) for d in box) if box and not template else "",
                               label_mode=farm_shippo.mode(), label_size=LABEL_SIZES.get(settings()["label_size"], ""))

    @app.post("/admin/orders/<code>/label/buy")
    @admin_required
    def admin_label_buy(code):
        o = order_by_code(code)
        problem = label_ready(o)
        if problem:
            flash(problem)
            return redirect(url_for("admin_order", code=o["code"]))
        f = request.form.to_dict()
        if f.get("pick", "").isdigit():  # the label page: one form, a Buy button per price
            rid, amount, carrier, service = (f.get("rate_" + f["pick"], "").split("|", 3) + ["", "", "", ""])[:4]
            f.update(rate=rid, cost=amount, carrier=carrier, service=service)
        if any(x["status"] == "bought" for x in order_labels(o)) and f.get("another") != "1":
            flash("This order already has a label. Print that one, or void it first.")
            return redirect(url_for("admin_order", code=o["code"]))
        # One purchase at a time per order, so a double-tap can't buy (and pay for) two labels.
        mine = "buying:{:.3f}".format(time.time())
        stale = "buying:{:.3f}".format(time.time() - 90)
        con = db()
        if con.execute("UPDATE shop_orders SET label_claim=? WHERE id=? AND (COALESCE(label_claim, '')='' OR label_claim < ?)",
                       (mine, o["id"], stale)).rowcount != 1:
            flash("A label for this order is being bought right now. Give it a moment, then check below.")
            return redirect(url_for("admin_order", code=o["code"]))
        try:
            try:
                got = farm_shippo.buy(f.get("rate", ""), settings()["label_size"], fp.pretty_code(o["code"]))
            except farm_shippo.ShippoError as e:
                flash("No label was bought: {}".format(e))
                return redirect(url_for("admin_label", code=o["code"]))
            try:
                cost = float(f.get("cost") or 0)
            except ValueError:
                cost = None
            con.execute("""INSERT INTO shop_labels(order_id, shippo_id, carrier, service, cost, tracking, tracking_url, weight_oz, box,
                           status, test, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?, 'bought', ?, ?, ?)""",
                        (o["id"], got["id"], f.get("carrier", "")[:40], f.get("service", "")[:80], cost, got["tracking"], got["tracking_url"],
                         num_or_none(f.get("weight_oz")), f.get("box", "")[:40], 1 if farm_shippo.mode() == "test" else 0,
                         now().isoformat(), "staff"))
            pickup_too = any(l["fulfillment"] == "pickup" for l in o["lines"])
            if not o["fulfilled_at"] and not pickup_too:
                con.execute("UPDATE shop_orders SET fulfilled_at=?, fulfilled_by=? WHERE id=?", (now().isoformat(), "label", o["id"]))
            if f.get("tell") == "1":
                body = ["Hi {}! Your order {} is on its way.".format(o["name"].split(" ")[0], fp.pretty_code(o["code"])), ""]
                if got["tracking"]:
                    body.append("{} tracking number: {}".format(f.get("carrier") or "Tracking", got["tracking"]))
                if got["tracking_url"]:
                    body.append(got["tracking_url"])
                body += ["", "Your order: " + url_for("order", code=o["code"], _external=True)]
                send_email(o["email"], "Your {} order has shipped".format(settings()["shop_name"]), "\n".join(body))
        finally:
            con.execute("UPDATE shop_orders SET label_claim='' WHERE id=? AND label_claim=?", (o["id"], mine))
        flash("Label bought{}. Tap Print label.".format(" (practice label: no real postage)" if farm_shippo.mode() == "test" else ""))
        return redirect(url_for("admin_order", code=o["code"]))

    def label_or_404(code, lid):
        o = order_by_code(code)
        lab = row("SELECT * FROM shop_labels WHERE id=? AND order_id=?", lid, o["id"])
        if not lab:
            abort(404)
        return o, lab

    @app.get("/admin/orders/<code>/labels/<int:lid>/print")
    @admin_required
    def admin_label_print(code, lid):
        o, lab = label_or_404(code, lid)
        try:
            return redirect(farm_shippo.label_link(lab["shippo_id"]))  # Shippo's links expire, so ask for a fresh one each time
        except farm_shippo.ShippoError as e:
            flash("Couldn't open the label: {}".format(e))
            return redirect(url_for("admin_order", code=o["code"]))

    @app.post("/admin/orders/<code>/labels/<int:lid>/void")
    @admin_required
    def admin_label_void(code, lid):
        o, lab = label_or_404(code, lid)
        if lab["status"] != "bought":
            return redirect(url_for("admin_order", code=o["code"]))
        try:
            farm_shippo.void(lab["shippo_id"])
        except farm_shippo.ShippoError as e:
            flash("The label wasn't voided: {}".format(e))
            return redirect(url_for("admin_order", code=o["code"]))
        db().execute("UPDATE shop_labels SET status='voided', voided_at=? WHERE id=?", (now().isoformat(), lab["id"]))
        if o["fulfilled_by"] == "label" and not any(x["status"] == "bought" for x in order_labels(o)):
            db().execute("UPDATE shop_orders SET fulfilled_at=NULL, fulfilled_by=NULL WHERE id=?", (o["id"],))
        flash("Label voided. Shippo returns the postage once the carrier confirms it wasn't used (usually within a few weeks). "
              "Don't use that label.")
        return redirect(url_for("admin_order", code=o["code"]))

    @app.post("/admin/settings/shipping")
    @admin_required
    def admin_settings_shipping():
        """What buyers pay for shipping: the standard charge, the add-on per extra item, free shipping, and the profiles."""
        f = request.form

        def money_in(k, blank=None):
            v = f.get(k, "").strip().replace("$", "")
            if not v:
                return blank
            try:
                return max(0.0, round(float(v), 2))
            except ValueError:
                return "bad"
        vals = {"shipping_flat": money_in("shipping_flat", 0.0), "ship_extra_item": money_in("ship_extra_item", 0.0),
                "free_ship_over": money_in("free_ship_over", None)}
        if "bad" in vals.values():
            flash("Shipping amounts should be numbers, like 6 or 2.50.")
            return redirect(url_for("admin_settings") + "#shipping")
        for k, v in vals.items():
            save_setting(k, v)
        problems = []
        ids = [x for x in f.getlist("prof_id")]
        for i, pid in enumerate(ids + ["new"]):
            key = "new" if pid == "new" else pid
            name = f.get("prof_name_" + key, "").strip()[:80]
            if pid == "new" and not name:
                continue
            if pid != "new" and f.get("prof_remove_" + key):
                db().execute("UPDATE shop_ship_profiles SET archived=1 WHERE id=?", (int(pid),))
                db().execute("UPDATE shop_products SET ship_profile_id=NULL WHERE ship_profile_id=?", (int(pid),))
                continue
            price = money_in("prof_price_" + key, None)
            template = f.get("prof_template_" + key, "")
            template = template if template in farm_shippo.TEMPLATES else ""
            box = parse_box(f.get("prof_box_" + key, "")) if not template else None
            service = f.get("prof_service_" + key, "")
            service = service if service in farm_shippo.SERVICES else ""
            if not name or price in (None, "bad"):
                problems.append("{}: needs a name and a price".format(name or "A new profile"))
                continue
            if not template and f.get("prof_box_" + key, "").strip() and not box:
                problems.append("{}: write the box size like 10x8x4".format(name))
                continue
            box_text = "x".join("{:g}".format(d) for d in box) if box else ""
            if pid == "new":
                db().execute("INSERT INTO shop_ship_profiles(name, price, template, box, service, sort, created_at) VALUES (?,?,?,?,?,?,?)",
                             (name, price, template, box_text, service, len(ids), now().isoformat()))
            else:
                db().execute("UPDATE shop_ship_profiles SET name=?, price=?, template=?, box=?, service=? WHERE id=?",
                             (name, price, template, box_text, service, int(pid)))
        flash("Shipping saved." if not problems else "Saved, except: " + "; ".join(problems) + ".")
        return redirect(url_for("admin_settings") + "#shipping")

    @app.post("/admin/settings/labels")
    @admin_required
    def admin_settings_labels():
        f = request.form
        save_setting("ship_from", {k: f.get("from_" + k, "").strip()[:100] for k in ("name", "street1", "street2", "city", "state", "zip", "phone", "email")})
        if f.get("label_size") in LABEL_SIZES:
            save_setting("label_size", f["label_size"])
        flash("Shipping label settings saved.")
        return redirect(url_for("admin_settings") + "#labels")

    @app.get("/admin/orders.csv")
    @admin_required
    def admin_orders_csv():
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["Order", "Paid", "Name", "Email", "Phone", "Item", "Qty", "Each", "Buyer's answers", "Fulfillment", "Ship to",
                    "Order subtotal", "Shipping", "Tax", "Total", "Done"])
        for o in rows("SELECT * FROM shop_orders WHERE status='paid' AND test=0 ORDER BY id"):
            ship_to = ", ".join(x for x in [o["ship_name"], o["ship_line1"], o["ship_line2"], o["ship_city"], o["ship_state"], o["ship_zip"]] if x) \
                if o["ship_line1"] else ""
            for l in rows("SELECT * FROM shop_order_lines WHERE order_id=?", o["id"]):
                w.writerow([fp.pretty_code(o["code"]), (o["paid_at"] or "")[:16].replace("T", " "), o["name"], o["email"], o["phone"],
                            l["name"], l["qty"], l["unit_price"], " | ".join("{}: {}".format(a["q"], a["a"]) for a in answers_of(l)),
                            l["fulfillment"], ship_to, o["subtotal"], o["shipping"], o["tax"],
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
            for k in [k for k in ("shipping_flat", "tax_rate") if k in f]:
                v = f.get(k, "").strip().replace("$", "").replace("%", "")
                try:
                    save_setting(k, float(v) if v else (None if k == "tax_rate" else 0.0))
                except ValueError:
                    flash("{} should be a number.".format(k.replace("_", " ").capitalize()))
            flash("Shop settings saved.")
            return redirect(url_for("admin_settings"))
        return render_template("admin/settings.html", pd_tax=playdate_settings().get("tax_rate", 6.475),
                               own_tax=row("SELECT value FROM shop_settings WHERE key='tax_rate'"),
                               stripe_mode=stripe_mode, stripe_webhook_set=bool(webhook_secret), stripe_events=STRIPE_EVENTS,
                               webhook_url=url_for("stripe_webhook", _external=True),
                               label_mode=farm_shippo.mode(), label_sizes=LABEL_SIZES, profiles=ship_profiles(),
                               templates=farm_shippo.TEMPLATES, services=farm_shippo.SERVICES, farm_email=farm_email())

    @app.post("/admin/settings/email")
    @admin_required
    def admin_settings_email():
        f = request.form
        save_setting("shop_emails", f.get("shop_emails") == "1")
        try:
            save_setting("email_daily_limit", max(0, min(100, int(f.get("email_daily_limit", "").strip() or farm_mail.DEFAULT_DAILY_LIMIT))))
        except ValueError:
            flash("The daily limit should be a whole number.")
            return redirect(url_for("admin_settings") + "#email")
        flash("Shop emails are now sent for real." if f.get("shop_emails") == "1" else "Shop emails are switched off: they only show on the Emails page.")
        return redirect(url_for("admin_settings") + "#email")

    def shop_emails(statuses=None):
        sql = "SELECT * FROM outbox WHERE booking_id IS NULL AND channel='email'"
        if statuses:
            sql += " AND email_status IN ({})".format(",".join("?" * len(statuses)))
        return rows(sql + " ORDER BY id DESC LIMIT 200", *(statuses or ()))

    @app.get("/admin/emails")
    @admin_required
    def admin_emails():
        return render_template("admin/emails.html", emails=shop_emails())

    @app.post("/admin/emails/<int:oid>/send-again")
    @admin_required
    def admin_email_send_again(oid):
        if not row("SELECT id FROM outbox WHERE id=? AND booking_id IS NULL", oid):
            abort(404)
        result = farm_mail.deliver(db(), oid, "shop", by_hand=True)
        flash({"sent": "Sent.", "waiting": "Not sent yet: it's waiting and will go out by itself (see the note on it).",
               "console": "No email service is connected here, so it only shows on this page."}.get(result, "It still couldn't be sent. See the note on it."))
        return redirect(url_for("admin_emails"))

    @app.before_request
    def send_waiting_emails():
        farm_mail.kick()  # at most every 5 minutes, in the background

    app.jinja_env.globals.update(email_state=farm_mail.describe, email_service=farm_mail.service_status,
                                 emails_sent_today=lambda: farm_mail.sent_today(db()),
                                 emails_needing_attention=lambda: len(shop_emails(["waiting", "sending", "failed"])))

    @app.errorhandler(404)
    def not_found(e):
        return render_template("message.html", title="We couldn't find that", body="That page isn't here. It may have been taken down.",
                               link=url_for("home"), link_text="Go to the shop"), 404

    return app
