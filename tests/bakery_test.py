"""Bakery check: menu, photos, stock limits, card and app payments, Mollie's admin.

    python tests/bakery_test.py            # data in a throwaway folder (like the Mac)
    DATABASE_URL=... python tests/bakery_test.py      # data in Postgres (like Render)
    python tests/bakery_test.py --r2       # photos in a stand-in Cloudflare R2 bucket
"""
import base64
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
os.environ["FARM_TEST_MODE"] = "1"  # allows the test passwords
work = tempfile.mkdtemp(prefix="bakery-test-")
os.environ["FARM_DATA_DIR"] = work
os.environ["PLAYDATE_DB"] = os.path.join(work, "playdates.db")
os.environ["BAKERY_PASSWORD"] = "pie"
os.environ["BAKERY_SECRET_KEY"] = "test"
os.environ["MAIL_PROVIDER"] = "memory"  # capture customer emails instead of sending them
if os.environ.get("DATABASE_URL"):
    os.environ["FARM_DB_NAME"] = "bakery_" + os.path.basename(work).replace("-", "_").lower()
use_r2 = "--r2" in sys.argv
mock = None
if use_r2:
    # A local stand-in for Cloudflare R2 (same S3-style interface R2 uses).
    from moto.server import ThreadedMotoServer
    import boto3
    mock = ThreadedMotoServer(port=5099, verbose=False)
    mock.start()
    os.environ.update(R2_BUCKET="farm-apps-photos", R2_ENDPOINT="http://127.0.0.1:5099",
                      R2_ACCESS_KEY_ID="test", R2_SECRET_ACCESS_KEY="test", NO_PROXY="127.0.0.1,localhost")
    boto3.client("s3", endpoint_url="http://127.0.0.1:5099", region_name="us-east-1", aws_access_key_id="test",
                 aws_secret_access_key="test").create_bucket(Bucket="farm-apps-photos")

sys.path.insert(0, ROOT)
import wsgi  # noqa: E402
from werkzeug.test import Client  # noqa: E402

c = Client(wsgi.application)
results, failures = [], []


def check(name, ok, detail=""):
    results.append({"step": name, "ok": bool(ok)})
    if not ok:
        failures.append("{}: {}".format(name, detail))


def api(method, path, data=None, client=c):
    r = client.open("/bakery/" + path.lstrip("/"), method=method, json=data)
    try:
        return r.status_code, json.loads(r.get_data(as_text=True) or "{}")
    except ValueError:
        return r.status_code, r.get_data(as_text=True)


def buyer(**kw):
    d = {"name": "Test Buyer", "email": "buyer@example.com", "phone": "417-555-0100", "pickupId": "farm", "bakeId": T}
    d.update(kw)
    return d


def admin():
    return api("GET", "api/admin/state")[1]


def act(**a):
    return api("POST", "api/admin/action", a)


def bake_of(shop_or_state, bid):
    return next((b for b in shop_or_state["bakes"] if b["id"] == bid), None)


T = ""
code, shop = api("GET", "api/shop")
check("shop loads with Mollie's first preorder", code == 200 and len(shop["bakes"]) <= 1, code)
check("app payments offered", shop["appPay"] and shop["appPay"]["apps"] == ["Venmo", "Cash App", "PayPal"])

code, _ = api("GET", "api/admin/state")
check("admin needs login", code == 401)
code, _ = api("POST", "api/admin/login", {"password": "nope"})
check("wrong password refused", code == 401)
code, _ = api("POST", "api/admin/login", {"password": "pie"})
check("Mollie signs in", code == 200)
code, st = api("GET", "api/admin/state")
check("build number in Mollie's admin", bool(st.get("build")), st.get("build"))
check("admin state", code == 200 and st["settings"]["shopName"] == "Mollie's Farmhouse Bakery")
T = st["bakes"][0]["id"]
check("a new store starts with one Bake holding the whole menu", len(st["bakes"]) == 1 and st["bakes"][0]["name"] == "Thanksgiving 2026"
      and len(st["bakes"][0]["menu"]) == 6 and [p["id"] for p in st["bakes"][0]["pickups"]] == ["farm", "fmo"], st["bakes"])

