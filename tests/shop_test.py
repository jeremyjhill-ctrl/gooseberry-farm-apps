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
guest.post("/shop/cart/add", data={"product_id": bundle["id"], "qty": 1})
guest.post("/shop/cart/add", data={"product_id": book["id"], "qty": 1})
r = guest.get("/shop/checkout")
check("checkout asks who the pass is for", "a gift for" in body(r) and "Ship to" in body(r))
r = guest.post("/shop/checkout", data={"name": "Ann Buyer", "email": "ann@example.com"})
check("terms and address required", "agree to the terms" in body(r) and "shipping address" in body(r))
r = guest.post("/shop/checkout", data={"name": "Ann Buyer", "email": "ann@example.com", "agree": "on",
                                       "gift_{}_0".format(bundle["id"]): "The Miller family",
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

# stock limit: 3 bundles; one sold
guest2 = Client(wsgi.application)
guest2.post("/shop/cart/add", data={"product_id": bundle["id"], "qty": 5})
r = guest2.get("/shop/cart")
check("cart capped at what's left", 'value="2" selected' in body(r), re.findall(r'value="\d+" selected', body(r)))

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

# sale windows
later = (dt.datetime.now() + dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
c.post("/shop/admin/pages", data={"id": page["id"], "title": "Black Friday 2026", "slug": "black-friday-2026", "starts_at": later})
r = guest.get("/shop/s/black-friday-2026")
check("upcoming sale shows when it opens", "This sale opens" in body(r))
r = guest.post("/shop/cart/add", data={"product_id": bundle["id"], "qty": 1}, follow_redirects=True)
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

summary = {"database": "postgres" if os.environ.get("DATABASE_URL") else "sqlite", "passed": len(results) - len(failures),
           "total": len(results), "failures": failures}
print(json.dumps(summary, indent=1))
sys.exit(1 if failures else 0)
