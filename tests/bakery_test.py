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
    d = {"name": "Test Buyer", "email": "buyer@example.com", "phone": "417-555-0100", "pickupId": "farm"}
    d.update(kw)
    return d


code, shop = api("GET", "api/shop")
check("shop loads", code == 200 and len(shop["items"]) == 6, code)
check("app payments offered", shop["appPay"] and shop["appPay"]["apps"] == ["Venmo", "Cash App", "PayPal"])
check("ordering open (or closed only by the starting deadline)", shop["ordering"]["open"] or "deadline" in shop["ordering"].get("reason", ""))

code, _ = api("GET", "api/admin/state")
check("admin needs login", code == 401)
code, _ = api("POST", "api/admin/login", {"password": "nope"})
check("wrong password refused", code == 401)
code, _ = api("POST", "api/admin/login", {"password": "pie"})
check("Mollie signs in", code == 200)
code, st = api("GET", "api/admin/state")
check("build number in Mollie's admin", bool(st.get("build")), st.get("build"))
check("admin state", code == 200 and st["settings"]["shopName"] == "Mollie's Farmhouse Bakery")

# The starting settings carry Mollie's real Thanksgiving deadline; clear it so this check doesn't depend on today's date.
api("POST", "api/admin/action", {"action": "saveSettings", "settings": {"deadline": ""}})
check("ordering open", api("GET", "api/shop")[1]["ordering"]["open"])
jpeg = base64.b64encode(b"\xff\xd8\xff\xe0fakejpegbytes\xff\xd9").decode()
code, res = api("POST", "api/admin/action", {"action": "saveItem", "item": {"id": "pump", "name": "Pumpkin Pie", "description": "Whole 9-inch pie.",
                                                                            "price": "35.00", "limit": "2", "active": True, "image": "data:image/jpeg;base64," + jpeg}})
check("item saved with photo and limit", code == 200, res)
_, shop = api("GET", "api/shop")
pump = next(i for i in shop["items"] if i["id"] == "pump")
check("photo path stored", pump["image"].startswith("uploads/"), pump["image"])
r = c.get("/bakery/" + pump["image"])
check("photo served", r.status_code == 200 and r.data.startswith(b"\xff\xd8"), r.status_code)
check("stock shows 2 left", pump["remaining"] == 2)

code, res = api("POST", "api/checkout", buyer(cart=[{"itemId": "pump", "qty": 1}, {"itemId": "cinn", "qty": 2}], pickupTime="around 10"))
check("card order (demo) goes through", code == 200 and "?order=" in res.get("url", ""), res)
card_id = res.get("orderId")
code, o = api("GET", "api/orders/" + card_id)
check("card order paid", o.get("status") == "paid" and o["total"] == 3500 + 2 * 1400, o)

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

code, _ = api("POST", "api/admin/action", {"action": "markPaid", "id": app_id})
code, o = api("GET", "api/orders/" + app_id)
check("Mollie marks app order paid", o.get("status") == "paid")
code, _ = api("POST", "api/admin/action", {"action": "setPickedUp", "id": card_id, "pickedUp": True})
r = c.get("/bakery/api/admin/orders.csv")
lines = r.get_data(as_text=True).strip().splitlines()
check("spreadsheet has both orders", r.status_code == 200 and len(lines) == 3, lines[:1])

code, _ = api("POST", "api/admin/action", {"action": "savePickup", "pickup": {"label": "Church lot", "details": "Sun after service", "capacity": "1", "active": True}})
_, shop = api("GET", "api/shop")
check("new pickup offered", any(p["label"] == "Church lot" for p in shop["pickups"]))
code, _ = api("POST", "api/admin/action", {"action": "saveSettings", "settings": {"deadline": "2020-01-01T00:00:00.000Z"}})
_, shop = api("GET", "api/shop")
check("deadline closes orders", not shop["ordering"]["open"] and "deadline" in shop["ordering"]["reason"])
code, _ = api("POST", "api/admin/action", {"action": "saveSettings", "settings": {"deadline": "", "orderingOpen": False}})
_, shop = api("GET", "api/shop")
check("stop button closes orders", not shop["ordering"]["open"])

