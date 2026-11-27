"""Farm shop check: builds a catalog the way Staci would, sells a pass bundle, then uses the pass
to book playdates (online) and for walk-ins (at the gate), and checks every guard rail.

    python tests/shop_test.py            # SQLite, like the Mac
    DATABASE_URL=... python tests/shop_test.py   # Postgres, like the live server

Uses a brand-new throwaway database every run.
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
os.environ["FARM_TEST_MODE"] = "1"
work = tempfile.mkdtemp(prefix="shop-test-")
os.environ["PLAYDATE_DB"] = os.path.join(work, "playdates.db")
os.environ["FARM_DATA_DIR"] = work
os.environ.setdefault("ADMIN_PASSWORD", "playdate")
if os.environ.get("DATABASE_URL"):
    os.environ["FARM_DB_NAME"] = "shoptest_" + os.path.basename(work).replace("-", "_").lower()
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "playdates"))

import wsgi  # noqa: E402
import dbcompat  # noqa: E402
from werkzeug.test import Client  # noqa: E402

c = Client(wsgi.application)          # the farm (logged in to both admins)
guest = Client(wsgi.application)      # a customer
results, failures = [], []


def real_orders():
    """Treat every order so far as real (as if paid with a live Stripe key)."""
    c_ = dbcompat.connect(os.environ["PLAYDATE_DB"], isolation_level=None)
    c_.execute("UPDATE shop_orders SET test=0")
    c_.close()


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


def left(pass_code, credit_type_id):
    pid = q("SELECT id FROM shop_passes WHERE code=?", pass_code)[0]["id"]
    issued = sum(x["qty"] for x in q("SELECT qty FROM shop_pass_credits WHERE pass_id=? AND credit_type_id=?", pid, credit_type_id))
    used = sum(x["qty"] for x in q("SELECT qty FROM shop_pass_uses WHERE pass_id=? AND credit_type_id=?", pid, credit_type_id))
    return issued - used


def open_slot(client, tickets):
    html = body(client.get("/book?tickets={}".format(tickets)))
    for m in re.finditer(r'name="date" value="([^"]+)"><input type="hidden" name="start" value="([^"]+)">.*?<button class="slot"([^>]*)>', html, re.S):
        if "disabled" not in m.group(3):
            return m.group(1), m.group(2)
    return None, None


def hold(client, tickets, used=set()):
    """Hold the first free arrival time (skipping ones this test already used)."""
    html = body(client.get("/book?tickets={}".format(tickets)))
    for m in re.finditer(r'name="date" value="([^"]+)"><input type="hidden" name="start" value="([^"]+)">.*?<button class="slot"([^>]*)>', html, re.S):
        if "disabled" in m.group(3) or (m.group(1), m.group(2)) in used:
            continue
        used.add((m.group(1), m.group(2)))
        r = client.post("/book/hold", data={"date": m.group(1), "start": m.group(2), "tickets": tickets, "under2": 0})
        if "/checkout/" in loc(r):
            return loc(r).rstrip("/").split("/")[-1]
    return None


def details(client, code):
    return client.post("/checkout/" + code, data={"name": "Pass Family", "email": "pass@example.com", "agree": "on"})


# ---------------------------------------------------------------- Staci logs in and builds the catalog
r = c.get("/shop/admin")
check("admin needs a login", r.status_code == 302 and "/shop/admin/login" in loc(r), loc(r))
r = c.post("/shop/admin/login?next=/shop/admin/products", data={"password": "nope"})
check("wrong password refused", "That password didn" in body(r))
r = c.post("/shop/admin/login?next=/shop/admin/products", data={"password": "playdate"})
check("login returns to the shop page asked for", loc(r).endswith("/shop/admin/products"), loc(r))
c.post("/admin/login", data={"password": "playdate"})  # playdate admin too (to cancel a booking later)
check("a new shop starts hidden from customers", "The shop is closed" in body(guest.get("/shop/")))
c.post("/shop/admin/visibility", data={"which": "shop", "hidden": "0"})
check("Staci opens the shop", "The shop is closed" not in body(guest.get("/shop/")))

types = q("SELECT * FROM shop_credit_types ORDER BY id")
check("starter credit types exist", [t["booking_role"] for t in types] == ["package", "person"], types)
PLAY, EXTRA = types[0]["id"], types[1]["id"]
c.post("/shop/admin/credits", data={"name": "Flower picking (1 bucket)", "value": "15", "booking_role": "", "at_farm": "on",
                                    "available": "on", "unavailable_note": "Flower season is over"})
FLOWER = q("SELECT id FROM shop_credit_types WHERE name LIKE 'Flower%'")[0]["id"]
check("new credit type saved", FLOWER > 0)

c.post("/shop/admin/pages", data={"title": "Black Friday 2026", "intro": "Pre-buy next spring and save.", "starts_at": "", "ends_at": "", "hidden": "on"})
page = q("SELECT * FROM shop_pages")[0]
check("sale page starts hidden", page["hidden"] == 1 and page["slug"] == "black-friday-2026", page)
r = guest.get("/shop/s/black-friday-2026")
check("hidden sale page not public", r.status_code == 404)
c.post("/shop/admin/pages/{}/toggle".format(page["id"]))
check("sale page shown", q("SELECT hidden FROM shop_pages")[0]["hidden"] == 0)

r = c.post("/shop/admin/products/new?kind=pass", data={
    "name": "Spring Family Pass", "price": "120", "value_price": "150", "stock_limit": "3", "page_id": str(page["id"]),
    "cq_0": "2", "ct_0": str(PLAY), "cq_1": "10", "ct_1": str(EXTRA), "cq_2": "1", "ct_2": str(FLOWER),
    "season_label": "Good during the 2027 season", "valid_until": "2027-12-31", "taxable": "on",
    "terms": "Flower picking depends on the weather.", "hidden": "on", "photo_data": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="})
check("pass bundle saved", r.status_code == 302, body(r)[-400:])
bundle = q("SELECT * FROM shop_products WHERE name='Spring Family Pass'")[0]
check("bundle credits saved", sorted((x["credit_type_id"], x["qty"]) for x in q("SELECT * FROM shop_product_credits WHERE product_id=?", bundle["id"]))
      == sorted([(PLAY, 2), (EXTRA, 10), (FLOWER, 1)]))
check("photo saved", bool(bundle["photo"]) and guest.get("/shop/photos/" + bundle["photo"]).status_code == 200, bundle["photo"])
r = c.post("/shop/admin/products/new?kind=pass", data={"name": "Empty pass", "price": "10"})
check("pass without credits refused", r.status_code == 200 and "at least one credit" in body(r))
c.post("/shop/admin/products/new?kind=item", data={"name": "The Preserver's Garden", "price": "30", "can_ship": "1", "taxable": "on", "hidden": "on"})
book = q("SELECT * FROM shop_products WHERE name LIKE 'The Preserver%'")[0]
check("new products start hidden", book["hidden"] == 1)
r = guest.get("/shop/p/{}".format(book["id"]))
check("hidden product not public", r.status_code == 404)
for pid in (bundle["id"], book["id"]):
    if q("SELECT hidden FROM shop_products WHERE id=?", pid)[0]["hidden"]:
        c.post("/shop/admin/products/{}/toggle".format(pid))
r = guest.get("/shop/")
check("shop home lists the sale page and the book", "Black Friday 2026" in body(r) and "Preserver" in body(r))
r = guest.get("/shop/p/{}".format(bundle["id"]))
check("bundle page shows what's included and the value", "2 × Playdate" in body(r) and "$150 value" in body(r) and "Add to cart" in body(r))

# ---------------------------------------------------------------- a customer buys
def add_pass(client, pid, **kw):
    """Passes go in the cart one at a time (Nov 26, 2026); who each is for is asked at checkout."""
    data = {"product_id": pid}
    data.update(kw)
    return client.post("/shop/cart/add", data=data)


def owners(client, gifts=()):
    """Checkout fields saying who each pass in the cart is for: the names in `gifts` in cart order, then "me"."""
    keys = re.findall(r'name="qty_(\d+:[0-9a-f]{8})"', body(client.get("/shop/cart")))
    out = {}
    for i, k in enumerate(keys):
        g = gifts[i] if i < len(gifts) else ""
        out.update({"owner_" + k: "gift" if g else "me", "gift_" + k: g})
    return out


h = body(guest.get("/shop/p/{}".format(bundle["id"])))
check("pass page is just Add to cart: no 'How many', no who-it's-for (asked once, at checkout)",
      'name="qty"' not in h and 'name="owner"' not in h and "Who is this pass for" not in h and "Add it to your cart once for each pass" in h
      and "Choose “Someone else” at checkout" in h)
add_pass(guest, bundle["id"])
guest.post("/shop/cart/add", data={"product_id": book["id"], "qty": 1})
check("the cart doesn't claim who a pass is for before checkout", "For you" not in body(guest.get("/shop/cart")))
r = guest.get("/shop/checkout")
check("checkout is one order form: buyer, who the pass is for, shipping, order, terms",
      [x in body(r) for x in ("Your information", "Who the pass is for", "Someone else (a gift)", "Ship to", "Your order", "Terms")] == [True] * 6
      and body(r).index("Your information") < body(r).index("Who the pass is for") < body(r).index("Terms"))
check("checkout asks for the gift name exactly as it should appear", "Type it exactly as it should appear on the pass and gift card" in body(r))
check("checkout says passes go to the buyer, not the gift recipient", "emailed to you (not to the person you" in body(r))
check("neither Me nor Someone else is picked for you", not re.search(r'name="owner_[^"]+" value="(me|gift)" required checked', body(r)))
r = guest.post("/shop/checkout", data={"name": "Ann Buyer", "email": "ann@example.com"})
check("terms, address and who the pass is for are required", "agree to the terms" in body(r) and "shipping address" in body(r)
      and "choose who the Spring Family Pass is for" in body(r))
r = guest.post("/shop/checkout", data={"name": "Ann Buyer", "email": "ann@example.com", "agree": "on",
                                       **owners(guest, ["The Miller family"]),
                                       "ship_line1": "1 Main St", "ship_city": "Topeka", "ship_state": "ks", "ship_zip": "66601"})
check("order created", "/shop/pay/" in loc(r), loc(r))
ocode = loc(r).rstrip("/").split("/")[-1]
o = q("SELECT * FROM shop_orders WHERE code=?", ocode)[0]
rate = json.loads(q("SELECT value FROM settings WHERE key='tax_rate'")[0]["value"])
check("out-of-state book not taxed; pass taxed; flat shipping", o["subtotal"] == 150 and o["shipping"] == 6 and o["tax"] == round(120 * rate / 100, 2), o)
check("terms saved with the order", "2027 season" in (o["agreed_terms"] or "") and "weather" in (o["agreed_terms"] or ""))
r = guest.post("/shop/pay/" + ocode, data={"card": "4000 0000 0000 0002"})
check("declined card refused", "declined" in body(r) and q("SELECT status FROM shop_orders WHERE code=?", ocode)[0]["status"] == "pending")
r = guest.post("/shop/pay/" + ocode, data={"card": "4242 4242 4242 4242"})
check("payment goes through", "/shop/order/" in loc(r), loc(r))
passes = q("SELECT * FROM shop_passes WHERE order_id=?", o["id"])
check("one pass made", len(passes) == 1 and passes[0]["gift_for"] == "The Miller family" and passes[0]["valid_until"] == "2027-12-31", passes)
P = passes[0]["code"]
check("pass has the bundle's credits", left(P, PLAY) == 2 and left(P, EXTRA) == 10 and left(P, FLOWER) == 1)
check("receipt email queued with the pass link", any("/shop/pass/" + P in x["body"] for x in q("SELECT body FROM outbox WHERE recipient='ann@example.com'")))
r = guest.get("/shop/order/" + ocode)
check("order page links the pass", "/shop/pass/" + P in body(r))
r = guest.get("/shop/pass/" + P)
check("pass page shows QR and credits", "<svg" in body(r) and "Playdate" in body(r) and "Book a playdate with this pass" in body(r))
check("guests don't see staff buttons", "Farm use" not in body(r))
check("printable certificate", guest.get("/shop/pass/{}/print".format(P)).status_code == 200)
check("pass link works with the dash", guest.get("/shop/pass/{}-{}".format(P[:4], P[4:])).status_code == 302)

# ---------------------------------------------------------------- gift cards to print (Nov 26, 2026)
CARD = "/shop/pass/{}/card".format(P)
h = body(guest.get("/shop/order/" + ocode))
check("order page offers one gift card for a gift pass", "Print a gift card for The Miller family" in h and 'href="{}"'.format(CARD) in h
      and "/card/folded" not in h and "Folded" not in h)
h = body(guest.get(CARD))
check("gift card: To and From filled in, real QR and code, what's included",
      'data-fill="to" value="The Miller family"' in h and 'data-fill="from" value="Ann Buyer"' in h and "<svg" in h
      and P[:4] in h and "2 × Playdate" in h and "A gift for you" in h and "cut along the dashed line" in h)
check("only the flat card now (the folded one was dropped)", "Fold it in half" not in h and "Folded card" not in h and 'class="sheet flat"' in h)
check("gift card has no greeting printed (Jeremy's choice)", "Merry Christmas" not in h)
check("old folded and flat card links open the one card", all(loc(guest.get("/shop/pass/{}/card/{}".format(P, st))).endswith(CARD) for st in ("folded", "flat")))
check("an unknown card style is not found", guest.get("/shop/pass/{}/card/poster".format(P)).status_code == 404)
check("pass page links the gift card", 'href="{}"'.format(CARD) in body(guest.get("/shop/pass/" + P)))
mail = " ".join(x["body"] for x in q("SELECT body FROM outbox WHERE recipient='ann@example.com'"))
check("receipt links the gift card for each gift pass", "Giving it to The Miller family? Print a gift card" in mail and CARD + "\n" in mail + "\n")


# Staci's own picture on the front (Nov 26, 2026)
PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
LAMB = 'viewBox="0 0 220 170"'
check("cards start with the drawn lamb", body(guest.get(CARD)).count(LAMB) == 1)
guest.post("/shop/admin/settings/gift-card", data={"photo_data": PNG})
check("only staff can change the gift card picture", not q("SELECT value FROM shop_settings WHERE key='gift_card_photo'")
      or json.loads(q("SELECT value FROM shop_settings WHERE key='gift_card_photo'")[0]["value"]) == "")
check("staff see a sample card; customers don't", "Sample card" in body(c.get("/shop/admin/gift-card-sample"))
      and "Sample card" not in body(guest.get("/shop/admin/gift-card-sample")))
c.post("/shop/admin/settings/gift-card", data={"photo_data": PNG})
pic = json.loads(q("SELECT value FROM shop_settings WHERE key='gift_card_photo'")[0]["value"])
hl = body(guest.get(CARD))
check("Staci's picture replaces the lamb on the gift card", pic and 'class="coverphoto" src="/shop/photos/{}"'.format(pic) in hl
      and hl.count(LAMB) == 0, (pic, hl.count(LAMB)))
check("the picture is served", guest.get("/shop/photos/" + pic).status_code == 200)
check("settings page shows the current picture", "/shop/photos/" + pic in body(c.get("/shop/admin/settings")))
r = c.post("/shop/admin/settings/gift-card", data={"photo_data": ""}, follow_redirects=True)
check("saving with no photo chosen says so and keeps the picture", "Choose a photo first" in body(r)
      and json.loads(q("SELECT value FROM shop_settings WHERE key='gift_card_photo'")[0]["value"]) == pic)
c.post("/shop/admin/settings/gift-card", data={"remove": "1"})
check("going back to the lamb", body(guest.get(CARD)).count(LAMB) == 1
      and 'class="coverphoto"' not in body(guest.get(CARD)))
# ---------------------------------------------------------------- two passes, two people, one checkout
c.post("/shop/admin/products/new?kind=pass", data={"name": "Duo pass", "price": "20", "cq_0": "1", "ct_0": str(PLAY),
                                                   "season_label": "Good in 2027"})
DUO = q("SELECT id FROM shop_products WHERE name='Duo pass'")[0]["id"]
rem = Client(wsgi.application)
add_pass(rem, DUO)
k = re.findall(r'name="qty_({}:[0-9a-f]{{8}})"'.format(DUO), body(rem.get("/shop/cart")))[0]
rem.post("/shop/cart/update", data={"qty_" + k: "3"})
check("a pass line can't be turned into several", body(rem.get("/shop/cart")).count(">Remove</button>") == 1
      and "$20" in body(rem.get("/shop/cart")) and "$60" not in body(rem.get("/shop/cart")))
rem.post("/shop/cart/update", data={"qty_" + k: "0"})
check("Remove takes a pass out of the cart", "Your cart is empty" in body(rem.get("/shop/cart")))
duo = Client(wsgi.application)
add_pass(duo, DUO)
add_pass(duo, DUO)
h = body(duo.get("/shop/cart"))
keys = re.findall(r'name="qty_({}:[0-9a-f]{{8}})"'.format(DUO), h)
check("each pass is its own cart line with a Remove button", len(keys) == 2 and h.count(">Remove</button>") == 2, keys)
r = duo.post("/shop/checkout", data={"name": "Dee Duo", "email": "dee@example.com", "agree": "on",
                                     "owner_" + keys[0]: "gift", "gift_" + keys[0]: "",
                                     "owner_" + keys[1]: "gift", "gift_" + keys[1]: "Uncle Ray"})
check("a gift left without a name at checkout is caught", "name of the person getting" in body(r) and "/shop/pay/" not in loc(r))
check("two of the same pass: its terms show once", body(r).count("Duo pass: Good in 2027") == 1, body(r).count("Duo pass: Good in 2027"))
r = duo.post("/shop/checkout", data={"name": "Dee Duo", "email": "dee@example.com", "agree": "on",
                                     "owner_" + keys[0]: "me", "gift_" + keys[0]: "",
                                     "owner_" + keys[1]: "gift", "gift_" + keys[1]: "Uncle Ray"})
dcode = loc(r).rstrip("/").split("/")[-1]
duo.post("/shop/pay/" + dcode, data={"card": "4242 4242 4242 4242"})
made = q("SELECT p.gift_for, p.holder FROM shop_passes p JOIN shop_orders o ON o.id = p.order_id WHERE o.code=? ORDER BY p.id", dcode)
check("owners changed at checkout are what the passes get", sorted(m["gift_for"] for m in made) == ["", "Uncle Ray"]
      and all(m["holder"] == "Dee Duo" for m in made), made)

# stock limit: 3 bundles; one sold
guest2 = Client(wsgi.application)
for _ in range(3):
    add_pass(guest2, bundle["id"], qty=5)
r = guest2.get("/shop/cart")
check("passes go in one per line, capped at what's left", body(r).count(">Remove</button>") == 2 and 'name="qty_{}"'.format(bundle["id"]) not in body(r),
      body(r).count("For you"))

# ---------------------------------------------------------------- an item the buyer can have shipped or pick up
check("bundle page says electronic delivery", "Electronic delivery" in body(guest.get("/shop/p/{}".format(bundle["id"]))))
c.post("/shop/admin/products/new?kind=item", data={"name": "Signed copy", "price": "35", "can_ship": "1", "can_pickup": "1", "taxable": "on"})
signed = q("SELECT * FROM shop_products WHERE name='Signed copy'")[0]
check("ship + pick up boxes save as 'both'", signed["fulfillment"] == "both", signed["fulfillment"])
r = c.get("/shop/admin/products/{}".format(signed["id"]))
check("edit form shows both boxes ticked", body(r).count("checked>") >= 2 and 'name="can_ship" value="1" checked' in body(r)
      and 'name="can_pickup" value="1" checked' in body(r))
check("products list says ships or pick up", "Ships or pick up" in body(c.get("/shop/admin/products")))
check("product page offers both", "pick it up at the farm for free" in body(guest.get("/shop/p/{}".format(signed["id"]))))


def card_of(page, pid):
    """The product's card on a shop page (from its link to the end of the card)."""
    start = page.find('href="/shop/p/{}"'.format(pid))
    return page[start:page.find("</a>", start)] if start >= 0 else ""


