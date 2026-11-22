"""Shipping label check for the farm shop: Print a shipping label -> prices -> one tap buys it -> print, tracking email,
order marked shipped; void; double-taps; Shippo refusing or down; settings and product sizes.

Shippo is replaced by a stand-in. The real connection code (farm_shippo.call) is also run against a small local web
server that answers like Shippo, to check the login header, errors and an unreachable Shippo.

    python tests/label_test.py                    # SQLite, like the Mac
    DATABASE_URL=... python tests/label_test.py   # Postgres, like the live server
"""
import http.server
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
work = tempfile.mkdtemp(prefix="label-test-")
os.environ.update(PLAYDATE_DB=os.path.join(work, "p.db"), FARM_DATA_DIR=work, FARM_TEST_MODE="1",
                  MAIL_PROVIDER="memory", PLAYDATE_EMAILS="on", ADMIN_PASSWORD="playdate", SHIPPO_API_KEY="shippo_test_standin")
os.environ.pop("FARM_STRIPE_SECRET_KEY", None)
if os.environ.get("DATABASE_URL"):
    os.environ["FARM_DB_NAME"] = "labeltest_" + os.path.basename(work).replace("-", "_").lower()
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "playdates"))

import wsgi  # noqa: E402
import dbcompat  # noqa: E402
import farm_shippo  # noqa: E402
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


def flashes(c, path):
    return " ".join(re.findall(r'class="flash[^"]*"[^>]*>([^<]+)', body(c.get(path))))


# ---------------------------------------------------------------- the Shippo stand-in
CALLS = []
TROUBLE = {"no_rates": False, "buy_fails": False, "refund_fails": False, "slow_buy": 0.0}
real_call = farm_shippo.call


def standin(method, path, data=None):
    CALLS.append((method, path, data))
    if path == "/shipments/":
        if TROUBLE["no_rates"]:
            return {"object_id": "shp_x", "rates": [], "messages": [{"text": "The address as submitted could not be found."}]}
        n = len([c for c in CALLS if c[1] == "/shipments/"])
        return {"object_id": "shp_{}".format(n), "status": "SUCCESS", "rates": [
            {"object_id": "rate_pri_{}".format(n), "amount": "9.85", "provider": "USPS", "estimated_days": 2,
             "servicelevel": {"name": "Priority Mail", "token": "usps_priority"}},
            {"object_id": "rate_ga_{}".format(n), "amount": "5.62", "provider": "USPS", "estimated_days": 4,
             "servicelevel": {"name": "Ground Advantage", "token": "usps_ground_advantage"}},
            {"object_id": "rate_mm_{}".format(n), "amount": "6.20", "provider": "USPS", "estimated_days": 6,
             "servicelevel": {"name": "Media Mail", "token": "usps_media_mail"}},
            {"object_id": "rate_bad", "amount": None, "provider": "X"}]}
    if path == "/transactions/" and method == "POST":
        if TROUBLE["slow_buy"]:
            time.sleep(TROUBLE["slow_buy"])
        if TROUBLE["buy_fails"]:
            return {"status": "ERROR", "messages": [{"text": "Insufficient funds in your Shippo account."}]}
        n = len([c for c in CALLS if c[1] == "/transactions/"])
        return {"object_id": "tx_{}".format(n), "status": "SUCCESS", "label_url": "https://shippo-delivery.test/label_{}.pdf?Expires=1".format(n),
                "tracking_number": "9400100000000000000{}".format(n), "tracking_url_provider": "https://tools.usps.com/track/{}".format(n)}
    if path.startswith("/transactions/") and method == "GET":
        return {"object_id": path.split("/")[2], "label_url": "https://shippo-delivery.test/fresh_{}.pdf".format(path.split("/")[2])}
    if path == "/refunds/":
        if TROUBLE["refund_fails"]:
            return {"status": "ERROR", "messages": [{"text": "Refund rejected, the shipment was found to be used."}]}
        return {"status": "QUEUED", "transaction": data["transaction"]}
    raise AssertionError("unexpected Shippo call " + path)