# ---- customer emails
sent = wsgi.bakery.mailer.sent
subjects = [m["subject"] for m in sent]
check("card order gets one 'confirmed' email", sum(1 for x in subjects if x.startswith("Order #1001 is confirmed")) == 1, subjects)
check("app order gets a 'reserved' email with how to pay", any(x.startswith("Order #1002 is reserved") for x in subjects), subjects)
rsv = next((m for m in sent if m["subject"].startswith("Order #1002 is reserved")), {"text": ""})
check("reserved email has the payment buttons' links and note", "venmo.com" in rsv["text"] and "Order #1002" in rsv["text"] and "$33.95" in rsv["text"], rsv["text"][:300])
check("app order gets 'confirmed' once Mollie marks it paid", sum(1 for x in subjects if x.startswith("Order #1002 is confirmed")) == 1, subjects)
api("POST", "api/admin/action", {"action": "markPaid", "id": app_id})
check("no duplicate email if marked paid twice", sum(1 for m in wsgi.bakery.mailer.sent if m["subject"].startswith("Order #1002 is confirmed")) == 1)
check("emails go to the customer", all(m["to"] == "buyer@example.com" for m in sent))

# ---- pickup windows (Mollie sets dated time slots; customers pick one)
api("POST", "api/admin/action", {"action": "saveSettings", "settings": {"deadline": "", "orderingOpen": True}})
code, res = api("POST", "api/admin/action", {"action": "savePickup", "pickup": {"id": "farm", "label": "Gooseberry Bridge Farm", "details": "177 Elm Grove Rd", "active": True,
    "windows": [{"date": "2030-11-25", "start": "13:00", "end": "11:00"}]}})
check("a window that ends before it starts is refused", code == 400 and "before it starts" in str(res), res)
wins = [{"date": "2030-11-25", "start": "11:00", "end": "13:00"}, {"date": "2030-11-25", "start": "9:00", "end": "11:00", "capacity": "1"},
        {"date": "2020-01-01", "start": "09:00", "end": "11:00"}, {"date": "", "start": "", "end": ""}]
