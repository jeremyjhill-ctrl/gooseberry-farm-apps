"""Stripe Checkout check for the farm shop (and the bakery's Stripe messages).

Stripe itself is replaced by a stand-in that behaves like Stripe's Python library: its objects can be read with
obj["key"] but NOT obj.get("key") (that difference broke the bakery's webhook before Nov 21, 2026), its webhook
messages are signed the way Stripe signs them, and payment pages can be opened, paid, expired and backed out of.

    python tests/stripe_test.py                    # SQLite, like the Mac (no Stripe library needed)
    DATABASE_URL=... python tests/stripe_test.py   # Postgres, like the live server
    STRIPE_REAL=1 python tests/stripe_test.py      # also uses the real Stripe library to check signatures
"""
import copy
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
work = tempfile.mkdtemp(prefix="stripe-test-")
SECRET = "whsec_farm_test_secret"
BAKERY_SECRET = "whsec_bakery_test_secret"
os.environ.update(PLAYDATE_DB=os.path.join(work, "p.db"), FARM_DATA_DIR=work, FARM_TEST_MODE="1",
                  MAIL_PROVIDER="memory", PLAYDATE_EMAILS="on", ADMIN_PASSWORD="playdate",
                  FARM_STRIPE_SECRET_KEY="sk_test_farm_stand_in", FARM_STRIPE_WEBHOOK_SECRET=SECRET,
                  BAKERY_STRIPE_SECRET_KEY="sk_test_bakery_stand_in", BAKERY_STRIPE_WEBHOOK_SECRET=BAKERY_SECRET,
                  BAKERY_PASSWORD="bakery")
if os.environ.get("DATABASE_URL"):
    os.environ["FARM_DB_NAME"] = "stripetest_" + os.path.basename(work).replace("-", "_").lower()
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "playdates"))


# ---------------------------------------------------------------- the Stripe stand-in
class StandInObject:
    """Like Stripe 17's objects: obj["x"] works, obj.get("x") doesn't, obj.to_dict() gives plain dicts."""

    def __init__(self, d):
        self._d = d

    def __getitem__(self, k):
        v = self._d[k]
        return StandInObject(v) if isinstance(v, dict) else v

    def get(self, *a):
        raise AttributeError("'get' is a dict method, but a Session is not a dict. Use .to_dict() to convert it.")

    def to_dict(self):
        return copy.deepcopy(self._d)


class StripeError(Exception):
    pass


SESSIONS, CALLS = {}, {"create": [], "retrieve": 0, "expire": []}
TROUBLE = {"create": False, "retrieve": False, "slow_create": 0.0}
REAL = None
if os.environ.get("STRIPE_REAL"):
    try:
        import stripe as REAL  # noqa: N812
    except ImportError:
        REAL = None


def wrap(d):
    if REAL:
        return REAL.checkout.Session.construct_from(copy.deepcopy(d), "sk_test_farm_stand_in")
    return StandInObject(copy.deepcopy(d))


def s_create(**kw):
    if TROUBLE["slow_create"]:
        time.sleep(TROUBLE["slow_create"])
    if TROUBLE["create"]:
        raise StripeError("Stripe is down")
    CALLS["create"].append(kw)
    sid = "cs_test_{}".format(len(CALLS["create"]))
    total = sum(x["quantity"] * x["price_data"]["unit_amount"] for x in kw["line_items"])
    SESSIONS[sid] = {"id": sid, "object": "checkout.session", "status": "open", "payment_status": "unpaid",
                     "url": "https://checkout.stripe.test/" + sid, "amount_total": total, "payment_intent": None,
                     "metadata": kw.get("metadata", {}), "client_reference_id": kw.get("client_reference_id")}
    return wrap(SESSIONS[sid])


def s_retrieve(sid, api_key=None, **kw):
    CALLS["retrieve"] += 1
    if TROUBLE["retrieve"]:
        raise StripeError("Stripe is down")
    return wrap(SESSIONS[sid])


def s_expire(sid, api_key=None, **kw):
    CALLS["expire"].append(sid)
    if SESSIONS[sid]["status"] != "open":
        raise StripeError("Only Checkout Sessions with a status of open can be expired.")
    SESSIONS[sid].update(status="expired")
    return wrap(SESSIONS[sid])


def sign(payload, secret, t=None):
    t = int(t or time.time())
    v1 = hmac.new(secret.encode(), "{}.{}".format(t, payload).encode(), hashlib.sha256).hexdigest()
    return "t={},v1={}".format(t, v1)