farm_shippo.call = standin


def calls(path, method=None):
    return [c for c in CALLS if c[1] == path and (method is None or c[0] == method)]


staff = Client(wsgi.application)
staff.post("/shop/admin/login", data={"password": "playdate"})
staff.post("/shop/admin/visibility", data={"which": "shop", "hidden": "0"})
staff.post("/shop/admin/settings/email", data={"shop_emails": "1", "email_daily_limit": "40"})

# ---------------------------------------------------------------- products know their packed size
r = staff.post("/shop/admin/products/new?kind=item", data={"name": "Embroidered hat", "price": "40", "can_ship": "1", "can_pickup": "1",
                                                          "ship_lb": "0", "ship_oz": "9", "box_l": "10", "box_w": "8", "box_h": "4"})
hat = q("SELECT * FROM shop_products WHERE name='Embroidered hat'")[0]
check("a product saves its packed weight and box", hat["ship_weight_oz"] == 9 and hat["ship_box"] == "10x8x4", (hat["ship_weight_oz"], hat["ship_box"]))
staff.post("/shop/admin/products/new?kind=item", data={"name": "Signed book", "price": "30", "can_ship": "1", "taxable": "on",
                                                       "ship_lb": "2", "ship_oz": "4", "box_l": "12", "box_w": "9", "box_h": "3"})
book = q("SELECT * FROM shop_products WHERE name='Signed book'")[0]
check("pounds and ounces add up", book["ship_weight_oz"] == 36, book["ship_weight_oz"])
r = staff.post("/shop/admin/products/new?kind=item", data={"name": "Half a box", "price": "5", "can_ship": "1", "box_l": "10"})
check("a half-filled box size is refused", "all three box measurements" in body(r) and not q("SELECT id FROM shop_products WHERE name='Half a box'"))
staff.post("/shop/admin/products/new?kind=item", data={"name": "Mystery mug", "price": "15", "can_ship": "1"})
MUG = q("SELECT id FROM shop_products WHERE name='Mystery mug'")[0]["id"]
page = body(staff.get("/shop/admin/products/{}".format(hat["id"])))
check("the product form shows its saved size", 'name="ship_oz"' in page and 'value="9"' in page and 'value="10"' in page)


def buy(email, items, ship=True, how=None, who=None):
    g = who or Client(wsgi.application)
    for pid, n in items:
        g.post("/shop/cart/add", data={"product_id": pid, "qty": n})
    data = {"name": "Pat " + email.split("@")[0], "email": email, "phone": "4175550100", "agree": "on"}
    if how:
        data["how"] = how
    if ship:
        data.update(ship_name="Pat Buyer", ship_line1="12 Elm St", ship_city="Springfield", ship_state="MO", ship_zip="65802")
    r = g.post("/shop/checkout", data=data)
    code = re.search(r"/shop/pay/([A-Z0-9]+)", loc(r)).group(1)
    g.post("/shop/pay/" + code, data={"card": "4242424242424242"})
    return code


def order(code):
    return q("SELECT * FROM shop_orders WHERE code=?", code)[0]


def labels(code):
    return q("SELECT l.* FROM shop_labels l JOIN shop_orders o ON o.id=l.order_id WHERE o.code=? ORDER BY l.id", code)


def tracking_emails(email):
    return q("SELECT * FROM outbox WHERE recipient=? AND subject LIKE '%has shipped%'", email)


# ---------------------------------------------------------------- return address first
c1 = buy("ann@example.com", [(hat["id"], 2)], how="ship")
o = body(staff.get("/shop/admin/orders/" + c1))
check("without a return address, the order page says what to do", "return address" in o and "Print a shipping label" not in o)
s = body(staff.get("/shop/admin/settings"))
check("Settings shows practice labels and the return address form", "Practice labels" in s and 'name="from_street1"' in s
      and "4×6 label printer" in s)