home = body(c.get("/shop/"))
check("card tags an item that ships or picks up", "Shipping available" in card_of(home, signed["id"]) and "Farm pickup" in card_of(home, signed["id"]),
      card_of(home, signed["id"])[-400:])
check("card tags a pass as neither", "Shipping available" not in card_of(home, bundle["id"]) and "Farm pickup" not in card_of(home, bundle["id"])
      and "Electronic delivery" in card_of(home, bundle["id"]))
g3 = Client(wsgi.application)
g3.post("/shop/cart/add", data={"product_id": signed["id"], "qty": 1})
check("cart explains the choice", "or free if you pick it up" in body(g3.get("/shop/cart")))
r = g3.get("/shop/checkout")
check("checkout asks ship or pick up", 'name="how" value="ship"' in body(r) and 'name="how" value="pickup"' in body(r) and 'id="shipto"' in body(r))
r = g3.post("/shop/checkout", data={"name": "Pat Pickup", "email": "pat@example.com", "agree": "on"})
check("the choice is required", "shipped or pick it up" in body(r))
r = g3.post("/shop/checkout", data={"name": "Pat Pickup", "email": "pat@example.com", "agree": "on", "how": "pickup", "ship_line1": "half typed"})
check("pickup order needs no address", "/shop/pay/" in loc(r), body(r)[:300])
po = q("SELECT * FROM shop_orders WHERE code=?", loc(r).rstrip("/").split("/")[-1])[0]
check("pickup: no shipping, Missouri tax, no address kept", po["shipping"] == 0 and po["tax"] == round(35 * rate / 100, 2) and not po["ship_line1"], po)
check("pickup: line marked pick up", q("SELECT fulfillment FROM shop_order_lines WHERE order_id=?", po["id"])[0]["fulfillment"] == "pickup")
g3.post("/shop/pay/" + po["code"], data={"card": "4242 4242 4242 4242"})
g4 = Client(wsgi.application)
g4.post("/shop/cart/add", data={"product_id": signed["id"], "qty": 1})
r = g4.post("/shop/checkout", data={"name": "Sam Ship", "email": "sam@example.com", "agree": "on", "how": "ship"})
check("ship choice needs the address", "shipping address" in body(r) and 'value="ship" required checked' in body(r))
r = g4.post("/shop/checkout", data={"name": "Sam Ship", "email": "sam@example.com", "agree": "on", "how": "ship",
                                    "ship_line1": "2 Elm", "ship_city": "Tulsa", "ship_state": "OK", "ship_zip": "74101"})