# The starting Bake carries Mollie's real Thanksgiving deadline; clear it so this check doesn't depend on today's date.
code, res = act(action="saveBake", bake={"id": T, "name": "Thanksgiving 2026", "deadline": ""})
check("Bake saved", code == 200 and res.get("id") == T, res)
_, shop = api("GET", "api/shop")
check("ordering open", bake_of(shop, T)["ordering"]["open"] and len(bake_of(shop, T)["items"]) == 6, bake_of(shop, T))
jpeg = base64.b64encode(b"\xff\xd8\xff\xe0fakejpegbytes\xff\xd9").decode()
code, res = act(action="saveItem", item={"id": "pump", "name": "Pumpkin Pie", "description": "Whole 9-inch pie.",
                                          "price": "35.00", "image": "data:image/jpeg;base64," + jpeg})
check("item saved with photo", code == 200, res)
code, res = act(action="setBakeItem", bakeId=T, itemId="pump", limit="2")
check("how many she can make, set on the Bake", code == 200, res)
_, shop = api("GET", "api/shop")
pump = next(i for i in bake_of(shop, T)["items"] if i["id"] == "pump")
check("photo path stored", pump["image"].startswith("uploads/"), pump["image"])
r = c.get("/bakery/" + pump["image"])
check("photo served", r.status_code == 200 and r.data.startswith(b"\xff\xd8"), r.status_code)
check("stock shows 2 left", pump["remaining"] == 2)

code, res = api("POST", "api/checkout", buyer(cart=[{"itemId": "pump", "qty": 1}, {"itemId": "cinn", "qty": 2}], pickupTime="around 10"))
check("card order (demo) goes through", code == 200 and "?order=" in res.get("url", ""), res)
card_id = res.get("orderId")
code, o = api("GET", "api/orders/" + card_id)
check("card order paid", o.get("status") == "paid" and o["total"] == 3500 + 2 * 1400, o)
check("order knows its Bake", o.get("bakeName") == "Thanksgiving 2026", o.get("bakeName"))

code, res = api("POST", "api/checkout", buyer(cart=[{"itemId": "pump", "qty": 1}], payMethod="app", pickupId="fmo"))
check("app order reserved", code == 200, res)
app_id = res.get("orderId")
code, o = api("GET", "api/orders/" + app_id)
check("app order waiting, 3% off", o.get("status") == "awaiting" and o["discount"] == 105 and o["total"] == 3395, o)
check("pay buttons for each app", [a["app"] for a in o.get("appLinks", [])] == ["Venmo", "Cash App", "PayPal"])

code, res = api("POST", "api/checkout", buyer(cart=[{"itemId": "pump", "qty": 1}]))
check("sold out once limit reached", code == 400 and "sold out" in res.get("error", ""), res)
code, res = api("POST", "api/checkout", buyer(cart=[{"itemId": "cinn", "qty": 1}], email="bad"))
check("bad email refused", code == 400 and "email" in res.get("error", ""))

code, _ = act(action="markPaid", id=app_id)
code, o = api("GET", "api/orders/" + app_id)
check("Mollie marks app order paid", o.get("status") == "paid")
code, _ = act(action="setPickedUp", id=card_id, pickedUp=True)
r = c.get("/bakery/api/admin/orders.csv?bake=" + T)
lines = r.get_data(as_text=True).strip().splitlines()
check("spreadsheet has both orders", r.status_code == 200 and len(lines) == 3, lines[:1])

code, _ = act(action="savePickup", bakeId=T, pickup={"label": "Church lot", "details": "Sun after service", "capacity": "1", "active": True})
_, shop = api("GET", "api/shop")
check("new pickup offered", any(p["label"] == "Church lot" for p in bake_of(shop, T)["pickups"]))
code, _ = act(action="saveBake", bake={"id": T, "name": "Thanksgiving 2026", "deadline": "2020-01-01T00:00:00.000Z"})
_, shop = api("GET", "api/shop")
check("a Bake past its deadline leaves the shop", bake_of(shop, T) is None, shop["bakes"])
code, res = api("POST", "api/checkout", buyer(cart=[{"itemId": "cinn", "qty": 1}]))
check("...and its orders are refused", code == 400 and "deadline" in res.get("error", ""), res)
act(action="saveBake", bake={"id": T, "name": "Thanksgiving 2026", "deadline": ""})
code, _ = act(action="setBakeOpen", bakeId=T, open=False)
_, shop = api("GET", "api/shop")
code, res = api("POST", "api/checkout", buyer(cart=[{"itemId": "cinn", "qty": 1}]))
check("stop button closes orders", bake_of(shop, T) is None and code == 400, res)