staff.post("/shop/admin/settings/labels", data={"from_name": "Gooseberry Bridge Farm", "from_street1": "100 Farm Rd", "from_city": "Rogersville",
                                                "from_state": "MO", "from_zip": "65742", "from_phone": "4175550199", "label_size": "PDF_4x6"})
check("the return address is saved", '"100 Farm Rd"' in body(staff.get("/shop/admin/settings")) or 'value="100 Farm Rd"' in body(staff.get("/shop/admin/settings")))

# ---------------------------------------------------------------- one tap: prices, buy, print
o = body(staff.get("/shop/admin/orders/" + c1))
check("a shipped order offers Print a shipping label", "Print a shipping label" in o)
CALLS.clear()
lp = body(staff.get("/shop/admin/orders/{}/label".format(c1)))
shp = calls("/shipments/")[0][2] if calls("/shipments/") else {}
check("opening the label page asks Shippo for prices right away", len(calls("/shipments/")) == 1)
parcel = (shp.get("parcels") or [{}])[0]
check("the package is 2 hats' weight in the hat box", parcel.get("weight") == "18" and parcel.get("mass_unit") == "oz"
      and (parcel.get("length"), parcel.get("width"), parcel.get("height")) == ("10", "8", "4") and parcel.get("distance_unit") == "in", parcel)
check("it ships to the buyer's address", shp.get("address_to", {}).get("street1") == "12 Elm St" and shp["address_to"]["zip"] == "65802"
      and shp["address_to"]["name"] == "Pat Buyer" and shp["address_to"]["country"] == "US")
check("from the farm's return address", shp.get("address_from", {}).get("street1") == "100 Farm Rd" and shp["address_from"]["zip"] == "65742")
check("prices are shown cheapest first", lp.find("Ground Advantage") < lp.find("Priority Mail") and "$5.62" in lp and "$9.85" in lp
      and "cheapest" in lp)
check("a price Shippo sent without an amount is left out", "rate_bad" not in lp)
check("practice mode starts with Email them tracking unticked", 'name="tell" value="1" checked' not in lp and "Practice labels" in lp)

CALLS.clear()
r = staff.post("/shop/admin/orders/{}/label/buy".format(c1), data={"rate": "rate_ga_1", "cost": "5.62", "carrier": "USPS",
                                                                     "service": "Ground Advantage", "weight_oz": "18", "box": "10x8x4", "tell": "1"})
tx = calls("/transactions/", "POST")
check("buying sends the chosen price to Shippo as a 4x6 label", len(tx) == 1 and tx[0][2]["rate"] == "rate_ga_1"
      and tx[0][2]["label_file_type"] == "PDF_4x6" and tx[0][2]["async"] is False, tx)
L = labels(c1)
check("the label is saved with its price and tracking", len(L) == 1 and L[0]["cost"] == 5.62 and L[0]["tracking"].startswith("9400")
      and L[0]["status"] == "bought" and L[0]["test"] == 1 and L[0]["service"] == "Ground Advantage", L)
check("the order is marked shipped", order(c1)["fulfilled_at"] and order(c1)["fulfilled_by"] == "label")
te = tracking_emails("ann@example.com")
check("the buyer is emailed the tracking number", len(te) == 1 and L[0]["tracking"] in te[0]["body"] and "tools.usps.com/track" in te[0]["body"], te)
o = body(staff.get(loc(r)))
check("back on the order: Print label, practice tag, tracking link", "Print label" in o and "practice" in o and L[0]["tracking"] in o
      and "Label bought" in o)
r = staff.get("/shop/admin/orders/{}/labels/{}/print".format(c1, L[0]["id"]))
check("Print label opens a fresh label link from Shippo", r.status_code == 302 and loc(r) == "https://shippo-delivery.test/fresh_{}.pdf".format(L[0]["shippo_id"]))
check("...the order no longer offers a first label", "Print a shipping label" not in o and "Buy another label" in o)