so = q("SELECT * FROM shop_orders WHERE code=?", loc(r).rstrip("/").split("/")[-1])[0]
check("ship: flat shipping, no tax out of state", so["shipping"] == 6 and so["tax"] == 0 and so["ship_state"] == "OK", so)
check("ship: line marked ship", q("SELECT fulfillment FROM shop_order_lines WHERE order_id=?", so["id"])[0]["fulfillment"] == "ship")
g4.post("/shop/pay/" + so["code"], data={"card": "4242 4242 4242 4242"})
# ---------------------------------------------------------------- questions for the buyer (e.g. how to inscribe a book)
c.post("/shop/admin/products/new?kind=item", data={"name": "Inscribed book", "price": "30", "can_pickup": "1", "per_order_limit": "3",
       "q_label_0": "How would you like it inscribed?", "q_long_0": "1", "q_req_0": "1",
       "q_label_1": "Anything else we should know?", "q_long_1": "0", "q_label_2": ""}, content_type="multipart/form-data")
ib = q("SELECT * FROM shop_products WHERE name='Inscribed book'")[0]
ibcard = card_of(body(c.get("/shop/")), ib["id"])
check("card tags a pickup-only item as farm pickup only", "Farm pickup" in ibcard and "Shipping available" not in ibcard, ibcard[-300:])
qs = json.loads(ib["custom_fields"])
check("questions save", [x["label"] for x in qs] == ["How would you like it inscribed?", "Anything else we should know?"]
      and qs[0]["long"] and qs[0]["required"] and not qs[1]["long"] and not qs[1]["required"], qs)