def construct_event(payload, header, secret):
    if isinstance(payload, bytes):
        payload = payload.decode()
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    want = hmac.new(secret.encode(), "{}.{}".format(parts.get("t", ""), payload).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, parts.get("v1", "")) or abs(time.time() - int(parts.get("t", 0))) > 300:
        raise StripeError("No signatures found matching the expected signature for payload")
    return StandInObject(json.loads(payload))


if REAL:
    REAL.checkout.Session.create = staticmethod(s_create)
    REAL.checkout.Session.retrieve = staticmethod(s_retrieve)
    REAL.checkout.Session.expire = staticmethod(s_expire)
else:
    fake = types.ModuleType("stripe")
    fake.checkout = types.SimpleNamespace(Session=types.SimpleNamespace(create=s_create, retrieve=s_retrieve, expire=s_expire))
    fake.Webhook = types.SimpleNamespace(construct_event=construct_event)
    sys.modules["stripe"] = fake


def pay_at_stripe(sid, amount=None):
    s = SESSIONS[sid]
    s.update(status="complete", payment_status="paid", payment_intent="pi_" + sid[3:])
    if amount is not None:
        s["amount_total"] = amount


import wsgi  # noqa: E402
import dbcompat  # noqa: E402
import farm_mail  # noqa: E402
import farm_passes as fp  # noqa: E402
from werkzeug.test import Client  # noqa: E402

results, failures = [], []


def check(name, ok, detail=""):
    results.append(name)
    if not ok:
        failures.append("{}: {}".format(name, str(detail)[:600]))


def q(sql, *args):
    c = dbcompat.connect(os.environ["PLAYDATE_DB"], isolation_level=None)
    c.row_factory = sqlite3.Row
    out = [dict(r) for r in c.execute(sql, args)]
    c.close()
    return out


def body(r):
    return r.get_data(as_text=True)


def loc(r):
    return r.headers.get("Location", "")


def webhook(kind, sid, secret=SECRET, path="/shop/stripe/webhook", header=None, data=None):
    payload = json.dumps({"id": "evt_" + sid, "object": "event", "type": kind,
                          "data": {"object": data if data is not None else SESSIONS[sid]}})
    return Client(wsgi.application).post(path, data=payload, content_type="application/json",
                                         headers={"Stripe-Signature": header if header is not None else sign(payload, secret)})


def order(code):
    return q("SELECT * FROM shop_orders WHERE code=?", code)[0]


def passes(code):
    return q("SELECT p.* FROM shop_passes p JOIN shop_orders o ON o.id = p.order_id WHERE o.code=?", code)


def receipts(email):
    return q("SELECT * FROM outbox WHERE channel='email' AND recipient=?", email)


M = farm_mail.get_mailer()[0]
staff = Client(wsgi.application)
staff.post("/shop/admin/login", data={"password": "playdate"})
staff.post("/shop/admin/visibility", data={"which": "shop", "hidden": "0"})
staff.post("/shop/admin/settings/email", data={"shop_emails": "1", "email_daily_limit": "40"})
PLAY = q("SELECT id FROM shop_credit_types ORDER BY id")[0]["id"]
staff.post("/shop/admin/products/new?kind=pass", data={"name": "Spring Family Pass", "price": "120", "cq_0": "2", "ct_0": str(PLAY),
                                                         "taxable": "on", "stock_limit": "50"})
staff.post("/shop/admin/products/new?kind=item", data={"name": "Signed book", "price": "30", "can_ship": "1", "taxable": "on"})
PASS = q("SELECT id FROM shop_products WHERE name='Spring Family Pass'")[0]["id"]
BOOK = q("SELECT id FROM shop_products WHERE name='Signed book'")[0]["id"]


def start(email, items=((PASS, 1),), ship=False, who=None):
    """A buyer fills the cart and the checkout form; returns (their browser, order code)."""
    g = who or Client(wsgi.application)
    for pid, n in items:
        g.post("/shop/cart/add", data={"product_id": pid, "qty": n})
    data = {"name": "Buyer " + email.split("@")[0], "email": email, "agree": "on"}
    if ship:
        data.update(ship_line1="1 Main St", ship_city="Rogersville", ship_state="MO", ship_zip="65742")
    r = g.post("/shop/checkout", data=data)
    m = re.search(r"/shop/pay/([A-Z0-9]+)", loc(r))
    if not m:
        raise AssertionError("checkout didn't make an order: " + body(r)[:800])
    return g, m.group(1)


# ---------------------------------------------------------------- settings show what's set up
s = body(staff.get("/shop/admin/settings"))
check("Settings shows Stripe test mode", "Stripe test mode" in s and "4242 4242 4242 4242" in s, s[s.find("Card payments"):][:500])
check("Settings shows the webhook address and events", "/shop/stripe/webhook" in s and "checkout.session.completed" in s
      and "checkout.session.expired" in s and "set up." in s)