made = len(calls("/transactions/", "POST"))
r = staff.post("/shop/admin/orders/{}/label/buy".format(c1), data={"rate": "rate_ga_1", "cost": "5.62"})
check("a second label isn't bought by accident", len(calls("/transactions/", "POST")) == made and len(labels(c1)) == 1)
lp = body(staff.get("/shop/admin/orders/{}/label".format(c1)))
check("buying another (second box) warns first", "already has a label" in lp and 'name="another" value="1"' in lp)

lp_rates = re.findall(r'name="(rate_\d)" value="([^"]+)"', body(staff.get("/shop/admin/orders/{}/label".format(c2 if False else c1))))
cX = buy("ivy@example.com", [(book["id"], 1)])
page = body(staff.get("/shop/admin/orders/{}/label".format(cX)))
picks = dict(re.findall(r'name="(rate_\d)" value="([^"]+)"', page))
check("the label page is one form with a Buy button per price", 'name="pick" value="0"' in page and 'name="pick" value="1"' in page
      and page.count('name="tell"') == 1, list(picks))
staff.post("/shop/admin/orders/{}/label/buy".format(cX), data=dict(picks, pick="1", weight_oz="36", box="12x9x3"))
LX = labels(cX)
rid1, amt1, car1, svc1 = picks["rate_1"].split("|")
check("pressing the second Buy buys that price, recorded right", LX and calls("/transactions/", "POST")[-1][2]["rate"] == rid1
      and LX[0]["cost"] == float(amt1) and LX[0]["service"] == svc1 and LX[0]["weight_oz"] == 36 and LX[0]["box"] == "12x9x3", (picks, LX))

# ---------------------------------------------------------------- void
CALLS.clear()
r = staff.post("/shop/admin/orders/{}/labels/{}/void".format(c1, L[0]["id"]))
check("void asks Shippo for the money back", calls("/refunds/") and calls("/refunds/")[0][2]["transaction"] == L[0]["shippo_id"])
check("the label shows voided and the order is not shipped anymore", labels(c1)[0]["status"] == "voided" and not order(c1)["fulfilled_at"])
o = body(staff.get("/shop/admin/orders/" + c1))
check("...and it offers a new label", "Print a shipping label" in o and "voided" in o)

c2 = buy("bo@example.com", [(book["id"], 1)])
staff.post("/shop/admin/orders/{}/label/buy".format(c2), data={"rate": "rate_x", "cost": "4.10", "carrier": "USPS", "service": "Media Mail", "tell": "1"})
TROUBLE["refund_fails"] = True
L2 = labels(c2)
r = staff.post("/shop/admin/orders/{}/labels/{}/void".format(c2, L2[0]["id"]))
TROUBLE["refund_fails"] = False
check("a refused void leaves the label in place and says why", labels(c2)[0]["status"] == "bought" and "found to be used" in flashes(staff, "/shop/admin/orders/" + c2))

# ---------------------------------------------------------------- trouble
c3 = buy("cy@example.com", [(book["id"], 1)])
TROUBLE["no_rates"] = True
lp = body(staff.get("/shop/admin/orders/{}/label".format(c3)))
TROUBLE["no_rates"] = False
check("no prices: Shippo's reason is shown", "could not be found" in lp and "Buy $" not in lp, lp[lp.find("card"):][:400])
TROUBLE["buy_fails"] = True
r = staff.post("/shop/admin/orders/{}/label/buy".format(c3), data={"rate": "rate_ga_1", "cost": "5.62"})
TROUBLE["buy_fails"] = False
check("a refused purchase saves nothing and says why", not labels(c3) and "Insufficient funds" in flashes(staff, loc(r)) and not order(c3)["fulfilled_at"])
r = staff.post("/shop/admin/orders/{}/label/buy".format(c3), data={"rate": "rate_ga_1", "cost": "5.62"})
check("trying again works (nothing left stuck)", len(labels(c3)) == 1)