# ---- customer emails
sent = wsgi.bakery.mailer.sent
subjects = [m["subject"] for m in sent]
check("card order gets one 'confirmed' email", sum(1 for x in subjects if x.startswith("Order #1001 is confirmed")) == 1, subjects)
check("app order gets a 'reserved' email with how to pay", any(x.startswith("Order #1002 is reserved") for x in subjects), subjects)
rsv = next((m for m in sent if m["subject"].startswith("Order #1002 is reserved")), {"text": ""})
check("reserved email has the payment buttons' links and note", "venmo.com" in rsv["text"] and "Order #1002" in rsv["text"] and "$33.95" in rsv["text"], rsv["text"][:300])
check("app order gets 'confirmed' once Mollie marks it paid", sum(1 for x in subjects if x.startswith("Order #1002 is confirmed")) == 1, subjects)
act(action="markPaid", id=app_id)
check("no duplicate email if marked paid twice", sum(1 for m in wsgi.bakery.mailer.sent if m["subject"].startswith("Order #1002 is confirmed")) == 1)
check("emails go to the customer", all(m["to"] == "buyer@example.com" for m in sent))

# ---- pickup windows (Mollie sets dated time slots; customers pick one)
act(action="setBakeOpen", bakeId=T, open=True)
code, res = act(action="savePickup", bakeId=T, pickup={"id": "farm", "label": "Gooseberry Bridge Farm", "details": "177 Elm Grove Rd", "active": True,
                                                         "windows": [{"date": "2030-11-25", "start": "13:00", "end": "11:00"}]})
check("a window that ends before it starts is refused", code == 400 and "before it starts" in str(res), res)
wins = [{"date": "2030-11-25", "start": "11:00", "end": "13:00"}, {"date": "2030-11-25", "start": "9:00", "end": "11:00", "capacity": "1"},
        {"date": "2020-01-01", "start": "09:00", "end": "11:00"}, {"date": "", "start": "", "end": ""}]
code, _ = act(action="savePickup", bakeId=T, pickup={"id": "farm", "label": "Gooseberry Bridge Farm", "details": "177 Elm Grove Rd", "active": True, "windows": wins})
saved = next(p for p in bake_of(admin(), T)["pickups"] if p["id"] == "farm")["windows"]
check("windows saved in time order, blank row ignored", code == 200 and [w["start"] for w in saved] == ["09:00", "09:00", "11:00"] and saved[0]["date"] == "2020-01-01", saved)
past_id, early_id, late_id = saved[0]["id"], saved[1]["id"], saved[2]["id"]
_, shop = api("GET", "api/shop")
farm = next(p for p in bake_of(shop, T)["pickups"] if p["id"] == "farm")
check("customers see future windows only, labeled plainly", [w["label"] for w in farm["windows"]] == ["Mon, Nov 25, 9–11am", "Mon, Nov 25, 11am–1pm"], farm["windows"])
check("customers see the pickup day on the preorder", bake_of(shop, T)["pickupDays"] == ["Mon, Nov 25"], bake_of(shop, T)["pickupDays"])
code, res = api("POST", "api/checkout", buyer(cart=[{"itemId": "dinr", "qty": 1}]))
check("a place with windows needs a time picked", code == 400 and "pickup time" in str(res), res)
code, res = api("POST", "api/checkout", buyer(cart=[{"itemId": "dinr", "qty": 1}], pickupWindowId=past_id))
check("a past window is refused", code == 400 and "passed" in str(res), res)
code, res = api("POST", "api/checkout", buyer(cart=[{"itemId": "dinr", "qty": 1}], pickupWindowId=early_id, pickupTime="5pm"))
win_order = res.get("orderId") if isinstance(res, dict) else None
_, o = api("GET", "api/orders/{}".format(win_order))
check("order records the window", code == 200 and o.get("pickupWhen") == "Mon, Nov 25, 9–11am" and not o.get("pickupTime"), o)
code, res = api("POST", "api/checkout", buyer(cart=[{"itemId": "dinr", "qty": 1}], pickupWindowId=early_id))
check("a window at its max is refused", code == 400 and "filled up" in str(res), res)
_, shop = api("GET", "api/shop")
farm = next(p for p in bake_of(shop, T)["pickups"] if p["id"] == "farm")
check("a full window shows as full", farm["windows"][0]["full"] and not farm["windows"][1]["full"], farm["windows"])
conf = next((m for m in wsgi.bakery.mailer.sent if m["subject"].startswith("Order #{} is confirmed".format(o.get("number")))), {"text": ""})
check("confirmation email names the window", "Pickup: Mon, Nov 25, 9–11am\n  Gooseberry Bridge Farm" in conf["text"], conf["text"][-300:])
code, res = api("POST", "api/checkout", buyer(cart=[{"itemId": "dinr", "qty": 1}], pickupId="fmo", pickupTime="around 4"))
_, o2 = api("GET", "api/orders/{}".format(res.get("orderId") if isinstance(res, dict) else ""))
check("a place without windows still works as before", code == 200 and o2.get("pickupTime") == "around 4" and not o2.get("pickupWhen"), o2)
act(action="savePickup", bakeId=T, pickup={"id": "farm", "label": "Gooseberry Bridge Farm", "details": "177 Elm Grove Rd", "active": True, "windows": saved})
check("editing keeps each window's identity", [w["id"] for w in next(p for p in bake_of(admin(), T)["pickups"] if p["id"] == "farm")["windows"]] == [past_id, early_id, late_id])
r = c.get("/bakery/api/admin/orders.csv?bake=" + T)
check("spreadsheet shows the window", "Mon, Nov 25, 9–11am" in r.get_data(as_text=True))