# ---------------------------------------------------------------- 1. pay, come back, then Stripe's message arrives
g1, c1 = start("ann@example.com", items=((PASS, 2), (BOOK, 1)), ship=True)
r = g1.get("/shop/pay/" + c1)
o1 = order(c1)
call = CALLS["create"][-1] if CALLS["create"] else {}
check("pay sends the buyer to Stripe's page", r.status_code == 302 and loc(r) == "https://checkout.stripe.test/cs_test_1", (r.status_code, loc(r)))
check("Stripe is asked for exactly the order total", SESSIONS["cs_test_1"]["amount_total"] == round(o1["total"] * 100) and o1["total"] > 0,
      (SESSIONS["cs_test_1"]["amount_total"], o1["total"]))
names = [x["price_data"]["product_data"]["name"] for x in call.get("line_items", [])]
check("Stripe's page lists the items, shipping and tax", names == ["Spring Family Pass", "Signed book", "Shipping", "Sales tax"], names)
check("Stripe call is cards only, in dollars, with the buyer's email", call.get("payment_method_types") == ["card"]
      and all(x["price_data"]["currency"] == "usd" for x in call["line_items"]) and call.get("customer_email") == "ann@example.com")
check("Stripe knows which order it is", call.get("metadata", {}).get("order") == c1 and call.get("client_reference_id") == c1
      and call["payment_intent_data"]["metadata"]["order"] == c1)
check("Stripe sends them back to the order page and to cancel", call.get("success_url", "").endswith("/shop/order/{}?session_id={{CHECKOUT_SESSION_ID}}".format(c1))
      and call.get("cancel_url", "").endswith("/shop/pay/{}/cancelled".format(c1)), (call.get("success_url"), call.get("cancel_url")))
mins = (call.get("expires_at", 0) - time.time()) / 60
check("Stripe's page stays open more than Stripe's 30-minute minimum", 33 < mins <= 35.1, mins)
held = (dt.datetime.fromisoformat(o1["hold_expires"]) - dt.datetime.now(dt.timezone.utc)).total_seconds() / 60
check("items stay held past Stripe's page closing", 43 < held <= 45.1, held)

r = g1.get("/shop/pay/" + c1)
check("going back to pay reopens the same page, not a second one", loc(r) == "https://checkout.stripe.test/cs_test_1" and len(CALLS["create"]) == 1)
r = g1.get("/shop/order/" + c1)
check("the order page before paying sends them back to pay", r.status_code == 302 and "/shop/pay/" in loc(r))

pay_at_stripe("cs_test_1")
r = g1.get("/shop/order/{}?session_id=cs_test_1".format(c1))
check("back from Stripe: thank-you page", r.status_code == 200 and "Thank you" in body(r), body(r)[:300])
o1 = order(c1)
check("order is paid by Stripe", o1["status"] == "paid" and o1["method"] == "stripe" and o1["stripe_payment"] == "pi_test_1"
      and o1["amount_paid"] == round(o1["total"] * 100) and not o1["pay_note"], o1)
check("the passes are made", len(passes(c1)) == 2)
check("one receipt is emailed", len(receipts("ann@example.com")) == 1)
check("their cart is emptied", "Your cart is empty" in body(g1.get("/shop/cart")) or "Signed book" not in body(g1.get("/shop/cart")))
r = webhook("checkout.session.completed", "cs_test_1")
check("Stripe's message afterwards is accepted", r.status_code == 200, body(r))
check("...and makes no extra passes or emails", len(passes(c1)) == 2 and len(receipts("ann@example.com")) == 1)

a = body(staff.get("/shop/admin/orders/" + c1))
check("admin order shows it was paid through Stripe, with a link", "card through Stripe" in a
      and "https://dashboard.stripe.com/test/payments/pi_test_1" in a)

# ---------------------------------------------------------------- 2. buyer closes the tab: Stripe's message does it all
g2, c2 = start("bo@example.com")
g2.get("/shop/pay/" + c2)
sid2 = order(c2)["stripe_session"]
pay_at_stripe(sid2)
r = webhook("checkout.session.completed", sid2)
check("Stripe's message alone completes the order", r.status_code == 200 and order(c2)["status"] == "paid" and len(passes(c2)) == 1
      and len(receipts("bo@example.com")) == 1, (body(r), order(c2)["status"]))