c4 = buy("di@example.com", [(book["id"], 1)])
TROUBLE["slow_buy"] = 0.8
made = len(calls("/transactions/", "POST"))


def tap():
    t = Client(wsgi.application)
    t.post("/shop/admin/login", data={"password": "playdate"})
    t.post("/shop/admin/orders/{}/label/buy".format(c4), data={"rate": "rate_ga_1", "cost": "5.62"})


threads = [threading.Thread(target=tap) for _ in range(2)]
for t in threads:
    t.start()
    time.sleep(0.15)
for t in threads:
    t.join()
TROUBLE["slow_buy"] = 0.0
check("a double-tap buys only one label", len(calls("/transactions/", "POST")) == made + 1 and len(labels(c4)) == 1,
      (len(calls("/transactions/", "POST")) - made, len(labels(c4))))

# ---------------------------------------------------------------- missing sizes, mixed orders, pickup, paper
c5 = buy("ed@example.com", [(MUG, 1)])
CALLS.clear()
lp = body(staff.get("/shop/admin/orders/{}/label".format(c5)))
check("a product without a size: no prices yet, and it says which", not calls("/shipments/") and "Mystery mug" in lp and "packed weight" in lp)
lp = body(staff.get("/shop/admin/orders/{}/label?lb=1&oz=2&box=8x8x8".format(c5)))
p5 = calls("/shipments/")[-1][2]["parcels"][0] if calls("/shipments/") else {}
check("typing the weight and box on the page gets prices", p5.get("weight") == "18" and p5.get("length") == "8" and "$5.62" in lp, p5)

c6 = buy("flo@example.com", [(hat["id"], 1)], how="pickup", ship=False)
check("(setup) a pickup order", not order(c6)["ship_line1"])
check("a pickup-only order offers no label", "Print a shipping label" not in body(staff.get("/shop/admin/orders/" + c6)))
staff.post("/shop/admin/products/new?kind=item", data={"name": "Fresh flowers", "price": "12", "can_pickup": "1"})
FLOWERS = q("SELECT id FROM shop_products WHERE name='Fresh flowers'")[0]["id"]
c7 = buy("gus@example.com", [(book["id"], 1), (FLOWERS, 1)])
kinds = sorted(x["fulfillment"] for x in q("SELECT l.fulfillment FROM shop_order_lines l JOIN shop_orders o ON o.id=l.order_id WHERE o.code=?", c7))
staff.post("/shop/admin/orders/{}/label/buy".format(c7), data={"rate": "rate_ga_1", "cost": "5.62"})
check("an order with a pickup item too isn't marked done by the label alone", kinds == ["pickup", "ship"] and len(labels(c7)) == 1
      and not order(c7)["fulfilled_at"], (kinds, labels(c7), order(c7)["fulfilled_at"]))

staff.post("/shop/admin/settings/labels", data={"from_name": "Gooseberry Bridge Farm", "from_street1": "100 Farm Rd", "from_city": "Rogersville",
                                                "from_state": "MO", "from_zip": "65742", "label_size": "PDF"})
c8 = buy("hal@example.com", [(book["id"], 1)])
staff.post("/shop/admin/orders/{}/label/buy".format(c8), data={"rate": "rate_ga_1", "cost": "5.62"})
check("plain paper labels ask Shippo for a letter-size PDF", calls("/transactions/", "POST")[-1][2]["label_file_type"] == "PDF")

check("no email when Email them tracking is unticked", not tracking_emails("hal@example.com"))