code, _ = api("POST", "api/admin/action", {"action": "savePickup", "pickup": {"id": "farm", "label": "Gooseberry Bridge Farm", "details": "177 Elm Grove Rd", "active": True, "windows": wins}})
_, st = api("GET", "api/admin/state")
saved = next(p for p in st["pickups"] if p["id"] == "farm")["windows"]
check("windows saved in time order, blank row ignored", code == 200 and [w["start"] for w in saved] == ["09:00", "09:00", "11:00"] and saved[0]["date"] == "2020-01-01", saved)
past_id, early_id, late_id = saved[0]["id"], saved[1]["id"], saved[2]["id"]
_, shop = api("GET", "api/shop")
farm = next(p for p in shop["pickups"] if p["id"] == "farm")
check("customers see future windows only, labeled plainly", [w["label"] for w in farm["windows"]] == ["Mon, Nov 25, 9\u201311am", "Mon, Nov 25, 11am\u20131pm"], farm["windows"])
code, res = api("POST", "api/checkout", buyer(cart=[{"itemId": "dinr", "qty": 1}]))
check("a place with windows needs a time picked", code == 400 and "pickup time" in str(res), res)
code, res = api("POST", "api/checkout", buyer(cart=[{"itemId": "dinr", "qty": 1}], pickupWindowId=past_id))
check("a past window is refused", code == 400 and "passed" in str(res), res)
code, res = api("POST", "api/checkout", buyer(cart=[{"itemId": "dinr", "qty": 1}], pickupWindowId=early_id, pickupTime="5pm"))
win_order = res.get("orderId") if isinstance(res, dict) else None
_, o = api("GET", "api/orders/{}".format(win_order))
check("order records the window", code == 200 and o.get("pickupWhen") == "Mon, Nov 25, 9\u201311am" and not o.get("pickupTime"), o)
code, res = api("POST", "api/checkout", buyer(cart=[{"itemId": "dinr", "qty": 1}], pickupWindowId=early_id))
check("a window at its max is refused", code == 400 and "filled up" in str(res), res)
_, shop = api("GET", "api/shop")
farm = next(p for p in shop["pickups"] if p["id"] == "farm")
check("a full window shows as full", farm["windows"][0]["full"] and not farm["windows"][1]["full"], farm["windows"])
conf = next((m for m in wsgi.bakery.mailer.sent if m["subject"].startswith("Order #{} is confirmed".format(o.get("number")))), {"text": ""})
check("confirmation email names the window", "Pickup: Mon, Nov 25, 9\u201311am\n  Gooseberry Bridge Farm" in conf["text"], conf["text"][-300:])
code, res = api("POST", "api/checkout", buyer(cart=[{"itemId": "dinr", "qty": 1}], pickupId="fmo", pickupTime="around 4"))
_, o2 = api("GET", "api/orders/{}".format(res.get("orderId") if isinstance(res, dict) else ""))
check("a place without windows still works as before", code == 200 and o2.get("pickupTime") == "around 4" and not o2.get("pickupWhen"), o2)
api("POST", "api/admin/action", {"action": "savePickup", "pickup": {"id": "farm", "label": "Gooseberry Bridge Farm", "details": "177 Elm Grove Rd", "active": True, "windows": saved}})
_, st = api("GET", "api/admin/state")
check("editing keeps each window's identity", [w["id"] for w in next(p for p in st["pickups"] if p["id"] == "farm")["windows"]] == [past_id, early_id, late_id])
r = c.get("/bakery/api/admin/orders.csv")
check("spreadsheet shows the window", "Mon, Nov 25, 9\u201311am" in r.get_data(as_text=True))

# ---- low-stock badge setting
api("POST", "api/admin/action", {"action": "saveSettings", "settings": {"lowStockAt": "3"}})
_, shop = api("GET", "api/shop")
check("Mollie can set when 'Only X left' shows", shop["settings"].get("lowStockAt") == 3, shop["settings"].get("lowStockAt"))

# ---- full descriptions and allergen tags
code, _ = api("POST", "api/admin/action", {"action": "saveItem", "item": {"id": "choc", "name": "Chocolate Pie", "description": "Whole 9-inch pie.", "price": "35",
    "details": "Decadent and rich.\nMost people don't believe it's dairy-free!", "contains": "Contains: coconut", "active": True}})
_, shop = api("GET", "api/shop")
choc = next(i for i in shop["items"] if i["id"] == "choc")
check("item full description saved with its paragraphs", code == 200 and choc["details"].startswith("Decadent and rich.\nMost"), choc)
check("allergen tag saved without a doubled 'Contains'", choc["contains"] == "coconut", choc.get("contains"))

# ---- each sale remembers its own menu (Nov 23, 2026)
def on_shop():
    return {i["id"] for i in api("GET", "api/shop")[1]["items"]}


def admin():
    return api("GET", "api/admin/state")[1]


start_sale = admin()["settings"]["season"]
all_items = {i["id"] for i in admin()["items"]}
for i in all_items:
    api("POST", "api/admin/action", {"action": "toggleItem", "id": i, "on": True})
check("(setup) every item in the current sale", on_shop() == all_items, on_shop())
code, _ = api("POST", "api/admin/action", {"action": "toggleItem", "id": "pmps", "on": False})
check("the In-this-sale switch takes an item off the shop without deleting it", code == 200 and "pmps" not in on_shop()
      and "pmps" in {i["id"] for i in admin()["items"]})
