"""Mollie's Farmhouse Bakery: preorder shop + Mollie's admin, as its own Flask app.

It runs beside the playdate app (see wsgi.py) but shares nothing with it: its own data file,
its own photos folder, its own login and its own Stripe keys. With no Stripe key it runs in
demo mode (card orders are marked paid without a payment).

Settings (environment variables):
  BAKERY_PASSWORD               old shared admin password. Only used until someone has a personal login with the
                                Bakery box ticked (made on the farm's Logins page); after that it's ignored.
  BAKERY_SECRET_KEY             long random string that signs her login cookie
  BAKERY_STRIPE_SECRET_KEY      Mollie's Stripe secret key (sk_test_... or sk_live_...)
  BAKERY_STRIPE_WEBHOOK_SECRET  from her Stripe webhook for <site>/api/stripe/webhook
  DATABASE_URL                  on Render: the Postgres server (data goes in the farm_apps database)
  FARM_DATA_DIR                 on the Mac: folder for the bakery's data file and photos
  MAIL_PROVIDER, RESEND_API_KEY, BAKERY_MAIL_FROM
                                customer emails (console = print instead of send, the default)
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
                                Cloudflare R2 bucket for Mollie's photos (private; this app serves them)
"""
import base64
import contextlib
import datetime as dt
import json
import os
import re
import secrets
import sys

from flask import Flask, Response, abort, jsonify, redirect, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix

from . import emails, store

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "playdates"))
import dbcompat  # noqa: E402  (SQLite file on the Mac, Postgres on Render)
import farm_auth as fa  # noqa: E402  (personal logins: Mollie gets her own, with only the Bakery box ticked)
import farm_build  # noqa: E402  (build number, shown at the bottom of Mollie's admin only)
import mailer  # noqa: E402  (same mail code as Pasture Keeper)