h = body(c.get("/shop/admin/products/{}".format(ib["id"])))
check("product form shows the saved questions", 'value="How would you like it inscribed?"' in h and 'name="q_label_2"' in h)
h = body(guest.get("/shop/p/{}".format(ib["id"])))
check("product page asks the questions", "How would you like it inscribed?" in h and '<textarea name="answer_0" rows="3" maxlength="500" required>' in h
      and "<span>Anything else we should know? <span class=\"muted\"" in h)
g5 = Client(wsgi.application)
r = g5.post("/shop/cart/add", data={"product_id": ib["id"], "qty": 1, "answer_0": "  "})
check("a required answer can't be skipped", "/shop/p/{}".format(ib["id"]) in loc(r) and not q("SELECT 1 AS x FROM shop_order_lines WHERE name='Inscribed book'"))
g5.post("/shop/cart/add", data={"product_id": ib["id"], "qty": 1, "answer_0": "To Sam,\nHappy canning! - Jeremy"})
g5.post("/shop/cart/add", data={"product_id": ib["id"], "qty": 2, "answer_0": "To Pat", "answer_1": "It's a gift"})
h = body(g5.get("/shop/cart"))
check("different answers are separate cart lines", h.count('name="qty_{}:'.format(ib["id"])) == 2 and "Happy canning!" in h and "To Pat" in h and "It&#39;s a gift" in h)
r = g5.post("/shop/cart/add", data={"product_id": ib["id"], "qty": 1, "answer_0": "To Lee"})
check("per-order limit counts every line of the item", "as many Inscribed book" in body(g5.get(loc(r))) and "To Lee" not in body(g5.get("/shop/cart")))
g5.post("/shop/cart/add", data={"product_id": ib["id"], "qty": 1, "answer_0": "To Pat", "answer_1": "It's a gift"})
check("same answers add to the same line", body(g5.get("/shop/cart")).count("To Pat") == 1)
r = g5.post("/shop/checkout", data={"name": "Ann Answers", "email": "ann@example.com", "agree": "on"})
oc = loc(r).rstrip("/").split("/")[-1]
ol = q("SELECT l.* FROM shop_order_lines l JOIN shop_orders o ON o.id=l.order_id WHERE o.code=? ORDER BY l.id", oc)
by_qty = {x["qty"]: json.loads(x["answers"]) for x in ol}
check("each line keeps its answers on the order", len(ol) == 2 and [answer["a"] for answer in by_qty.get(1, [])] == ["To Sam,\nHappy canning! - Jeremy"]
      and by_qty.get(2) == [{"q": "How would you like it inscribed?", "a": "To Pat"}, {"q": "Anything else we should know?", "a": "It's a gift"}], ol)
g5.post("/shop/pay/" + oc, data={"card": "4242 4242 4242 4242"})
h = body(g5.get("/shop/order/" + oc))
check("receipt page shows the answers", "Happy canning!" in h and "To Pat" in h)
mail = q("SELECT body FROM outbox WHERE recipient='ann@example.com' ORDER BY id DESC")
check("receipt email shows the answers", mail and "How would you like it inscribed?: To Pat" in mail[0]["body"], mail[:1])
oid = q("SELECT id FROM shop_orders WHERE code=?", oc)[0]["id"]
check("admin order page shows the answers", "Happy canning!" in body(c.get("/shop/admin/orders/" + oc)))
real_orders()  # (these checks are about real orders; with no live Stripe key every order is a test)
check("orders spreadsheet has the answers", "How would you like it inscribed?: To Pat | Anything else we should know?: It's a gift" in body(c.get("/shop/admin/orders.csv")))
c.post("/shop/admin/products/{}".format(ib["id"]), data={"name": "Inscribed book", "price": "30", "can_pickup": "1", "q_label_0": ""},
       content_type="multipart/form-data")
check("questions can be turned off", q("SELECT custom_fields FROM shop_products WHERE id=?", ib["id"])[0]["custom_fields"] == ""
      and "answer_0" not in body(guest.get("/shop/p/{}".format(ib["id"]))))

# ---------------------------------------------------------------- one of a kind (e.g. a custom embroidered hat)
c.post("/shop/admin/products/new?kind=item", data={"name": "Embroidered hat", "price": "40", "can_pickup": "1", "one_of_a_kind": "1",
       "stock_limit": "5", "per_order_limit": "3"}, content_type="multipart/form-data")
hat = q("SELECT * FROM shop_products WHERE name='Embroidered hat'")[0]
check("one of a kind means exactly one, one per order", hat["one_of_a_kind"] == 1 and hat["stock_limit"] == 1 and hat["per_order_limit"] == 1, hat)
h = body(guest.get("/shop/"))
card = h[h.find("Embroidered hat") - 1500:h.find("Embroidered hat") + 800]
check("card says One of a kind, not Only 1 left", "One of a kind" in card and "Only 1 left" not in card)
h = body(guest.get("/shop/p/{}".format(hat["id"])))
check("product page: no quantity box, the tag, and a note", '<input type="hidden" name="qty" value="1">' in h and 'name="qty">' not in h
      and "One of a kind" in h and "Once it's sold, it's gone." in h)
check("product form shows it ticked", 'id="one_of_a_kind" checked' in body(c.get("/shop/admin/products/{}".format(hat["id"]))))
check("products list tags it", "One of a kind" in body(c.get("/shop/admin/products")))
g6 = Client(wsgi.application)
g6.post("/shop/cart/add", data={"product_id": hat["id"], "qty": 3})
h = body(g6.get("/shop/cart"))
check("can only put one in the cart", '<option value="1" selected>1</option>' in h and '<option value="2"' not in h)
g6.post("/shop/cart/update", data={"qty_{}".format(hat["id"]): "4"})
check("cart can't be bumped past one", "<option value=\"1\" selected>1</option>" in body(g6.get("/shop/cart")))
r = g6.post("/shop/checkout", data={"name": "Hat Fan", "email": "hat@example.com", "agree": "on"})
g6.post("/shop/pay/" + loc(r).rstrip("/").split("/")[-1], data={"card": "4242 4242 4242 4242"})
h = body(guest.get("/shop/"))
card = h[h.find("Embroidered hat") - 1500:h.find("Embroidered hat") + 800]
check("once bought, the card says Sold", ">Sold<" in card and "Sold out" not in card)
check("once bought, the page says it's been sold", "This one has been sold." in body(guest.get("/shop/p/{}".format(hat["id"]))))
c.post("/shop/admin/products/new?kind=pass", data={"name": "Unique pass test", "price": "5", "one_of_a_kind": "1", "cq_0": "1", "ct_0": str(PLAY)}, content_type="multipart/form-data")
check("passes ignore one of a kind", q("SELECT one_of_a_kind FROM shop_products WHERE name='Unique pass test'")[0]["one_of_a_kind"] == 0)