check("...and this sale's saved menu updates right away", "pmps" not in admin()["settings"]["saleMenus"][start_sale]
      and "pump" in admin()["settings"]["saleMenus"][start_sale])
code, _ = api("POST", "api/admin/action", {"action": "saveSettings", "settings": {"season": "Christmas 2026", "menuStartFrom": ""}})
check("a new sale starts with the current menu by default", code == 200 and on_shop() == all_items - {"pmps"}, on_shop())
api("POST", "api/admin/action", {"action": "toggleItem", "id": "pump", "on": False})
check("Christmas without pumpkin pie", "pump" not in on_shop())
api("POST", "api/admin/action", {"action": "saveSettings", "settings": {"season": start_sale}})
check("going back to the earlier sale brings its menu back", on_shop() == all_items - {"pmps"}, on_shop())
api("POST", "api/admin/action", {"action": "saveSettings", "settings": {"season": "Christmas 2026"}})
check("...and Christmas keeps its own", on_shop() == all_items - {"pmps", "pump"}, on_shop())
api("POST", "api/admin/action", {"action": "saveSettings", "settings": {"season": "Easter 2027", "menuStartFrom": "none"}})
check("a new sale can start empty", on_shop() == set() and len(admin()["items"]) == len(all_items))
api("POST", "api/admin/action", {"action": "saveSettings", "settings": {"season": "Thanksgiving 2027", "menuStartFrom": start_sale}})
check("a new sale can start from an earlier sale's menu", on_shop() == all_items - {"pmps"}, on_shop())
menus = admin()["settings"].get("saleMenus", {})
check("every sale's menu is remembered", {start_sale, "Christmas 2026", "Easter 2027", "Thanksgiving 2027"} <= set(menus), list(menus))
api("POST", "api/admin/action", {"action": "saveItem", "item": {"id": "pmps", "name": "Pumpkin Pie, 2 Slices", "description": "Two generous slices.",
                                                               "price": "14", "active": True}})
check("ticking In this sale while editing an item counts for this sale", "pmps" in on_shop() and "pmps" in admin()["settings"]["saleMenus"]["Thanksgiving 2027"])
check("customers never see the list of sales", "saleMenus" not in json.dumps(api("GET", "api/shop")[1]))
api("POST", "api/admin/action", {"action": "saveItem", "item": {"name": "Gingerbread", "price": "12", "active": True}})
gid = next(i["id"] for i in admin()["items"] if i["name"] == "Gingerbread")
api("POST", "api/admin/action", {"action": "deleteItem", "id": gid})
check("deleting an item takes it out of every sale's menu", all(gid not in ids for ids in admin()["settings"]["saleMenus"].values()))
api("POST", "api/admin/action", {"action": "saveSettings", "settings": {"season": start_sale}})

# Mollie's live data today has no saved menus at all: the first sale switch must still keep Thanksgiving's menu.
import copy  # noqa: E402
from bakery import store as bakery_store  # noqa: E402
legacy = copy.deepcopy(bakery_store.default_state())
legacy["settings"].pop("saleMenus", None)
next(i for i in legacy["items"] if i["id"] == "pmps")["active"] = False
before = sorted(i["id"] for i in legacy["items"] if i["active"])
bakery_store.admin_action(legacy, {"action": "saveSettings", "settings": {"season": "Christmas 2026", "menuStartFrom": "none"}}, 0)
check("(legacy data) the new sale starts as asked", not any(i["active"] for i in legacy["items"]))
bakery_store.admin_action(legacy, {"action": "saveSettings", "settings": {"season": "Thanksgiving 2026"}}, 0)
check("(legacy data) the first switch still keeps the old sale's menu", sorted(i["id"] for i in legacy["items"] if i["active"]) == before,
      sorted(i["id"] for i in legacy["items"] if i["active"]))

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