# ---- low-stock badge setting
act(action="saveSettings", settings={"lowStockAt": "3"})
_, shop = api("GET", "api/shop")
check("Mollie can set when 'Only X left' shows", shop["settings"].get("lowStockAt") == 3, shop["settings"].get("lowStockAt"))

# ---- full descriptions and allergen tags
code, _ = act(action="saveItem", item={"id": "choc", "name": "Chocolate Pie", "description": "Whole 9-inch pie.", "price": "35",
                                        "details": "Decadent and rich.\nMost people don't believe it's dairy-free!", "contains": "Contains: coconut"})
_, shop = api("GET", "api/shop")
choc = next(i for i in bake_of(shop, T)["items"] if i["id"] == "choc")
check("item full description saved with its paragraphs", code == 200 and choc["details"].startswith("Decadent and rich.\nMost"), choc)
check("allergen tag saved without a doubled 'Contains'", choc["contains"] == "coconut", choc.get("contains"))

# ---- several Bakes at once (Nov 23, 2026): weekly market preorders beside a holiday one
code, res = act(action="saveBake", bake={"name": "", "open": True})
check("a Bake needs a name", code == 400, res)
code, res = act(action="saveBake", bake={"name": "Market – Sat, Dec 5", "note": "Pickup at the Saturday market.", "open": True,
                                         "opensAt": "2030-01-02T00:00:00Z", "deadline": "2030-01-01T00:00:00Z"})
check("orders can't close before they open", code == 400 and "before they open" in str(res), res)
code, res = act(action="saveBake", bake={"name": "Market – Sat, Dec 5", "note": "Pickup at the Saturday market.", "open": True})
M = res.get("id", "")
check("Mollie starts a second Bake", code == 200 and M and M != T, res)
act(action="setBakeItem", bakeId=M, itemId="cinn", on=True, limit="3")
act(action="setBakeItem", bakeId=M, itemId="dinr", on=True)
act(action="setBakeItem", bakeId=T, itemId="cinn", limit="5")
act(action="savePickup", bakeId=M, pickup={"label": "Farmers Market of the Ozarks", "details": "Springfield", "active": True,
                                         "windows": [{"date": "2030-12-07", "start": "08:00", "end": "12:00"}]})