import farm_build
check("build number at the bottom of shop pages", "Build {}".format(farm_build.build()) in body(guest.get("/shop/")) and "buildtag" in body(c.get("/shop/admin")))
check("build number at the bottom of playdate pages", "buildtag" in body(Client(wsgi.application).get("/book")) and "buildtag" in body(Client(wsgi.application).get("/account/login")))
os.environ["RENDER_GIT_COMMIT"] = "300a131f0e9d8c7b6a5"
check("on Render it shows the deployed commit", "Build 300a131<" in body(guest.get("/shop/")))
os.environ.pop("RENDER_GIT_COMMIT")
import base64 as _b64
big_photo = "data:image/jpeg;base64," + _b64.b64encode(os.urandom(1200000)).decode()  # a 1.6 MB photo, bigger than the old 500 KB limit
r = c.post("/shop/admin/products/new?kind=item", data={"name": "Big photo book", "price": "30", "can_ship": "1", "photo_data": big_photo}, content_type="multipart/form-data")
big = q("SELECT * FROM shop_products WHERE name='Big photo book'")
check("product with a full-size photo saves", r.status_code == 302 and big and big[0]["photo"], (r.status_code, body(r)[:200]))
bid = big[0]["id"] if big else 0
bigcard = card_of(body(c.get("/shop/")), bid)
check("card tags a ship-only item as shipping only", "Shipping available" in bigcard and "Farm pickup" not in bigcard, bigcard[-300:])
r = c.post("/shop/admin/products/{}".format(bid), data={"name": "Big photo book", "price": "30", "can_ship": "1",
           "photo_fit": "whole", "photo_x": "20", "photo_y": "80", "photo_zoom": "2"}, content_type="multipart/form-data")
pv = json.loads(q("SELECT photo_view FROM shop_products WHERE id=?", bid)[0]["photo_view"] or "{}")
check("photo settings save", pv == {"fit": "whole", "x": 20.0, "y": 80.0, "zoom": 2.0}, pv)
check("photo kept when only settings change", q("SELECT photo FROM shop_products WHERE id=?", bid)[0]["photo"] == big[0]["photo"])
h = body(guest.get("/shop/p/{}".format(bid)))
check("whole photo shows uncropped on the product page", "object-fit:contain" in h and 'class="ph whole"' in h)
c.post("/shop/admin/products/{}".format(bid), data={"name": "Big photo book", "price": "30", "can_ship": "1",
       "photo_fit": "fill", "photo_x": "-40", "photo_y": "300", "photo_zoom": "9"}, content_type="multipart/form-data")
pv = json.loads(q("SELECT photo_view FROM shop_products WHERE id=?", bid)[0]["photo_view"])
check("photo settings kept in range", pv == {"fit": "fill", "x": 0.0, "y": 100.0, "zoom": 4.0}, pv)
h = body(guest.get("/shop/"))
check("shop cards use the photo settings", "object-position:0.0% 100.0%" in h and "scale(4.0)" in h)
h = body(c.get("/shop/admin/products/{}".format(bid)))
check("photo editor shows on the product form", 'id="photo_box"' in h and 'name="photo_zoom"' in h and 'id="photo_rotate"' in h)
r = c.post("/shop/admin/products/new?kind=item", data={"name": "Huge", "price": "1", "photo_data": "x" * (13 * 1024 * 1024)}, content_type="multipart/form-data")
check("an over-the-limit save gets a friendly message", r.status_code == 413 and "too big to save" in body(r) and not q("SELECT 1 AS x FROM shop_products WHERE name='Huge'"))
c.post("/shop/admin/products/new?kind=item", data={"name": "Farm sponsorship", "price": "25"})
spon = q("SELECT * FROM shop_products WHERE name='Farm sponsorship'")[0]
check("no boxes = electronic delivery", spon["fulfillment"] == "none" and "Electronic delivery" in body(guest.get("/shop/p/{}".format(spon["id"]))))

# ---------------------------------------------------------------- the pass books a playdate online (7 people)
b1 = hold(guest, 7)
check("held a time for 7", bool(b1))
r = guest.post("/checkout/{}/pass".format(b1), data={"pass_code": P[:4].lower() + "-" + P[4:].lower()})
r = guest.get("/checkout/" + b1)
check("checkout shows the pass covering it all", "Pass {}-{}".format(P[:4], P[4:]) in body(r) and "−$90" in body(r), re.findall(r"−\$[\d.]+", body(r)))
details(guest, b1)
r = guest.get("/pay/" + b1)
check("nothing to pay: no card asked", "Confirm my booking" in body(r) and 'name="card"' not in body(r))
r = guest.post("/pay/" + b1, data={})
check("booked with the pass", "/done/" in loc(r), loc(r))
bk1 = q("SELECT * FROM bookings WHERE code=?", b1)[0]
pay1 = q("SELECT * FROM payments WHERE booking_id=?", bk1["id"])
check("$0 payment recorded as pass", bk1["status"] == "paid" and pay1[0]["total"] == 0 and pay1[0]["method"] == "pass", pay1)
check("1 playdate + 2 extra people used", left(P, PLAY) == 1 and left(P, EXTRA) == 8)
r = guest.get("/done/" + b1)
check("confirmation names the pass", "from pass" in body(r))

# second booking, 6 people: guest chooses to pay for the extra person instead of using a credit
b2 = hold(guest, 6)
guest.post("/checkout/{}/pass".format(b2), data={"pass_code": P})
guest.post("/checkout/{}/pass/adjust".format(b2), data={"use_{}".format(PLAY): "1", "use_{}".format(EXTRA): "0"})
details(guest, b2)
r = guest.get("/pay/" + b2)
check("pays just the extra person", ">$10<" in body(r).replace(" ", "") or "$10" in body(r), re.findall(r"\$[\d.]+", body(r))[:6])
r = guest.post("/pay/" + b2, data={"card": "4242424242424242"})
pay2 = q("SELECT p.* FROM payments p JOIN bookings b ON b.id=p.booking_id WHERE b.code=?", b2)
check("card charged for the difference", "/done/" in loc(r) and pay2[0]["subtotal"] == 10 and pay2[0]["method"] == "test_card", pay2)
check("playdates used up, extras kept", left(P, PLAY) == 0 and left(P, EXTRA) == 8)

# third booking, 3 people: no playdate left and extras don't apply to a small group
b3 = hold(guest, 3)
r = guest.post("/checkout/{}/pass".format(b3), data={"pass_code": P}, follow_redirects=True)
check("pass with nothing usable is turned away politely", "have any credits that work" in body(r))

# ---------------------------------------------------------------- at the gate (summer walk-in)
r = guest.post("/shop/pass/{}/use".format(P), data={"credit_type_id": EXTRA, "qty": 3})
check("guests can't tick off credits", r.status_code == 302 and "/admin/login" in loc(r) and left(P, EXTRA) == 8)
r = c.get("/shop/pass/" + P)
check("staff see farm-use buttons", "Farm use" in body(r))
c.post("/shop/pass/{}/use".format(P), data={"credit_type_id": EXTRA, "qty": 3})
check("3 walk-in admissions ticked off", left(P, EXTRA) == 5)
r = c.post("/shop/pass/{}/use".format(P), data={"credit_type_id": PLAY, "qty": 1}, follow_redirects=True)
check("booking-only credit can't be used at the gate", "used when booking online" in body(r))
r = c.post("/shop/pass/{}/use".format(P), data={"credit_type_id": EXTRA, "qty": 9}, follow_redirects=True)
check("can't use more than is left", "Only 5" in body(r) and left(P, EXTRA) == 5)
use_id = q("SELECT id FROM shop_pass_uses WHERE where_used='farm' ORDER BY id DESC")[0]["id"]
c.post("/shop/pass/{}/undo/{}".format(P, use_id))
check("undo puts them back", left(P, EXTRA) == 8)
r = c.post("/shop/pass/{}/undo/{}".format(P, use_id), follow_redirects=True)
check("undo only once", "already undone" in body(r) and left(P, EXTRA) == 8)

# seasonal switch
c.post("/shop/admin/credits/{}/season".format(FLOWER))
r = c.post("/shop/pass/{}/use".format(P), data={"credit_type_id": FLOWER, "qty": 1}, follow_redirects=True)
check("out-of-season credit can't be used", "Flower season is over" in body(r) and left(P, FLOWER) == 1)
r = guest.get("/shop/pass/" + P)
check("pass page says it's out of season", "Flower season is over" in body(r))
c.post("/shop/admin/credits/{}/season".format(FLOWER))
c.post("/shop/pass/{}/use".format(P), data={"credit_type_id": FLOWER, "qty": 1})
check("back in season: used", left(P, FLOWER) == 0)