r = g2.get("/shop/order/{}?session_id={}".format(c2, sid2))
check("coming back later still shows the order, no duplicates", r.status_code == 200 and len(passes(c2)) == 1 and len(receipts("bo@example.com")) == 1)
webhook("checkout.session.completed", sid2)
check("a repeated message from Stripe changes nothing", len(passes(c2)) == 1 and len(receipts("bo@example.com")) == 1)

# ---------------------------------------------------------------- 3. forged or broken messages
g3, c3 = start("cy@example.com")
g3.get("/shop/pay/" + c3)
sid3 = order(c3)["stripe_session"]
fake_paid = dict(SESSIONS[sid3], status="complete", payment_status="paid", payment_intent="pi_forged")
r = webhook("checkout.session.completed", sid3, secret="whsec_wrong", data=fake_paid)
check("a message signed with the wrong secret is refused", r.status_code == 400 and order(c3)["status"] == "pending", body(r))
r = webhook("checkout.session.completed", sid3, header="", data=fake_paid)
check("a message with no signature is refused", r.status_code == 400 and order(c3)["status"] == "pending")
payload = json.dumps({"type": "checkout.session.completed", "data": {"object": fake_paid}})
r = Client(wsgi.application).post("/shop/stripe/webhook", data=payload, content_type="application/json",
                                  headers={"Stripe-Signature": sign(payload, SECRET, t=time.time() - 3600)})
check("an old replayed message is refused", r.status_code == 400 and order(c3)["status"] == "pending")
r = webhook("checkout.session.completed", sid3, data={"id": "cs_other", "object": "checkout.session", "payment_status": "paid",
                                                     "metadata": {"order": "NOTOURS1"}})
check("a message about someone else's payment is ignored politely", r.status_code == 200 and "not a farm shop order" in body(r))

# ---------------------------------------------------------------- 4. buyer backs out of Stripe's page
left_before = q("SELECT COUNT(*) AS n FROM shop_order_lines l JOIN shop_orders o ON o.id=l.order_id WHERE o.status='pending'")[0]["n"]
r = g3.get("/shop/pay/{}/cancelled".format(c3))
check("backing out closes Stripe's page and cancels the order", sid3 in CALLS["expire"] and order(c3)["status"] == "cancelled")
cart3 = body(g3.get("/shop/cart"))
check("...and goes back to their cart, still full", "/shop/cart" in loc(r) and "Spring Family Pass" in cart3)
check("...saying nothing was charged", "No payment was made" in cart3)

g4, c4 = start("di@example.com")
g4.get("/shop/pay/" + c4)
sid4 = order(c4)["stripe_session"]
pay_at_stripe(sid4)
r = g4.get("/shop/pay/{}/cancelled".format(c4))
check("backing out after paying (two tabs) completes the order instead", order(c4)["status"] == "paid" and "/shop/order/" in loc(r)
      and len(passes(c4)) == 1, (order(c4)["status"], loc(r)))

# ---------------------------------------------------------------- 5. paid at the last moment, after the hold ran out
g5, c5 = start("ed@example.com")
g5.get("/shop/pay/" + c5)
sid5 = order(c5)["stripe_session"]
past = (dt.datetime.now(fp.TZ).replace(microsecond=0) - dt.timedelta(minutes=1)).isoformat()  # the app's own clock
c = dbcompat.connect(os.environ["PLAYDATE_DB"], isolation_level=None)
c.execute("UPDATE shop_orders SET hold_expires=? WHERE code=?", (past, c5))
c.close()
Client(wsgi.application).get("/shop/cart")  # any visit tidies up old holds
check("(setup) the hold ran out", order(c5)["status"] == "expired", order(c5)["status"])
pay_at_stripe(sid5)
webhook("checkout.session.completed", sid5)
o5 = order(c5)
check("money taken after the hold still completes the order", o5["status"] == "paid" and len(passes(c5)) == 1, o5["status"])
check("...with a note to check stock", "check that the items are still in stock" in (o5["pay_note"] or ""), o5["pay_note"])
check("...shown on the admin order page", "check that the items are still in stock" in body(staff.get("/shop/admin/orders/" + c5)))

g6, c6 = start("flo@example.com")
g6.get("/shop/pay/" + c6)
sid6 = order(c6)["stripe_session"]
pay_at_stripe(sid6, amount=999)
webhook("checkout.session.completed", sid6)
check("a different amount from Stripe is flagged", "Stripe charged $9.99" in (order(c6)["pay_note"] or ""), order(c6)["pay_note"])

g7, c7 = start("gus@example.com")
g7.get("/shop/pay/" + c7)
sid7 = order(c7)["stripe_session"]
SESSIONS[sid7]["status"] = "expired"
webhook("checkout.session.expired", sid7)
check("Stripe saying the page expired lets the items go", order(c7)["status"] == "expired")