def create_app():
    data_dir = os.environ.get("FARM_DATA_DIR") or os.path.join(os.path.dirname(HERE), "data")
    uploads = os.path.join(data_dir, "bakery_uploads")
    os.makedirs(uploads, exist_ok=True)
    db_path = os.path.join(data_dir, "bakery.db")
    password = os.environ.get("BAKERY_PASSWORD", "")
    stripe_key = os.environ.get("BAKERY_STRIPE_SECRET_KEY", "")
    webhook_secret = os.environ.get("BAKERY_STRIPE_WEBHOOK_SECRET", "")
    demo = not stripe_key
    mail = mailer.build_mailer("BAKERY_MAIL_FROM")  # refuses to start if half set up
    r2 = None
    if os.environ.get("R2_BUCKET"):
        import boto3  # only needed when photos go to Cloudflare R2
        endpoint = os.environ.get("R2_ENDPOINT") or "https://{}.r2.cloudflarestorage.com".format(os.environ["R2_ACCOUNT_ID"])
        r2 = boto3.client("s3", endpoint_url=endpoint, region_name="auto",
                          aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                          aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])
    r2_bucket = os.environ.get("R2_BUCKET", "")
    stripe = None
    if stripe_key:
        import stripe as stripe_lib  # only needed once real payments are on
        stripe = stripe_lib

    app = Flask(__name__, static_folder="static", static_url_path="")
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # so links say https:// behind Render
    app.secret_key = os.environ.get("BAKERY_SECRET_KEY") or secrets.token_hex(32)
    app.config.update(
        SESSION_COOKIE_NAME="bakery_session",  # never mixes with the playdate admin login
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=dt.timedelta(days=30),
        MAX_CONTENT_LENGTH=12 * 1024 * 1024,  # photos arrive already shrunk by the phone
    )

    # ---------------------------------------------------------------- storage
    # The whole shop is one JSON document in one table row. Each change runs in a
    # transaction, so two people checking out at once can't both get the last pie.
    def connect():
        return dbcompat.connect(db_path, timeout=15, isolation_level=None)

    con = connect()
    con.executescript("CREATE TABLE IF NOT EXISTS bakery_store (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL);")
    con.close()

    def load(con):
        row = con.execute("SELECT data FROM bakery_store WHERE id = 1").fetchone()
        return store.normalize(json.loads(row[0]) if row else store.default_state())

    @contextlib.contextmanager
    def changing():
        con = connect()
        con.execute("BEGIN IMMEDIATE")
        try:
            state = load(con)
            yield state
            con.execute("INSERT OR REPLACE INTO bakery_store(id, data) VALUES (1, ?)", (json.dumps(state),))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def reading():
        con = connect()
        try:
            return load(con)
        finally:
            con.close()

    with changing():
        pass  # creates the file with Mollie's starting menu on first run
    fa.ensure_schema()

    # ---------------------------------------------------------------- helpers
    def fail(e):
        if isinstance(e, store.UserError):
            return jsonify(error=str(e)), 400
        app.logger.exception("bakery error")
        return jsonify(error="Something went wrong. Please try again."), 500

    def body():
        return request.get_json(silent=True) or {}

    def is_admin():
        if fa.accounts_for("bakery"):  # someone has a personal login for the bakery: only personal logins work
            return fa.can(fa.current_user(request), "bakery")
        if not password:
            return demo  # no password set: only allowed in demo mode
        return session.get("bakery_admin") is True

    def admin_only():
        if not is_admin():
            abort(Response(json.dumps({"error": "Please sign in."}), 401, mimetype="application/json"))

    def site():
        return request.url_root  # e.g. https://bakery.gooseberrybridge.com/ or http://127.0.0.1:5050/bakery/

    def save_photo(data_url):
        m = re.match(r"^data:image/(jpeg|png|webp);base64,(.+)$", data_url or "", re.S)
        if not m:
            raise store.UserError("That photo format isn’t supported.")
        name = store.uid(10) + "." + ("jpg" if m.group(1) == "jpeg" else m.group(1))
        data = base64.b64decode(m.group(2))
        if r2:
            r2.put_object(Bucket=r2_bucket, Key="bakery/" + name, Body=data, ContentType="image/" + m.group(1))
        else:
            with open(os.path.join(uploads, name), "wb") as f:
                f.write(data)
        return "uploads/" + name

    def notify(order_id, kind):
        """Send the "reserved" or "paid" email once per order, whichever path got there first."""
        with changing() as state:
            o = store.find_order(state, order_id)
            if not o or kind in o.setdefault("mailed", []):
                return
            o["mailed"].append(kind)
            order, settings = json.loads(json.dumps(o)), dict(state["settings"])
        subject, text = emails.compose(kind, order, settings, site() + "?order=" + order_id)
        if not mail.send(order["customer"]["email"], subject, text):
            with changing() as state:
                o = store.find_order(state, order_id)
                if o:
                    o.setdefault("mailProblems", []).append(kind)

    # ---------------------------------------------------------------- pages
    @app.get("/")
    def index():
        resp = app.send_static_file("index.html")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/admin")
    def admin_redirect():
        return redirect(site() + "#admin")

    @app.get("/uploads/<path:name>")
    def photo(name):
        if not r2:
            return send_from_directory(uploads, name, max_age=30 * 24 * 3600)
        if not re.match(r"^[a-z0-9]+\.(jpg|png|webp)$", name):
            abort(404)
        try:
            obj = r2.get_object(Bucket=r2_bucket, Key="bakery/" + name)
        except Exception:  # noqa: BLE001  (missing photo)
            abort(404)
        return Response(obj["Body"].read(), mimetype=obj.get("ContentType") or "image/jpeg",
                        headers={"Cache-Control": "public, max-age=2592000"})

    # ---------------------------------------------------------------- customers
    @app.get("/api/shop")
    def shop():
        out = store.public_shop(reading(), store.now_ms())
        out["demo"] = demo
        return jsonify(out)

    @app.post("/api/checkout")
    def checkout():
        try:
            with changing() as state:
                order = store.build_order(state, body(), store.now_ms())
                state["orders"].append(order)
        except Exception as e:  # noqa: BLE001
            return fail(e)

        # Venmo / Cash App / PayPal: reserved now; the confirmation page shows how to pay.
        if order["payMethod"] == "app":
            notify(order["id"], "reserved")
            return jsonify(orderId=order["id"], url=site() + "?order=" + order["id"])

        if demo:
            with changing() as state:
                store.mark_paid(state, order["id"], None, store.now_ms())
            notify(order["id"], "paid")
            return jsonify(orderId=order["id"], url=site() + "?order=" + order["id"])

        try:
            state = reading()
            items = {i["id"]: i for i in state["items"]}
            hold = max(30, int(state["settings"].get("holdMinutes") or 30))
            shop_name = state["settings"]["shopName"]
            line_items = []
            for l in order["lines"]:
                product = {"name": l["name"]}
                img = (items.get(l["itemId"]) or {}).get("image") or ""
                if img and site().startswith("https://"):
                    product["images"] = [site() + img]
                line_items.append({"quantity": l["qty"], "price_data": {"currency": "usd", "unit_amount": l["price"], "product_data": product}})
            meta = {"orderId": order["id"], "orderNumber": str(order["number"]), "pickup": order["pickupLabel"]}
            checkout_session = stripe.checkout.Session.create(
                api_key=stripe_key,
                mode="payment",
                customer_email=order["customer"]["email"],
                line_items=line_items,
                metadata=meta,
                payment_intent_data={"description": "{} #{} — {}".format(shop_name, order["number"], order["customer"]["name"]),
                                     "metadata": {"orderId": order["id"], "orderNumber": str(order["number"])}},
                expires_at=int(store.now_ms() / 1000) + hold * 60,
                success_url=site() + "?order={}&session_id={{CHECKOUT_SESSION_ID}}".format(order["id"]),
                cancel_url=site() + "?cancelled=" + order["id"],
            )
            with changing() as state:
                o = store.find_order(state, order["id"])
                o["stripeSessionId"] = checkout_session["id"]
            return jsonify(orderId=order["id"], url=checkout_session["url"])
        except Exception:  # noqa: BLE001
            app.logger.exception("Stripe checkout failed")
            with changing() as state:
                o = store.find_order(state, order["id"])
                if o and o["status"] == "pending":
                    o["status"] = "failed"  # frees the items right away
            return jsonify(error="We couldn't start checkout. Please try again in a minute."), 502

    @app.get("/api/orders/<order_id>")
    def order_status(order_id):
        state = reading()
        o = store.find_order(state, order_id)
        if not o:
            return jsonify(error="Order not found."), 404
        # Double-check Stripe in case its "paid" message is slow or missing.
        if stripe and o["status"] == "pending" and o.get("stripeSessionId"):
            try:
                cs = stripe.checkout.Session.retrieve(o["stripeSessionId"], api_key=stripe_key)
                if cs["payment_status"] == "paid":
                    with changing() as state:
                        o = store.mark_paid(state, order_id, {"amountPaid": cs["amount_total"]}, store.now_ms())
                    notify(order_id, "paid")
            except Exception:  # noqa: BLE001
                app.logger.exception("Stripe lookup failed")
        return jsonify(store.public_order(o, state["settings"]))

    @app.post("/api/orders/<order_id>/cancel")
    def order_cancel(order_id):
        """Customer backed out of Stripe's page: put the items back on sale right away."""
        o = store.find_order(reading(), order_id)
        if o and o["status"] == "pending":
            if stripe and o.get("stripeSessionId"):
                try:
                    stripe.checkout.Session.expire(o["stripeSessionId"], api_key=stripe_key)
                except Exception:  # noqa: BLE001
                    return jsonify(ok=False)  # already paid or expired; Stripe's message will settle it
            with changing() as state:
                o = store.find_order(state, order_id)
                if o and o["status"] == "pending":
                    o["status"] = "cancelled"
        return jsonify(ok=True)

    @app.post("/api/stripe/webhook")
    def stripe_webhook():
        if not stripe or not webhook_secret:
            return "", 400
        try:
            event = stripe.Webhook.construct_event(request.get_data(), request.headers.get("Stripe-Signature", ""), webhook_secret)
        except Exception as e:  # noqa: BLE001
            return "Webhook error: {}".format(e), 400
        cs = event["data"]["object"]
        order_id = (cs.get("metadata") or {}).get("orderId")
        paid = False
        with changing() as state:
            o = store.find_order(state, order_id)
            if o:
                if event["type"] == "checkout.session.completed" and cs.get("payment_status") == "paid":
                    store.mark_paid(state, order_id, {"amountPaid": cs.get("amount_total")}, store.now_ms())
                    paid = True
                elif event["type"] == "checkout.session.expired" and o["status"] == "pending":
                    o["status"] = "expired"
        if paid:
            notify(order_id, "paid")
        return jsonify(received=True)

    # ---------------------------------------------------------------- Mollie
    @app.get("/api/admin/login-mode")
    def login_mode():
        return jsonify(accounts=fa.accounts_for("bakery"))

    @app.post("/api/admin/login")
    def login():
        if fa.accounts_for("bakery"):
            u, err = fa.authenticate(body().get("email", ""), str(body().get("password", "")), request.remote_addr or "")
            if not u:
                return jsonify(error=err), 401
            if not fa.can(u, "bakery"):
                return jsonify(error="That login doesn't have the bakery admin turned on."), 403
            resp = jsonify(ok=True, mustChange=bool(u["must_change"]))
            return fa.set_cookie(resp, request, fa.start_login(u))
        if not password:
            return (jsonify(ok=True), 200) if demo else (jsonify(error="Set BAKERY_PASSWORD on the server first."), 403)
        if secrets.compare_digest(str(body().get("password", "")), password):
            session.permanent = True
            session["bakery_admin"] = True
            return jsonify(ok=True)
        return jsonify(error="That password didn't work."), 401

    @app.post("/api/admin/logout")
    def logout():
        session.pop("bakery_admin", None)
        fa.end_login(request.cookies.get(fa.COOKIE))
        return fa.clear_cookie(jsonify(ok=True), request)

    # Forgot / change password pages, in Mollie's colors and name (nothing farm-branded).
    fa.add_pages(app, brand="Mollie's Farmhouse Bakery", color="#7a2e4a", farm=False, default_next="/#admin",
                 mailer_from="BAKERY_MAIL_FROM")

    @app.get("/api/admin/state")
    def admin_state():
        admin_only()
        state = reading()
        now = store.now_ms()
        state["demo"] = demo
        state["build"] = farm_build.build()
        state["stock"] = {i["id"]: store.remaining(state, i, now) for i in state["items"]}
        return jsonify(state)

    @app.post("/api/admin/action")
    def admin_action():
        admin_only()
        try:
            a = body()
            if a.get("action") == "saveItem" and isinstance(a.get("item"), dict) and str(a["item"].get("image", "")).startswith("data:"):
                a["item"]["image"] = save_photo(a["item"]["image"])
            with changing() as state:
                result = store.admin_action(state, a, store.now_ms())
            if a.get("action") == "markPaid":
                notify(a.get("id"), "paid")
            return jsonify(result)
        except Exception as e:  # noqa: BLE001
            return fail(e)

    @app.get("/api/admin/orders.csv")
    def orders_csv():
        admin_only()
        state = reading()
        season = request.args.get("season") or state["settings"]["season"]
        safe = re.sub(r"[^\w -]", "", season) or "orders"
        return Response(store.orders_csv(state, season), mimetype="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="{} orders.csv"'.format(safe)})

    @app.get("/api/health")
    def health():
        return jsonify(ok=True, demo=demo, email=mail.kind)

    app.mailer = mail  # lets the tests see what would have been sent

    return app