guest = Client(wsgi.application)
r = guest.get("/shop/admin/orders/{}/label".format(c8))
check("label pages need a login", r.status_code in (302, 303) and "login" in loc(r))
L8 = labels(c8)
r = guest.get("/shop/admin/orders/{}/labels/{}/print".format(c8, L8[0]["id"]))
check("...so does printing a label", r.status_code in (302, 303) and "shippo-delivery" not in loc(r))
r = guest.post("/shop/admin/orders/{}/label/buy".format(c8), data={"rate": "rate_ga_1"})
check("...and buying one", r.status_code in (302, 303) and len(labels(c8)) == 1)
r = staff.get("/shop/admin/orders/{}/labels/{}/print".format(c1, L8[0]["id"]))
check("a label can't be opened through another order", r.status_code == 404)

os.environ["SHIPPO_API_KEY"] = ""
o = body(staff.get("/shop/admin/orders/" + c3))
check("with no Shippo key: says labels aren't set up", "set up yet" in o and "Print a shipping label" not in o)
r = staff.post("/shop/admin/orders/{}/label/buy".format(c3), data={"rate": "rate_ga_1", "another": "1"})
check("...and won't buy one", len(labels(c3)) == 1)
os.environ["SHIPPO_API_KEY"] = "shippo_live_standin"
lp = body(staff.get("/shop/admin/orders/{}/label?lb=1&box=8x8x8".format(c5)))
check("live key: Email them tracking starts ticked", 'name="tell" value="1" checked' in lp and "Practice labels" not in lp)
os.environ["SHIPPO_API_KEY"] = "shippo_test_standin"

# ---------------------------------------------------------------- shipping profiles: what buyers pay, and which box/service labels use
staff.post("/shop/admin/settings/shipping", data={"shipping_flat": "6", "ship_extra_item": "2", "free_ship_over": "",
                                                 "prof_name_new": "Book", "prof_price_new": "5", "prof_template_new": "", "prof_box_new": "11x8.5x2",
                                                 "prof_service_new": "usps_media_mail"})
staff.post("/shop/admin/settings/shipping", data={"shipping_flat": "6", "ship_extra_item": "2", "free_ship_over": "",
                                                 "prof_id": [str(x["id"]) for x in q("SELECT id FROM shop_ship_profiles WHERE archived=0")],
                                                 **{"prof_name_{}".format(x["id"]): x["name"] for x in q("SELECT * FROM shop_ship_profiles")},
                                                 **{"prof_price_{}".format(x["id"]): str(x["price"]) for x in q("SELECT * FROM shop_ship_profiles")},
                                                 **{"prof_box_{}".format(x["id"]): x["box"] for x in q("SELECT * FROM shop_ship_profiles")},
                                                 **{"prof_service_{}".format(x["id"]): x["service"] for x in q("SELECT * FROM shop_ship_profiles")},
                                                 "prof_name_new": "Medium flat-rate box", "prof_price_new": "10",
                                                 "prof_template_new": "USPS_MediumFlatRateBox1"})
PR = {x["name"]: x for x in q("SELECT * FROM shop_ship_profiles WHERE archived=0")}
check("shipping profiles are saved", set(PR) == {"Book", "Medium flat-rate box"} and PR["Book"]["price"] == 5 and PR["Book"]["box"] == "11x8.5x2"
      and PR["Book"]["service"] == "usps_media_mail" and PR["Medium flat-rate box"]["template"] == "USPS_MediumFlatRateBox1", PR)
r = staff.post("/shop/admin/settings/shipping", data={"shipping_flat": "6", "ship_extra_item": "2", "prof_name_new": "No price", "prof_price_new": ""})
check("a profile without a price isn't saved, and it says so", "No price" not in [x["name"] for x in q("SELECT name FROM shop_ship_profiles")]
      and "needs a name and a price" in flashes(staff, loc(r)))
s = body(staff.get("/shop/admin/settings"))
check("Settings lists the profiles, the flat-rate boxes and Media Mail", "Medium flat-rate box" in s and "USPS Medium Flat Rate Box (opens on top)" in s
      and "USPS Media Mail" in s and 'name="free_ship_over"' in s)