# ---------------------------------------------------------------- the farm cancels a booking: credits come back
c.post("/admin/bookings/{}/cancel".format(bk1["id"]))
check("cancelled booking's credits returned", left(P, PLAY) == 1 and left(P, EXTRA) == 10,
      (left(P, PLAY), left(P, EXTRA)))

# ---------------------------------------------------------------- codes made by hand (donation)
r = c.post("/shop/admin/passes/new", data={"title": "Gift playdate", "gift_for": "Auction winner", "cq_0": "1", "ct_0": str(PLAY),
                                           "season_label": "Good during the 2027 season", "valid_until": "2027-12-31",
                                           "note": "Rogersville Elementary auction"})
made = q("SELECT * FROM shop_passes WHERE source='made'")
check("made a code", len(made) == 1 and made[0]["note"] == "Rogersville Elementary auction" and "/shop/pass/" in loc(r))
M = made[0]["code"]
b4 = hold(guest, 4)
guest.post("/checkout/{}/pass".format(b4), data={"pass_code": M})
details(guest, b4)
r = guest.post("/pay/" + b4, data={})
check("donated code books a playdate for free", "/done/" in loc(r) and left(M, PLAY) == 0, loc(r))
b5 = hold(guest, 4)
r = guest.post("/checkout/{}/pass".format(b5), data={"pass_code": M}, follow_redirects=True)
check("used-up code can't be used again", "have any credits" in body(r))

# expired and turned-off passes
con = dbcompat.connect(os.environ["PLAYDATE_DB"])
con.execute("UPDATE shop_passes SET valid_until='2020-01-01' WHERE code=?", (M,))
con.commit()
con.close()
r = guest.post("/checkout/{}/pass".format(b5), data={"pass_code": M}, follow_redirects=True)
check("expired pass refused", "was good through" in body(r), (b5, re.findall(r'class="flash"[^>]*>([^<]+)', body(r))))
c.post("/shop/pass/{}/edit".format(P), data={"holder": "Ann Buyer", "gift_for": "The Miller family", "valid_until": "2027-12-31",
                                             "season_label": "Good during the 2027 season", "void": "on"})
r = guest.post("/checkout/{}/pass".format(b5), data={"pass_code": P}, follow_redirects=True)
check("turned-off pass refused", "no longer active" in body(r))

# guessing codes
g3 = Client(wsgi.application)
b6 = hold(g3, 4)
for i in range(8):
    g3.post("/checkout/{}/pass".format(b6), data={"pass_code": "ZZZZZZZ{}".format(i % 8 + 2)})
r = g3.post("/checkout/{}/pass".format(b6), data={"pass_code": P}, follow_redirects=True)
check("too many wrong codes blocks guessing", "Too many codes" in body(r))

# ---------------------------------------------------------------- hiding things
c.post("/shop/admin/visibility", data={"which": "booking", "hidden": "1", "message": "Booking opens March 1!"})
r = guest.get("/book")
check("hidden booking page shows the message", "Booking opens March 1!" in body(r) and 'class="slot"' not in body(r))
r = guest.post("/book/hold", data={"date": "2027-03-20", "start": "10:00", "tickets": 4})
check("hidden booking page can't be booked", "/checkout/" not in loc(r))
r = c.get("/book")
check("farm still sees the booking page, with a banner", "hidden from guests" in body(r) and "Pick a day" in body(r))
check("tickets still open while hidden", guest.get("/t/" + b2).status_code == 200)
c.post("/shop/admin/visibility", data={"which": "booking", "hidden": "0"})
check("booking page back", "Pick a day" in body(guest.get("/book")))

c.post("/shop/admin/visibility", data={"which": "shop", "hidden": "1", "message": "Back on Black Friday!"})
r = guest.get("/shop/")
check("hidden shop shows the message", "Back on Black Friday!" in body(r))
r = guest.post("/shop/cart/add", data={"product_id": book["id"], "qty": 1})
check("hidden shop can't sell", "Back on Black Friday!" in body(r))
check("farm still sees the hidden shop", "Black Friday 2026" in body(c.get("/shop/")))
check("pass pages still work while the shop is hidden", guest.get("/shop/pass/" + P).status_code == 200)
c.post("/shop/admin/visibility", data={"which": "shop", "hidden": "0"})

# ---------------------------------------------------------------- one farm address: shop + booking together
FARM = "https://shop.gooseberrybridge.com"
visitor = Client(wsgi.application)
c.post("/shop/admin/visibility", data={"which": "shop", "hidden": "1"})
r = visitor.get("/", base_url=FARM, follow_redirects=True)
check("front page is the booking page while the shop is hidden", r.status_code == 200 and "Pick a day" in body(r), r.status_code)
c.post("/shop/admin/visibility", data={"which": "shop", "hidden": "0"})
r = visitor.get("/", base_url=FARM)
check("front page is the shop while it's open", r.status_code == 302 and loc(r).endswith("/shop/"), (r.status_code, loc(r)))
h = body(visitor.get("/shop/", base_url=FARM))
check("shop front page has a Book a playdate tile on the same address", 'class="card bookplay" href="{}/book"'.format(FARM) in h, h[:300])
check("booking works on the farm address", "Pick a day" in body(visitor.get("/book", base_url=FARM)))
check("pass pages work on the farm address", visitor.get("/shop/pass/" + P, base_url=FARM).status_code == 200)
check("staff admins live on the farm address", "/admin/login" in loc(visitor.get("/admin", base_url=FARM))
      and "/shop/admin/login" in loc(visitor.get("/shop/admin", base_url=FARM)))
c.post("/shop/admin/visibility", data={"which": "booking", "hidden": "1"})
check("no Book a playdate tile while booking is hidden", "bookplay" not in body(visitor.get("/shop/", base_url=FARM)))
# Nov 26, 2026: the hidden booking page showed a blank button that just reloaded it, and pass pages and receipts
# still sent pass buyers to that page.
h = body(visitor.get("/book", base_url=FARM))
check("hidden booking page sends visitors to the open shop", 'href="/shop/">Visit our farm shop</a>' in h, h[-400:])
check("...and never shows an empty button", not re.search(r'<a class="btn" href="[^"]*">\s*</a>', h))
c.post("/shop/admin/visibility", data={"which": "shop", "hidden": "1"})
h = body(visitor.get("/book", base_url=FARM))
check("with the shop hidden too, the booking message has no button at all", "Visit our farm shop" not in h
      and not re.search(r'<a class="btn" href="[^"]*">\s*</a>', h) and "isn" in h, h[-400:])
c.post("/shop/admin/visibility", data={"which": "shop", "hidden": "0"})
bea = Client(wsgi.application)
add_pass(bea, bundle["id"])
r = bea.post("/shop/checkout", data={"name": "Bea Buyer", "email": "bea@example.com", "agree": "on", **owners(bea)})
bcode = loc(r).rstrip("/").split("/")[-1]
bea.post("/shop/pay/" + bcode, data={"card": "4242 4242 4242 4242"})
mail = " ".join(x["body"] for x in q("SELECT body FROM outbox WHERE recipient='bea@example.com'"))
check("receipt says booking opens soon while it's hidden", "Playdate booking opens soon" in mail and "/shop/pass/" in mail, mail[-400:])
BP = q("SELECT p.code FROM shop_passes p JOIN shop_orders o ON o.id = p.order_id WHERE o.code=?", bcode)[0]["code"]
h = body(visitor.get("/shop/pass/" + BP, base_url=FARM))
check("pass page doesn't send people to the hidden booking page", "Book a playdate with this pass" not in h and "booking isn't open yet" in h)
c.post("/shop/admin/visibility", data={"which": "booking", "hidden": "0"})
check("pass page offers booking again once it's open", "Book a playdate with this pass" in body(visitor.get("/shop/pass/" + BP, base_url=FARM)))
staff = Client(wsgi.application)
staff.post("/shop/admin/login", data={"password": "playdate"}, base_url=FARM)
c.post("/shop/admin/visibility", data={"which": "shop", "hidden": "1"})
h = body(staff.get("/shop/", base_url=FARM))
check("staff see the hidden shop on the farm address, with a banner", "hidden from customers" in h and "bookplay" in h, h[:300])
check("customers see the closed message there", "hidden from customers" not in body(visitor.get("/shop/", base_url=FARM))
      and visitor.get("/shop/cart", base_url=FARM).status_code in (200, 302))