_, shop = api("GET", "api/shop")
check("customers see both preorders", {b["id"] for b in shop["bakes"]} == {T, M}, [b["name"] for b in shop["bakes"]])
check("each has its own menu", [i["id"] for i in bake_of(shop, M)["items"]] == ["cinn", "dinr"] and len(bake_of(shop, T)["items"]) == 6)
check("...its own note", bake_of(shop, M)["note"] == "Pickup at the Saturday market.")
check("...and its own pickups", [p["label"] for p in bake_of(shop, M)["pickups"]] == ["Farmers Market of the Ozarks"])
check("the preorder with the sooner pickup is listed first", [b["id"] for b in shop["bakes"]] == [T, M], [b["name"] for b in shop["bakes"]])
check("customers never see Mollie's menus or limits lists", "limits" not in json.dumps(shop) and '"menu"' not in json.dumps(shop))
mwin = bake_of(shop, M)["pickups"][0]
code, res = api("POST", "api/checkout", buyer(bakeId=M, pickupId=mwin["id"], pickupWindowId=mwin["windows"][0]["id"], cart=[{"itemId": "cinn", "qty": 2}]))
check("an order for the market preorder", code == 200, res)
m_order = res.get("orderId")
_, shop = api("GET", "api/shop")
check("its stock counts only its own orders", next(i for i in bake_of(shop, M)["items"] if i["id"] == "cinn")["remaining"] == 1
      and next(i for i in bake_of(shop, T)["items"] if i["id"] == "cinn")["remaining"] == 5 - 2,
      [(b["name"], [(i["id"], i["remaining"]) for i in b["items"]]) for b in shop["bakes"]])
code, res = api("POST", "api/checkout", buyer(bakeId=M, pickupId=mwin["id"], pickupWindowId=mwin["windows"][0]["id"], cart=[{"itemId": "pump", "qty": 1}]))
check("an item not on that preorder's menu is refused", code == 400 and "no longer available" in str(res), res)
code, res = api("POST", "api/checkout", buyer(bakeId=M, pickupId="farm", cart=[{"itemId": "cinn", "qty": 1}]))
check("another preorder's pickup is refused", code == 400 and "pickup option" in str(res), res)
code, res = api("POST", "api/checkout", buyer(bakeId="", cart=[{"itemId": "cinn", "qty": 1}]))
check("with two open, an order must say which", code == 400 and "isn't available" in str(res), res)
_, o = api("GET", "api/orders/" + m_order)
check("the order is the market's", o.get("bakeName") == "Market – Sat, Dec 5")
r = c.get("/bakery/api/admin/orders.csv?bake=" + M)
rows = r.get_data(as_text=True).strip().splitlines()
check("each Bake's spreadsheet has only its orders", len(rows) == 2 and "Market" in r.headers.get("Content-Disposition", ""), rows)
st = admin()
check("Mollie sees each Bake's status, stock and orders", bake_of(st, M)["phase"] == "open" and bake_of(st, M)["stock"]["cinn"] == 1
      and bake_of(st, M)["orderCount"] == 1, bake_of(st, M))
act(action="saveBake", bake={"id": M, "name": "Market – Saturday, Dec 5"})
check("renaming a Bake renames its orders", api("GET", "api/orders/" + m_order)[1].get("bakeName") == "Market – Saturday, Dec 5")

code, res = act(action="saveBake", bake={"name": "Christmas 2030", "open": True, "opensAt": "2030-12-01T06:00:00Z", "deadline": "2030-12-20T06:00:00Z"})
X = res.get("id")
act(action="setBakeItem", bakeId=X, itemId="choc", on=True)
_, shop = api("GET", "api/shop")
xb = bake_of(shop, X)
check("a Bake that opens later shows its menu but isn't taking orders", xb and not xb["ordering"]["open"] and xb["ordering"].get("opensAt"), xb and xb["ordering"])
code, res = api("POST", "api/checkout", buyer(bakeId=X, cart=[{"itemId": "choc", "qty": 1}]))
check("...and its orders wait", code == 400 and "aren't open yet" in str(res), res)
check("open preorders are listed before ones opening later", [b["id"] for b in shop["bakes"]][-1] == X)

code, res = act(action="copyBake", bakeId=M, days=7)
C = res.get("id")
cb, mb = bake_of(admin(), C), bake_of(admin(), M)
check("copying a Bake keeps its menu, stock and pickups", cb and cb["menu"] == mb["menu"] and cb["limits"] == mb["limits"]
      and [p["label"] for p in cb["pickups"]] == [p["label"] for p in mb["pickups"]], cb)