staff.post("/shop/admin/products/new?kind=item", data={"name": "Cookbook", "price": "25", "can_ship": "1", "ship_lb": "1", "ship_oz": "8",
                                                       "ship_profile_id": str(PR["Book"]["id"])})
staff.post("/shop/admin/products/new?kind=item", data={"name": "Jam sampler", "price": "30", "can_ship": "1", "ship_lb": "3",
                                                       "ship_profile_id": str(PR["Medium flat-rate box"]["id"])})
CB = q("SELECT * FROM shop_products WHERE name='Cookbook'")[0]
JS = q("SELECT * FROM shop_products WHERE name='Jam sampler'")[0]
check("a product keeps its shipping profile", CB["ship_profile_id"] == PR["Book"]["id"])
check("the product form offers the profiles", "Medium flat-rate box ($10)" in body(staff.get("/shop/admin/products/{}".format(CB["id"]))))
check("the product page shows its own shipping charge", "$5 shipping" in body(Client(wsgi.application).get("/shop/p/{}".format(CB["id"]))))


def shipping_of(items):
    code = buy("ship{}@example.com".format(len(CALLS)), items)
    return order(code)["shipping"], code


sh, _ = shipping_of([(CB["id"], 1)])
check("one book: the Book charge", sh == 5, sh)
sh, _ = shipping_of([(CB["id"], 2)])
check("two books: $5 + $2 for the second", sh == 7, sh)
sh, _ = shipping_of([(CB["id"], 1), (JS["id"], 1)])
check("book + flat-rate box: the higher charge + $2", sh == 12, sh)
sh, _ = shipping_of([(CB["id"], 1), (MUG, 1)])
check("book + a product with no profile: standard $6 is higher, + $2", sh == 8, sh)
staff.post("/shop/admin/settings/shipping", data={"shipping_flat": "6", "ship_extra_item": "2", "free_ship_over": "50"})
sh, _ = shipping_of([(JS["id"], 2)])
check("free shipping once the shipped items reach $50", sh == 0, sh)
sh, _ = shipping_of([(CB["id"], 1)])
check("...but not below it", sh == 5, sh)
check("the product page mentions free shipping", "Free shipping on $50 or more" in body(Client(wsgi.application).get("/shop/p/{}".format(CB["id"]))))
staff.post("/shop/admin/settings/shipping", data={"shipping_flat": "6", "ship_extra_item": "2", "free_ship_over": ""})

_, cjam = shipping_of([(JS["id"], 1)])
CALLS.clear()
lp = body(staff.get("/shop/admin/orders/{}/label".format(cjam)))
pj = calls("/shipments/")[0][2]["parcels"][0] if calls("/shipments/") else {}
check("a flat-rate profile's label uses the USPS flat-rate box", pj.get("template") == "USPS_MediumFlatRateBox1" and pj.get("weight") == "48"
      and "length" not in pj, pj)
check("...and the label page says which box", "USPS Medium Flat Rate Box (opens on top)" in lp)
_, cbk = shipping_of([(CB["id"], 1)])
CALLS.clear()
lp = body(staff.get("/shop/admin/orders/{}/label".format(cbk)))
pb = calls("/shipments/")[0][2]["parcels"][0] if calls("/shipments/") else {}
check("a profile's own box is used when the product has none", (pb.get("length"), pb.get("width"), pb.get("height")) == ("11", "8.5", "2"), pb)
first = re.search(r'name="rate_0" value="([^"]+)"', lp)
check("the profile's preferred service (Media Mail) comes first, marked suggested", first and "Media Mail" in first.group(1)
      and "suggested by the product" in lp, first and first.group(1))
_, cmix = shipping_of([(CB["id"], 1), (JS["id"], 1)])
lp = body(staff.get("/shop/admin/orders/{}/label".format(cmix)))
check("a book shipped with something else is not steered to Media Mail", "suggested by the product" not in lp
      and "Media Mail" not in re.search(r'name="rate_0" value="([^"]+)"', lp).group(1))