staff.post("/shop/cart/add", data={"product_id": book["id"], "qty": 1}, base_url=FARM)
check("staff can test-buy while the shop is hidden", "The Preserver" in body(staff.get("/shop/cart", base_url=FARM)))
c.post("/shop/admin/visibility", data={"which": "shop", "hidden": "0"})
con = dbcompat.connect(os.environ["PLAYDATE_DB"]); con.execute("DELETE FROM shop_settings WHERE key='shop_hidden'"); con.commit(); con.close()
r = visitor.get("/", base_url=FARM)
check("a brand-new install treats the shop as hidden (front page stays on booking)", "/shop/" not in loc(r), loc(r))
c.post("/shop/admin/visibility", data={"which": "shop", "hidden": "0"})
h = body(staff.get("/shop/admin", base_url=FARM))
check("admin bar links stay on the farm address", 'href="{}/admin"'.format(FARM) in h and 'href="{}/shop/admin"'.format(FARM) in h
      and "booking.gooseberrybridge.com" not in h, h[:400])

# sale windows
later = (dt.datetime.now() + dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
c.post("/shop/admin/pages", data={"id": page["id"], "title": "Black Friday 2026", "slug": "black-friday-2026", "starts_at": later})
r = guest.get("/shop/s/black-friday-2026")
check("upcoming sale shows when it opens", "This sale opens" in body(r))
r = guest.post("/shop/cart/add", data={"product_id": bundle["id"], "owner": "me"}, follow_redirects=True)
check("can't buy before the sale opens", "available right now" in body(r))
earlier = (dt.datetime.now() - dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
c.post("/shop/admin/pages", data={"id": page["id"], "title": "Black Friday 2026", "slug": "black-friday-2026", "starts_at": "", "ends_at": earlier})
r = guest.get("/shop/p/{}".format(bundle["id"]))
check("ended sale can't be bought", "This sale has ended" in body(r) and "Add to cart" not in body(r))

# ---------------------------------------------------------------- admin pages and reports
for path in ["/shop/admin/products/new?kind=pass", "/shop/admin/products/new?kind=item", "/shop/admin/products/new",
             "/shop/admin/credits?edit=new", "/shop/admin/credits?edit={}".format(PLAY), "/shop/admin/pages?edit=new",
             "/shop/admin/pages?edit={}".format(page["id"]), "/shop/admin/passes/new", "/shop/admin/scan",
             "/shop/admin/products/{}".format(book["id"]), "/shop/", "/shop/cart", "/shop/s/black-friday-2026",
             "/shop/admin", "/shop/admin/products", "/shop/admin/credits", "/shop/admin/pages", "/shop/admin/orders",
             "/shop/admin/orders/" + ocode, "/shop/admin/passes", "/shop/admin/passes?q=Miller", "/shop/admin/settings",
             "/shop/admin/products/{}".format(bundle["id"])]:
    r = c.get(path)
    check("admin page loads: " + path, r.status_code == 200, r.status_code)
check("pass search finds the gift name", P in body(c.get("/shop/admin/passes?q=Miller")).replace("-", ""))
r = c.get("/shop/admin")
check("dashboard shows credits still owed", "Credits still owed" in body(r))
real_orders()
r = c.get("/shop/admin/orders.csv")
check("orders spreadsheet", r.status_code == 200 and "Spring Family Pass" in body(r))
c.post("/shop/admin/orders/{}/fulfilled".format(ocode), data={"tell": "1"})
check("mark shipped emails the customer", q("SELECT fulfilled_at FROM shop_orders WHERE code=?", ocode)[0]["fulfilled_at"] is not None
      and any("has shipped" in (x["subject"] or "") for x in q("SELECT subject FROM outbox WHERE recipient='ann@example.com'")))
r = c.get("/shop/admin/scan?code=" + P)
check("scan finds a pass", "/shop/pass/" + P in loc(r))
r = c.get("/shop/admin/scan?code=" + b2)
check("scan sends playdate tickets to check-in", "/admin/checkin/" + b2 in loc(r), loc(r))
c.post("/shop/admin/products/{}/copy".format(bundle["id"]))
check("copy a bundle", len(q("SELECT id FROM shop_products WHERE name='Copy of Spring Family Pass' AND hidden=1")) == 1)
r = c.post("/shop/admin/settings", data={"shop_name": "Farm Shop", "intro": "Hi", "terms": "Final sale.", "pickup_note": "Pick up at the barn.",
                                         "shop_hidden_message": "Closed", "shipping_flat": "5", "tax_rate": ""})
check("settings save", r.status_code == 302 and json.loads(q("SELECT value FROM shop_settings WHERE key='shipping_flat'")[0]["value"]) == 5)
check("playdate admin links to the shop", "/shop/admin" in body(c.get("/admin")))
check("bakery untouched", Client(wsgi.application).get("/bakery/").status_code == 200)

# ---------------------------------------------------------------- test orders: tagged, hidden, deletable
real_orders()
c.post("/shop/admin/visibility", data={"which": "shop", "hidden": "0"})
c.post("/shop/admin/products/new?kind=pass", data={"name": "Test-order pass", "price": "20", "cq_0": "1", "ct_0": str(PLAY)})
TP = q("SELECT id FROM shop_products WHERE name='Test-order pass'")[0]["id"]
g9 = Client(wsgi.application)
add_pass(g9, TP)
r = g9.post("/shop/checkout", data={"name": "Tess Test", "email": "tess@example.com", "agree": "on", **owners(g9),
                                    "ship_line1": "1 Main St", "ship_city": "Ozark", "ship_state": "MO", "ship_zip": "65721"})
tcode = loc(r).rstrip("/").split("/")[-1]
g9.post("/shop/pay/" + tcode, data={"card": "4242424242424242"})
T = q("SELECT * FROM shop_orders WHERE code=?", tcode)[0]
check("an order with no live Stripe key is tagged Test", T["test"] == 1 and T["status"] == "paid", T)
ol = body(c.get("/shop/admin/orders"))
check("test orders stay off the Orders list", "Tess Test" not in ol and "Test orders (1)" in ol)
check("...and out of the spreadsheet", "Tess Test" not in body(c.get("/shop/admin/orders.csv")))
check("...but show under Test orders", "Tess Test" in body(c.get("/shop/admin/orders?status=test")))
real = q("SELECT code FROM shop_orders WHERE test=0 AND status='paid' ORDER BY id LIMIT 1")[0]["code"]
r = c.post("/shop/admin/orders/{}/delete".format(real))
check("a real order can't be deleted", q("SELECT id FROM shop_orders WHERE code=?", real) and "Only test orders" in body(c.get(loc(r))))
tp = q("SELECT id FROM shop_passes WHERE order_id=?", T["id"])
check("(setup) the test order made a pass", len(tp) == 1)
r = guest.post("/shop/admin/orders/delete-tests")
check("customers can't delete anything", q("SELECT id FROM shop_orders WHERE code=?", tcode))
real_before = {"lines": q("SELECT COUNT(*) AS n FROM shop_order_lines WHERE order_id IN (SELECT id FROM shop_orders WHERE test=0)")[0]["n"],
               "passes": q("SELECT COUNT(*) AS n FROM shop_passes WHERE order_id IN (SELECT id FROM shop_orders WHERE test=0)")[0]["n"],
               "credits": q("SELECT COUNT(*) AS n FROM shop_pass_credits WHERE pass_id IN (SELECT p.id FROM shop_passes p JOIN shop_orders o ON o.id=p.order_id WHERE o.test=0)")[0]["n"]}
r = c.post("/shop/admin/orders/delete-tests")
real_after = {"lines": q("SELECT COUNT(*) AS n FROM shop_order_lines WHERE order_id IN (SELECT id FROM shop_orders WHERE test=0)")[0]["n"],
              "passes": q("SELECT COUNT(*) AS n FROM shop_passes WHERE order_id IN (SELECT id FROM shop_orders WHERE test=0)")[0]["n"],
              "credits": q("SELECT COUNT(*) AS n FROM shop_pass_credits WHERE pass_id IN (SELECT p.id FROM shop_passes p JOIN shop_orders o ON o.id=p.order_id WHERE o.test=0)")[0]["n"]}
check("...real orders keep every item, pass and credit", real_before == real_after and real_before["passes"] > 0, (real_before, real_after))
check("Delete all test orders removes them and their passes", not q("SELECT id FROM shop_orders WHERE code=?", tcode)
      and not q("SELECT id FROM shop_passes WHERE order_id=?", T["id"]) and not q("SELECT id FROM shop_pass_credits WHERE pass_id=?", tp[0]["id"])
      and not q("SELECT id FROM shop_order_lines WHERE order_id=?", T["id"]))
check("...and leaves real orders alone", q("SELECT id FROM shop_orders WHERE code=?", real) and q("SELECT COUNT(*) AS n FROM shop_orders WHERE test=0")[0]["n"] > 0)

# ---------------------------------------------------------------- staff adjust any credit on a pass
r = c.post("/shop/admin/passes/new", data={"title": "Adjust me", "cq_0": "1", "ct_0": str(PLAY), "cq_1": "5", "ct_1": str(EXTRA)})
A = q("SELECT code FROM shop_passes WHERE title='Adjust me'")[0]["code"]
page_ = body(c.get("/shop/pass/" + A))
check("staff see Adjust credits on a pass", "Adjust credits" in page_ and 'name="left_{}"'.format(PLAY) in page_ and 'name="add_type"' in page_)
check("customers don't", "Adjust credits" not in body(guest.get("/shop/pass/" + A)))
r = c.post("/shop/pass/{}/adjust".format(A), data={"left_{}".format(PLAY): "0", "left_{}".format(EXTRA): "5", "reason": ""})
check("a reason is required", left(A, PLAY) == 1)
c.post("/shop/pass/{}/adjust".format(A), data={"left_{}".format(PLAY): "0", "left_{}".format(EXTRA): "5", "reason": "Booked by phone for May 2"})
check("staff mark a booking-only playdate used", left(A, PLAY) == 0 and left(A, EXTRA) == 5, (left(A, PLAY), left(A, EXTRA)))
h = body(c.get("/shop/pass/" + A))
check("...and the history says so", "adjusted: Booked by phone for May 2" in h)
c.post("/shop/pass/{}/adjust".format(A), data={"left_{}".format(PLAY): "1", "left_{}".format(EXTRA): "7", "reason": "Goodwill"})
cr = {x["credit_type_id"]: x for x in q("SELECT credit_type_id, SUM(qty) AS qty FROM shop_pass_credits WHERE pass_id=(SELECT id FROM shop_passes WHERE code=?) GROUP BY credit_type_id", A)}
check("raising gives back used credits first, then adds more", left(A, PLAY) == 1 and left(A, EXTRA) == 7
      and int(cr[PLAY]["qty"]) == 1 and int(cr[EXTRA]["qty"]) == 7, (left(A, PLAY), left(A, EXTRA), cr))
c.post("/shop/pass/{}/adjust".format(A), data={"left_{}".format(PLAY): "1", "left_{}".format(EXTRA): "7", "add_type": str(FLOWER), "add_qty": "2",
                                               "reason": "Thank-you for helping at the event"})
check("staff add a credit the pass didn't have", left(A, FLOWER) == 2)
h = body(c.get("/shop/pass/" + A))
check("...shown in the history", "added 2: Thank-you for helping at the event" in h)
r = guest.post("/shop/pass/{}/adjust".format(A), data={"left_{}".format(PLAY): "9", "reason": "hack"})
check("customers can't adjust", left(A, PLAY) == 1 and r.status_code in (302, 303))
c.post("/shop/pass/{}/adjust".format(A), data={"left_{}".format(PLAY): "-4", "reason": "typo"})
check("a count can't go below zero", left(A, PLAY) == 0)
c.post("/shop/pass/{}/adjust".format(A), data={"left_{}".format(PLAY): "1", "reason": "undo typo"})
check("a playdate given back can be booked online again", left(A, PLAY) == 1)

# The "one save, once" script with its slow-save safety net (Nov 26, 2026): same file on the shop and playdate pages.
shop_js = open(os.path.join(ROOT, "shop", "static", "once.js")).read()
check("slow-save safety net is in the shop's script and the playdate pages' copy is the same",
      "taking longer than usual" in shop_js and shop_js == open(os.path.join(ROOT, "playdates", "static", "once.js")).read())
check("shop and playdate pages load it", "/shop/static/once.js" in body(guest.get("/shop/cart")) and "once.js" in body(guest.get("/book")))

check("links that stall get a note with a Try again button", "data-once-slow-link" in shop_js and "Try again" in shop_js and "window.stop()" in shop_js)

# The farm's icon (Nov 26, 2026): the leaf for browser tabs, the sprig for phone home screens; Mollie keeps her own.
icon_files = {name: open(os.path.join(ROOT, "icons", name), "rb").read() for name in ("favicon.ico", "icon.svg", "apple-touch-icon.png")}
for path, name, kind in (("/favicon.ico", "favicon.ico", "image/x-icon"), ("/icon.svg", "icon.svg", "image/svg+xml"),
                         ("/apple-touch-icon.png", "apple-touch-icon.png", "image/png"),
                         ("/apple-touch-icon-precomposed.png", "apple-touch-icon.png", "image/png")):
    r = visitor.get(path, base_url=FARM)
    check("farm address serves " + path, r.status_code == 200 and r.data == icon_files[name] and r.headers["Content-Type"] == kind,
          (r.status_code, r.headers.get("Content-Type")))
check("phone icon is a real 180px PNG", icon_files["apple-touch-icon.png"][:8] == b"\x89PNG\r\n\x1a\n"
      and int.from_bytes(icon_files["apple-touch-icon.png"][16:20], "big") == 180)
check("tab icon is a real .ico", icon_files["favicon.ico"][:4] == b"\x00\x00\x01\x00")
ICON_LINK = 'rel="icon" href="/favicon.ico?v=1"'
for label, url in (("shop page", "/shop/"), ("product page", "/shop/p/{}".format(signed["id"])), ("booking page", "/book"),
                   ("sign-in page", "/account/login"), ("shop admin sign-in", "/shop/admin/login"), ("playdate admin sign-in", "/admin/login")):
    h = body(visitor.get(url, base_url=FARM))
    check("the icon is on the " + label, ICON_LINK in h and 'rel="apple-touch-icon" href="/apple-touch-icon.png?v=1"' in h)
check("the icon is on the gift card", ICON_LINK in body(c.get("/shop/admin/gift-card-sample")))
bk = Client(wsgi.application)
r = bk.get("/favicon.ico", base_url="https://bakery.example.test")
check("Mollie's address doesn't get the farm icon", r.data != icon_files["favicon.ico"], r.status_code)
r = bk.get("/account/login", base_url="https://bakery.example.test")
check("Mollie's sign-in page doesn't link the farm icon", r.status_code == 200 and "Mollie" in body(r) and ICON_LINK not in body(r), r.status_code)

summary = {"database": "postgres" if os.environ.get("DATABASE_URL") else "sqlite", "passed": len(results) - len(failures),
           "total": len(results), "failures": failures}
print(json.dumps(summary, indent=1))
sys.exit(1 if failures else 0)