check("...moves its pickup dates a week later, with new window ids",
      cb["pickups"][0]["windows"][0]["date"] == "2030-12-14" and cb["pickups"][0]["windows"][0]["id"] != mb["pickups"][0]["windows"][0]["id"], cb["pickups"])
check("...starts switched off with '(copy)' on its name", not cb["open"] and cb["name"].endswith("(copy)") and bake_of(api("GET", "api/shop")[1], C) is None)
check("...and has no orders", cb["orderCount"] == 0)
code, res = act(action="deleteBake", bakeId=M)
check("a Bake with orders can't be deleted", code == 400 and "Switch it off" in str(res), res)
code, _ = act(action="deleteBake", bakeId=C)
check("an empty Bake can be deleted", code == 200 and bake_of(admin(), C) is None)
act(action="saveItem", item={"name": "Gingerbread", "price": "12"})
gid = next(i["id"] for i in admin()["items"] if i["name"] == "Gingerbread")
act(action="setBakeItem", bakeId=M, itemId=gid, on=True, limit="4")
act(action="deleteItem", id=gid)
check("deleting an item takes it off every Bake", all(gid not in b["menu"] and gid not in b["limits"] for b in admin()["bakes"]))
act(action="setBakeItem", bakeId=M, itemId="dinr", on=False)
check("unticking takes an item off one Bake only", "dinr" not in bake_of(admin(), M)["menu"] and "dinr" in bake_of(admin(), T)["menu"])
act(action="setBakeItem", bakeId=M, itemId="cinn", limit="")
check("a blank 'How many' means no limit", "cinn" not in bake_of(admin(), M)["limits"])

# A page loaded before Bakes existed sends no Bake: fine when exactly one is taking orders.
act(action="setBakeOpen", bakeId=M, open=False)
act(action="setBakeOpen", bakeId=X, open=False)
code, res = api("POST", "api/checkout", buyer(bakeId=None, pickupId="fmo", cart=[{"itemId": "cinn", "qty": 1}]))
check("an order from an old page goes to the one open Bake", code == 200 and api("GET", "api/orders/" + res.get("orderId", ""))[1].get("bakeName") == "Thanksgiving 2026", res)

# ---- moving Mollie's real data (one sale at a time) to Bakes
import copy  # noqa: E402
from bakery import store as bakery_store  # noqa: E402
legacy = bakery_store.legacy_default_state()
legacy["settings"]["deadline"] = "2026-11-21T23:59:00-06:00"
legacy["settings"]["saleMenus"] = {"Thanksgiving 2026": ["pump"], "Christmas 2026": ["choc", "chcs", "gone"]}
for i in legacy["items"]:
    i["active"] = i["id"] in ("pump", "cinn")
next(i for i in legacy["items"] if i["id"] == "pump")["limit"] = 10
next(p for p in legacy["pickups"] if p["id"] == "farm")["windows"] = [
    {"id": "w1", "date": "2030-11-25", "start": "09:00", "end": "11:00", "capacity": 1, "active": True}]
legacy["orders"] = [
    {"id": "o0", "number": 1000, "season": "Test 2025", "status": "paid", "createdAt": "2025-11-19T00:00:00.000Z",
     "pickupId": "farm", "pickupWindowId": "w1", "pickupLabel": "Gooseberry Bridge Farm", "lines": [{"itemId": "cinn", "name": "Cinnamon", "price": 1400, "qty": 1}],
     "customer": {"name": "C", "email": "c@example.com", "phone": "4175550100"}, "total": 1400},
    {"id": "o1", "number": 1001, "season": "Thanksgiving 2026", "status": "paid", "createdAt": "2026-11-20T00:00:00.000Z",
     "pickupId": "farm", "pickupLabel": "Gooseberry Bridge Farm", "lines": [{"itemId": "pump", "name": "Pumpkin Pie", "price": 3500, "qty": 3}],
     "customer": {"name": "A", "email": "a@example.com", "phone": "4175550100"}, "total": 10500},
    {"id": "o2", "number": 1002, "season": "Test 2025", "status": "paid", "createdAt": "2025-11-20T00:00:00.000Z",
     "pickupId": "farm", "pickupLabel": "Gooseberry Bridge Farm", "lines": [{"itemId": "cinn", "name": "Cinnamon", "price": 1400, "qty": 1}],
     "customer": {"name": "B", "email": "b@example.com", "phone": "4175550100"}, "total": 1400},
]
before = copy.deepcopy(legacy)
moved = bakery_store.normalize(copy.deepcopy(legacy))
names = [b["name"] for b in moved["bakes"]]
now_bake = moved["bakes"][0]
check("(moving data) the current sale becomes the first Bake", names[0] == "Thanksgiving 2026" and set(names) == {"Thanksgiving 2026", "Test 2025", "Christmas 2026"}, names)
check("(moving data) it keeps its menu, stock limit, deadline, on/off and welcome message",
      now_bake["menu"] == ["cinn", "pump"] and now_bake["limits"] == {"pump": 10} and now_bake["deadline"] == before["settings"]["deadline"]
      and now_bake["open"] is True and now_bake["note"] == before["settings"]["intro"], now_bake)