# ---------------------------------------------------------------- 6. Stripe trouble
g8, c8 = start("hal@example.com")
TROUBLE["create"] = True
r = g8.get("/shop/pay/" + c8)
TROUBLE["create"] = False
check("if Stripe is down, the buyer is told and nothing is charged", "open the payment page" in body(r)
      and order(c8)["stripe_session"] == "", (body(r)[:300], order(c8)["stripe_session"]))
r = g8.get("/shop/pay/" + c8)
check("trying again works", loc(r).startswith("https://checkout.stripe.test/") and order(c8)["stripe_session"].startswith("cs_"))
made = len(CALLS["create"])
TROUBLE["retrieve"] = True
r = g8.get("/shop/pay/" + c8)
TROUBLE["retrieve"] = False
check("if Stripe can't be asked about an open page, no second page is opened", len(CALLS["create"]) == made
      and "open the payment page" in body(r))
SESSIONS[order(c8)["stripe_session"]]["status"] = "expired"
r = g8.get("/shop/pay/" + c8)
check("a page that expired at Stripe is replaced by a fresh one", len(CALLS["create"]) == made + 1
      and loc(r) == SESSIONS[order(c8)["stripe_session"]]["url"])
old_sid = "cs_test_{}".format(made)
webhook("checkout.session.expired", old_sid)
check("an old page's expiry message doesn't cancel the order's new page", order(c8)["status"] == "pending"
      and order(c8)["stripe_session"] != old_sid, (old_sid, order(c8)["stripe_session"]))

# ---------------------------------------------------------------- 7. a double-tap on Continue to payment
g9, c9 = start("ivy@example.com")
made = len(CALLS["create"])
TROUBLE["slow_create"] = 0.8
got = []


def tap():
    got.append(loc(Client(wsgi.application).get("/shop/pay/" + c9, follow_redirects=False)))


threads = [threading.Thread(target=tap) for _ in range(2)]
for t in threads:
    t.start()
    time.sleep(0.1)
for t in threads:
    t.join()
TROUBLE["slow_create"] = 0.0
follow = [x if "checkout.stripe.test" in x else loc(Client(wsgi.application).get(x)) for x in got]
check("two taps open only one Stripe page", len(CALLS["create"]) == made + 1, (len(CALLS["create"]) - made, got))
check("...and both land on it", len(set(follow)) == 1 and follow[0].startswith("https://checkout.stripe.test/"), follow)

# ---------------------------------------------------------------- 8. test-buying while the shop is hidden
staff.post("/shop/admin/visibility", data={"which": "shop", "hidden": "1"})
guest = Client(wsgi.application)
guest.post("/shop/cart/add", data={"product_id": PASS, "qty": 1})
check("customers can't check out while hidden", "The shop is closed" in body(guest.get("/shop/checkout")))
gj, cj = start("jeremy@example.com", who=staff)
r = staff.get("/shop/pay/" + cj)
sidj = order(cj)["stripe_session"]
pay_at_stripe(sidj)
r = staff.get("/shop/order/{}?session_id={}".format(cj, sidj))
check("staff can test-buy while the shop is hidden", r.status_code == 200 and order(cj)["status"] == "paid" and len(passes(cj)) == 1)
r = webhook("checkout.session.completed", sidj)
check("Stripe's messages get through while the shop is hidden", r.status_code == 200)
staff.post("/shop/admin/visibility", data={"which": "shop", "hidden": "0"})

# ---------------------------------------------------------------- 9. the bakery reads Stripe's messages without crashing
r = webhook("checkout.session.completed", "cs_test_1", secret=BAKERY_SECRET, path="/bakery/api/stripe/webhook",
            data={"id": "cs_b", "object": "checkout.session", "payment_status": "paid", "amount_total": 500, "metadata": {"orderId": "nope"}})
check("bakery: a signed message from Stripe is read without crashing", r.status_code == 200, (r.status_code, body(r)[:300]))
r = webhook("checkout.session.completed", "cs_test_1", secret="whsec_wrong", path="/bakery/api/stripe/webhook",
            data={"id": "cs_b", "object": "checkout.session", "metadata": {}})
check("bakery: a forged message is refused", r.status_code == 400)

summary = {"database": "postgres" if os.environ.get("DATABASE_URL") else "sqlite", "stripe_library": "real" if REAL else "stand-in",
           "passed": len(results) - len(failures), "total": len(results), "failures": failures}
print(json.dumps(summary, indent=1))
sys.exit(1 if failures else 0)