_, c2bk = shipping_of([(CB["id"], 2)])
lp = body(staff.get("/shop/admin/orders/{}/label".format(c2bk)))
check("two books together still suggest Media Mail", "Media Mail" in re.search(r'name="rate_0" value="([^"]+)"', lp).group(1))
CALLS.clear()
staff.get("/shop/admin/orders/{}/label?template=USPS_SmallFlatRateBox&lb=1&oz=8".format(cbk))
check("staff can switch the box to a flat-rate box on the label page", calls("/shipments/")[0][2]["parcels"][0].get("template") == "USPS_SmallFlatRateBox")
staff.post("/shop/admin/settings/shipping", data={"shipping_flat": "6", "ship_extra_item": "2", "prof_id": [str(PR["Book"]["id"])],
                                                 "prof_name_{}".format(PR["Book"]["id"]): "Book", "prof_price_{}".format(PR["Book"]["id"]): "5",
                                                 "prof_remove_{}".format(PR["Book"]["id"]): "1"})
check("removing a profile puts its products back on standard shipping", q("SELECT ship_profile_id FROM shop_products WHERE id=?", CB["id"])[0]["ship_profile_id"] is None
      and "$6 shipping" in body(Client(wsgi.application).get("/shop/p/{}".format(CB["id"]))))

# ---------------------------------------------------------------- the real connection code, against a pretend Shippo
farm_shippo.call = real_call
SEEN = []


class FakeShippo(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        SEEN.append((self.path, self.headers.get("Authorization"), self.headers.get("Content-Type"), json.loads(self.rfile.read(n) or b"{}")))
        if self.path == "/transactions/":
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"rate": ["Rate not found."]}).encode())
            return
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"object_id": "shp_real", "rates": [{"object_id": "r1", "amount": "6.10", "provider": "USPS",
                                                                          "servicelevel": {"name": "Ground Advantage"}}]}).encode())


srv = http.server.HTTPServer(("127.0.0.1", 0), FakeShippo)
threading.Thread(target=srv.serve_forever, daemon=True).start()
farm_shippo.BASE = "http://127.0.0.1:{}".format(srv.server_port)
sid, got = farm_shippo.rates({"street1": "a"}, {"street1": "b"}, 9, (10, 8, 4))
check("real code: sends Shippo's login header and JSON", SEEN and SEEN[0][1] == "ShippoToken shippo_test_standin" and SEEN[0][2] == "application/json"
      and SEEN[0][3]["parcels"][0]["weight"] == "9", SEEN[:1])
check("real code: reads Shippo's prices", sid == "shp_real" and got[0]["amount"] == 6.10 and got[0]["service"] == "Ground Advantage")
try:
    farm_shippo.buy("nope", "PDF_4x6", "X")
    check("real code: a refusal from Shippo is reported", False)
except farm_shippo.ShippoError as e:
    check("real code: a refusal from Shippo is reported", "400" in str(e) and "Rate not found" in str(e), str(e))
srv.shutdown()
srv.server_close()
farm_shippo.BASE = "http://127.0.0.1:9"  # nothing listens here
try:
    farm_shippo.label_link("tx")
    check("real code: Shippo unreachable is reported, not a crash", False)
except farm_shippo.ShippoError as e:
    check("real code: Shippo unreachable is reported, not a crash", "reach Shippo" in str(e), str(e))
check("mode follows the key", (os.environ.update(SHIPPO_API_KEY="shippo_live_x") or farm_shippo.mode()) == "live"
      and (os.environ.update(SHIPPO_API_KEY="") or farm_shippo.mode()) == "off")

summary = {"database": "postgres" if os.environ.get("DATABASE_URL") else "sqlite", "passed": len(results) - len(failures),
           "total": len(results), "failures": failures}
print(json.dumps(summary, indent=1))
sys.exit(1 if failures else 0)