check("(moving data) it keeps its pickups", [p["id"] for p in now_bake["pickups"]] == ["farm", "fmo"])
check("(moving data) stock left is the same as before", bakery_store.remaining(moved, now_bake, "pump", 0) == 10 - 3)
check("(moving data) every order belongs to its sale's Bake",
      [(o["number"], o["bakeName"]) for o in moved["orders"]] == [(1000, "Test 2025"), (1001, "Thanksgiving 2026"), (1002, "Test 2025")]
      and all(bakery_store.find_bake(moved, o["bakeId"])["name"] == o["bakeName"] for o in moved["orders"]))
check("(moving data) an older sale's orders don't fill the current Bake's pickup times",
      bakery_store.window_taken(moved, now_bake, "w1", 0) == 0 and bakery_store.pickup_taken(moved, now_bake, "farm", 0) == 1)
xm = next(b for b in moved["bakes"] if b["name"] == "Christmas 2026")
check("(moving data) a saved sale menu becomes a switched-off Bake (items that are gone dropped)", xm["menu"] == ["choc", "chcs"] and not xm["open"], xm)
check("(moving data) old one-sale settings are gone, shop settings stay",
      not any(k in moved["settings"] for k in ("season", "deadline", "orderingOpen", "saleMenus")) and moved["settings"]["venmo"] == before["settings"]["venmo"]
      and moved["settings"]["intro"] == "" and "pickups" not in moved and not any("active" in i or "limit" in i for i in moved["items"]))
again = bakery_store.normalize(copy.deepcopy(moved))
check("(moving data) running it again changes nothing", again == moved)
check("(moving data) the same old data always gets the same Bake ids",
      [b["id"] for b in bakery_store.normalize(copy.deepcopy(legacy))["bakes"]] == [b["id"] for b in moved["bakes"]])

# The real path: old data in the database; Mollie's page reads it, then she changes something on that Bake.
sys.path.insert(0, os.path.join(ROOT, "playdates"))
import dbcompat  # noqa: E402
con = dbcompat.connect(os.path.join(work, "bakery.db"), isolation_level=None)
con.execute("UPDATE bakery_store SET data = ? WHERE id = 1", (json.dumps(legacy),))
con.close()
st = admin()
tid = st["bakes"][0]["id"]
code, res = act(action="setBakeItem", bakeId=tid, itemId="dinr", on=True)
check("(moving data) a change made from a page read before the move was saved lands on the right Bake",
      code == 200 and "dinr" in bake_of(admin(), tid)["menu"], res)
_, shop = api("GET", "api/shop")
check("(moving data) customers aren't shown a sale whose deadline passed", bake_of(shop, tid) is None)

other = Client(wsgi.application)
code, _ = api("GET", "api/admin/state", client=other)
check("someone else isn't signed in", code == 401)
r = other.get("/admin")
check("playdate admin still needs its own login", r.status_code == 302 and "/admin/login" in r.headers.get("Location", ""))

if mock:
    mock.stop()
print(json.dumps({"database": "postgres" if os.environ.get("DATABASE_URL") else "sqlite", "photos": "r2" if use_r2 else "folder",
                  "passed": len(results) - len(failures), "total": len(results), "failures": failures}))
sys.exit(1 if failures else 0)
