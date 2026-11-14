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
check("ordering open", shop["ordering"]["open"])

code, _ = api("GET", "api/admin/state")
check("admin needs login", code == 401)
code, _ = api("POST", "api/admin/login", {"password": "nope"})
check("wrong password refused", code == 401)
code, _ = api("POST", "api/admin/login", {"password": "pie"})
check("Mollie signs in", code == 200)
code, st = api("GET", "api/admin/state")
check("admin state", code == 200 and st["settings"]["shopName"] == "Mollie's Farmhouse Bakery")

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
