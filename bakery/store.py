"""Mollie's bakery: the store rules (what's for sale, what's left, is this order valid).

A line-for-line Python version of the rules in static/core.js, which the admin page still
uses in the browser for the bake list. Money is always whole cents. Times are milliseconds.
Works on Python 3.9 (the Mac's version).
"""
import copy
import datetime as dt
import math
import re
import secrets
from urllib.parse import quote

ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


class UserError(Exception):
    """A problem to show the customer or Mollie as-is."""


def uid(n=12):
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def now_ms():
    return int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)


def iso(ms):
    d = dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + "{:03d}Z".format(int(ms % 1000))


def parse_ms(value):
    """ISO date string -> milliseconds, or None if it isn't a date."""
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000)


def js_round(x):
    return int(math.floor(x + 0.5))


def clean(v, maxlen=500):
    return ("" if v is None else str(v)).strip()[:maxlen]


def to_num(v):
    try:
        n = float(v)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def to_int(v):
    n = to_num(v)
    return int(math.floor(n)) if n is not None else 0


def opt_int(v):
    return None if v is None or v == "" else max(0, to_int(v))


def money(cents):
    return "${:.0f}".format(cents / 100) if cents % 100 == 0 else "${:.2f}".format(cents / 100)


def default_state():
    return {
        "settings": {
            "shopName": "Mollie's Farmhouse Bakery",
            "season": "Thanksgiving 2026",
            "tagline": "Gluten-free holiday baking from Gooseberry Bridge Farm",
            "intro": "Preorder for pickup on Wednesday, November 25th. Order by November 21st, or until sold out.",
            "dietaryNote": "Everything is gluten-free, dairy-free, soy-free, seed-oil-free and free of artificial dyes, made in a 100% gluten-free kitchen.",
            "contactEmail": "mollie@gooseberrybridge.com",
            "contactNote": "Questions? Email mollie@gooseberrybridge.com or message Mollie's Farmhouse Bakery on Facebook.",
            "orderingOpen": True,
            "deadline": "2026-11-21T23:59:00-06:00",
            "timezone": "America/Chicago",
            "holdMinutes": 30,
            "perOrderMax": 12,
            "askPickupTime": True,
            "askAllergies": True,
            "allergyPrompt": "Any allergies besides dairy and wheat/gluten? Mollie will reach out if she can't make a modification.",
            "thankYouNote": "Thank you! Mollie will have your order ready at pickup. Watch your email for your receipt.",
            # Pay-by-app: the order is reserved, the customer sends payment, Mollie marks it paid.
            "appPayEnabled": True,
            "appPayDiscountPct": 3,
            "appPayHours": 24,
            "venmo": "molliesfarmhousebakery",
            "cashapp": "molliesbakery",
            "paypal": "mollie@gooseberrybridge.com",
            "appPayNote": "Please put your order number in the payment note so Mollie can match it up.",
        },
        "items": [
            {"id": "cinn", "name": "Take & Bake Cinnamon Rolls", "description": "6 per order. Frozen, with baking instructions.", "price": 1400, "image": "", "limit": None, "active": True},
            {"id": "dinr", "name": "Take & Bake Dinner Rolls", "description": "6 per order. Frozen, with baking instructions.", "price": 1200, "image": "", "limit": None, "active": True},
            {"id": "pump", "name": "Pumpkin Pie", "description": "Whole 9-inch pie.", "price": 3500, "image": "", "limit": None, "active": True},
            {"id": "pmps", "name": "Pumpkin Pie, 2 Slices", "description": "Two generous slices.", "price": 1400, "image": "", "limit": None, "active": True},
            {"id": "choc", "name": "Chocolate Pie", "description": "Whole 9-inch pie. Egg-free.", "price": 3500, "image": "", "limit": None, "active": True},
            {"id": "chcs", "name": "Chocolate Pie, 2 Slices", "description": "Two generous slices. Egg-free.", "price": 1400, "image": "", "limit": None, "active": True},
        ],
        "pickups": [
            {"id": "farm", "label": "Gooseberry Bridge Farm", "details": "177 Elm Grove Rd, Rogersville. Wed, Nov 25, 9am–2pm", "capacity": None, "active": True},
            {"id": "fmo", "label": "Farmers Market of the Ozarks", "details": "FMO Holiday Market, Springfield. Wed, Nov 25, 3–7pm", "capacity": None, "active": True},
        ],
        "orders": [],
        "nextOrderNumber": 1001,
    }


def normalize(state):
    """Fill in settings added after a store was first created."""
    merged = default_state()["settings"]
    merged.update(state.get("settings") or {})
    state["settings"] = merged
    return state


APPS = [("venmo", "Venmo"), ("cashapp", "Cash App"), ("paypal", "PayPal")]


def apps_on(s):
    return [label for key, label in APPS if s.get(key)]


def app_pay_open(s):
    return bool(s.get("appPayEnabled")) and len(apps_on(s)) > 0


def hold_until(order, s):
    if order["status"] == "awaiting":
        span = (s.get("appPayHours") or 24) * 3600000
    else:
        span = (s.get("holdMinutes") or 30) * 60000
    return parse_ms(order["createdAt"]) + span


def holds(order, s, now):
    """An order counts against stock if it's paid, or still waiting on payment."""
    if order["status"] == "paid":
        return True
    if order["status"] not in ("pending", "awaiting"):
        return False
    return now < hold_until(order, s)


def reserved(state, item_id, now):
    n = 0
    s = state["settings"]
    for o in state["orders"]:
        if o.get("season") != s["season"] or not holds(o, s, now):
            continue
        n += sum(l["qty"] for l in o["lines"] if l["itemId"] == item_id)
    return n


def remaining(state, item, now):
    if item.get("limit") is None:
        return None  # no limit
    return max(0, item["limit"] - reserved(state, item["id"], now))


# ---------------------------------------------------------------- pickup windows
# A pickup place can offer dated time windows ("Wed, Nov 25, 9-11am"). Customers pick one.
# Dates and times are the shop's local wall-clock time (settings.timezone).

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _hm(t):
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(t or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return int(m.group(1)), int(m.group(2))


def _clock(hm, with_ampm=True):
    hh, mm = hm
    h12 = hh % 12 or 12
    text = str(h12) if mm == 0 else "{}:{:02d}".format(h12, mm)
    return text + (("am" if hh < 12 else "pm") if with_ampm else "")


def window_label(w):
    """'Wed, Nov 25, 9-11am' (or '11am-1pm' when the window crosses noon)."""
    try:
        d = dt.date(*[int(x) for x in w["date"].split("-")])
        day = "{}, {} {}".format(d.strftime("%a"), d.strftime("%b"), d.day)
    except (KeyError, ValueError, TypeError):
        day = str(w.get("date") or "")
    a, b = _hm(w.get("start")), _hm(w.get("end"))
    if not a:
        return day
    if not b:
        return "{}, from {}".format(day, _clock(a))
    same = (a[0] < 12) == (b[0] < 12)
    return "{}, {}\u2013{}".format(day, _clock(a, not same), _clock(b))


def window_start(w):
    return "{}T{}".format(w.get("date") or "", (w.get("start") or "00:00").zfill(5))


def window_end_ms(w, tz_name):
    """When the window closes, in epoch ms (end of that day if it has no end time)."""
    try:
        y, mo, d = [int(x) for x in w["date"].split("-")]
    except (KeyError, ValueError, TypeError):
        return None
    hh, mm = _hm(w.get("end")) or (23, 59)
    local = dt.datetime(y, mo, d, hh, mm)
    try:
        from zoneinfo import ZoneInfo
        local = local.replace(tzinfo=ZoneInfo(tz_name or "America/Chicago"))
    except Exception:  # no time zone data: assume Central standard time
        local = local.replace(tzinfo=dt.timezone(dt.timedelta(hours=-6)))
    return int(local.timestamp() * 1000)


def window_open(w, s, now):
    end = window_end_ms(w, s.get("timezone"))
    return w.get("active", True) and end is not None and now < end


def window_taken(state, window_id, now):
    s = state["settings"]
    return len([o for o in state["orders"]
                if o.get("season") == s["season"] and o.get("pickupWindowId") == window_id and holds(o, s, now)])


def clean_windows(raw, old):
    known = {w["id"] for w in (old or [])}
    out = []
    for w in (raw if isinstance(raw, list) else [])[:80]:
        w = w if isinstance(w, dict) else {}
        date, start, end = clean(w.get("date"), 10), clean(w.get("start"), 5), clean(w.get("end"), 5)
        if not (date or start or end):
            continue  # an empty row nobody filled in
        if not DATE_RE.match(date):
            raise UserError("Each pickup window needs a date.")
        if not _hm(start) or not _hm(end):
            raise UserError("Each pickup window needs a start and an end time.")
        if _hm(end) <= _hm(start):
            raise UserError("The window starting {} ends before it starts.".format(window_label({"date": date, "start": start})))
        out.append({"id": w.get("id") if w.get("id") in known else uid(6), "date": date,
                    "start": "{:02d}:{:02d}".format(*_hm(start)), "end": "{:02d}:{:02d}".format(*_hm(end)),
                    "capacity": opt_int(w.get("capacity")), "active": True})
    out.sort(key=window_start)
    return out


def pickup_taken(state, pickup_id, now):
    s = state["settings"]
    return len([o for o in state["orders"]
                if o.get("season") == s["season"] and o.get("pickupId") == pickup_id and holds(o, s, now)])


def ordering_status(state, now):
    s = state["settings"]
    if not s.get("orderingOpen"):
        return {"open": False, "reason": "Preorders are closed right now."}
    deadline = parse_ms(s.get("deadline"))
    if deadline is not None and now > deadline:
        return {"open": False, "reason": "The preorder deadline has passed."}
    for_sale = [i for i in state["items"] if i.get("active")]
    if not for_sale:
        return {"open": False, "reason": "Nothing is on the menu right now. Check back soon!"}
    if all(remaining(state, i, now) == 0 for i in for_sale):
        return {"open": False, "reason": "We're sold out. Thank you!"}
    return {"open": True}


# ---------------------------------------------------------------- customer side

def public_shop(state, now):
    s = state["settings"]
    keys = ["shopName", "season", "tagline", "intro", "dietaryNote", "contactNote", "deadline", "timezone",
            "perOrderMax", "askPickupTime", "askAllergies", "allergyPrompt"]
    return {
        "settings": {k: s.get(k) for k in keys},
        "appPay": ({"discountPct": to_num(s.get("appPayDiscountPct")) or 0, "hours": s.get("appPayHours"), "apps": apps_on(s)}
                   if app_pay_open(s) else None),
        "ordering": ordering_status(state, now),
        "items": [{"id": i["id"], "name": i["name"], "description": i.get("description", ""), "price": i["price"],
                   "image": i.get("image", ""), "remaining": remaining(state, i, now)}
                  for i in state["items"] if i.get("active")],
        "pickups": public_pickups(state, now),
    }


def public_pickups(state, now):
    s = state["settings"]
    out = []
    for p in state["pickups"]:
        if not p.get("active"):
            continue
        full = p.get("capacity") is not None and pickup_taken(state, p["id"], now) >= p["capacity"]
        wins = [{"id": w["id"], "label": window_label(w), "start": window_start(w),
                 "full": full or (w.get("capacity") is not None and window_taken(state, w["id"], now) >= w["capacity"])}
                for w in p.get("windows") or [] if window_open(w, s, now)]
        if p.get("windows") and not wins:
            continue  # every window has passed
        out.append({"id": p["id"], "label": p["label"], "details": p.get("details", ""), "full": full,
                    "windows": wins})
    return out


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def build_order(state, data, now):
    """Check a checkout request and return a new unpaid order (the caller saves it)."""
    s = state["settings"]
    status = ordering_status(state, now)
    if not status["open"]:
        raise UserError(status["reason"])

    name = clean(data.get("name"), 80)
    email = clean(data.get("email"), 120).lower()
    phone = clean(data.get("phone"), 30)
    if not name:
        raise UserError("Please add a name for the order.")
    if not EMAIL_RE.match(email):
        raise UserError("Please check your email address.")
    if len(re.sub(r"\D", "", phone)) < 10:
        raise UserError("Please add a phone number Mollie can reach you at.")

    pickup = next((p for p in state["pickups"] if p["id"] == data.get("pickupId") and p.get("active")), None)
    if not pickup:
        raise UserError("Please choose a pickup option.")
    if pickup.get("capacity") is not None and pickup_taken(state, pickup["id"], now) >= pickup["capacity"]:
        raise UserError(pickup["label"] + " is full. Please choose another pickup option.")
    window = None
    if pickup.get("windows"):
        window = next((w for w in pickup["windows"] if w["id"] == data.get("pickupWindowId")), None)
        if not window:
            raise UserError("Please choose a pickup time.")
        if not window_open(window, s, now):
            raise UserError("That pickup time has passed. Please choose another.")
        if window.get("capacity") is not None and window_taken(state, window["id"], now) >= window["capacity"]:
            raise UserError("That pickup time just filled up. Please choose another.")

    lines = []
    most = s.get("perOrderMax") or 12
    cart = data.get("cart") if isinstance(data.get("cart"), list) else []
    for entry in cart:
        entry = entry if isinstance(entry, dict) else {}
        qty = to_int(entry.get("qty"))
        if qty <= 0:
            continue
        item = next((i for i in state["items"] if i["id"] == entry.get("itemId") and i.get("active")), None)
        if not item:
            raise UserError("Something in your order is no longer available. Please refresh the page.")
        if qty > most:
            raise UserError("You can order up to {} of each item. Need more? Contact Mollie.".format(most))
        left = remaining(state, item, now)
        if left is not None and qty > left:
            raise UserError(item["name"] + " just sold out." if left == 0 else "Only {} left of {}.".format(left, item["name"]))
        lines.append({"itemId": item["id"], "name": item["name"], "price": item["price"], "qty": qty})
    if not lines:
        raise UserError("Your order is empty.")

    pay_method = "app" if data.get("payMethod") == "app" else "card"
    if pay_method == "app" and not app_pay_open(s):
        raise UserError("Paying by app isn’t available right now. Please pay by card.")
    subtotal = sum(l["price"] * l["qty"] for l in lines)
    discount = js_round(subtotal * (to_num(s.get("appPayDiscountPct")) or 0) / 100) if pay_method == "app" else 0

    number = state["nextOrderNumber"]
    state["nextOrderNumber"] = number + 1
    return {
        "id": uid(16),
        "number": number,
        "season": s["season"],
        "createdAt": iso(now),
        "status": "awaiting" if pay_method == "app" else "pending",
        "payMethod": pay_method,
        "customer": {"name": name, "email": email, "phone": phone},
        "pickupId": pickup["id"],
        "pickupLabel": pickup["label"],
        "pickupDetails": pickup.get("details", ""),
        "pickupWindowId": window["id"] if window else "",
        "pickupWhen": window_label(window) if window else "",
        "pickupStart": window_start(window) if window else "",
        "pickupTime": clean(data.get("pickupTime"), 60) if s.get("askPickupTime") and not window else "",
        "allergyNote": clean(data.get("allergyNote"), 500) if s.get("askAllergies") else "",
        "lines": lines,
        "subtotal": subtotal,
        "discount": discount,
        "total": subtotal - discount,
        "pickedUp": False,
    }


def find_order(state, order_id):
    return next((o for o in state["orders"] if o["id"] == order_id), None)


def mark_paid(state, order_id, info, now):
    o = find_order(state, order_id)
    if not o or o["status"] == "paid":
        return o
    o["status"] = "paid"
    o["paidAt"] = iso(now)
    info = info or {}
    if info.get("amountPaid") is not None:
        o["amountPaid"] = info["amountPaid"]
    if info.get("via"):
        o["paidVia"] = info["via"]
    return o


def _strip_handle(v):
    v = re.sub(r"^[@$]", "", str(v).strip())
    v = re.sub(r"^https?://(www\.)?(venmo\.com|cash\.app|paypal\.me)/(u/)?", "", v, flags=re.I)
    return re.sub(r"/.*$", "", v)


def app_links(s, o):
    """Buttons that open each payment app with the amount (and note, where the app allows it) filled in."""
    amt = "{:.2f}".format(o["total"] / 100)
    note = "Order #{}".format(o["number"])
    out = []
    if s.get("venmo"):
        h = _strip_handle(s["venmo"])
        out.append({"app": "Venmo", "handle": "@" + h,
                    "url": "https://venmo.com/?txn=pay&recipients={}&amount={}&note={}".format(quote(h, safe=""), amt, quote(note, safe=""))})
    if s.get("cashapp"):
        h = _strip_handle(s["cashapp"])
        out.append({"app": "Cash App", "handle": "$" + h, "url": "https://cash.app/${}/{}".format(quote(h, safe=""), amt)})
    if s.get("paypal") and "@" in s["paypal"]:
        # An email can't pre-fill PayPal, so open the Send page and let them copy the address.
        e = s["paypal"].strip()
        out.append({"app": "PayPal", "handle": e, "copy": e, "url": "https://www.paypal.com/myaccount/transfer/homepage/pay"})
    elif s.get("paypal"):
        h = _strip_handle(s["paypal"])
        out.append({"app": "PayPal", "handle": "paypal.me/" + h, "url": "https://paypal.me/{}/{}USD".format(quote(h, safe=""), amt)})
    return out


def public_order(o, s=None):
    out = {
        "id": o["id"], "number": o["number"], "status": o["status"], "payMethod": o.get("payMethod") or "card",
        "name": o["customer"]["name"], "email": o["customer"]["email"],
        "pickupLabel": o["pickupLabel"], "pickupDetails": o.get("pickupDetails", ""), "pickupTime": o.get("pickupTime", ""),
        "pickupWhen": o.get("pickupWhen", ""),
        "lines": o["lines"], "subtotal": o.get("subtotal") or o["total"], "discount": o.get("discount") or 0, "total": o["total"],
    }
    if o["status"] == "awaiting" and s:
        out["payBy"] = iso(hold_until(o, s))
        out["timezone"] = s.get("timezone")
        out["appLinks"] = app_links(s, o)
        out["appPayNote"] = s.get("appPayNote")
    return out


# ---------------------------------------------------------------- Mollie's side

def admin_action(state, a, now):
    """Every change Mollie makes goes through here."""
    s = state["settings"]
    action = a.get("action")
    if action == "saveSettings":
        v = a.get("settings") or {}
        for k in ["shopName", "season", "tagline", "intro", "dietaryNote", "contactEmail", "contactNote",
                  "allergyPrompt", "thankYouNote", "appPayNote"]:
            if k in v:
                s[k] = clean(v[k], 1000)
        for k in ["venmo", "cashapp", "paypal"]:
            if k in v:
                s[k] = re.sub(r"\s+", "", clean(v[k], 80))
        for k in ["orderingOpen", "askPickupTime", "askAllergies", "appPayEnabled"]:
            if k in v:
                s[k] = bool(v[k])
        if "appPayDiscountPct" in v:
            s["appPayDiscountPct"] = min(10, max(0, to_num(v["appPayDiscountPct"]) or 0))
        if "appPayHours" in v:
            s["appPayHours"] = min(168, max(1, to_int(v["appPayHours"]) or 24))
        if "deadline" in v:
            ms = parse_ms(v["deadline"])
            s["deadline"] = iso(ms) if ms is not None else ""
        if "perOrderMax" in v:
            s["perOrderMax"] = min(99, max(1, to_int(v["perOrderMax"]) or 12))
        if not s.get("shopName"):
            s["shopName"] = "Bakery"
        if not s.get("season"):
            s["season"] = "Orders"
        return {}

    if action == "saveItem":
        v = a.get("item") or {}
        num = to_num(re.sub(r"[$,]", "", str(v.get("price", ""))))
        price = js_round(num * 100) if num is not None else None
        if not clean(v.get("name")):
            raise UserError("Give the item a name.")
        if price is None or price < 50:
            raise UserError("Price needs to be at least $0.50 (a Stripe minimum).")
        item = next((i for i in state["items"] if i["id"] == v.get("id")), None)
        if not item:
            item = {"id": uid(6), "active": True, "image": ""}
            state["items"].append(item)
        item["name"] = clean(v.get("name"), 80)
        item["description"] = clean(v.get("description"), 400)
        item["price"] = price
        item["limit"] = opt_int(v.get("limit"))
        if "active" in v:
            item["active"] = bool(v["active"])
        if "image" in v:
            item["image"] = v["image"] or ""
        return {"id": item["id"]}

    if action == "deleteItem":
        state["items"] = [i for i in state["items"] if i["id"] != a.get("id")]
        return {}

    if action in ("moveItem", "movePickup"):
        lst = state["items"] if action == "moveItem" else state["pickups"]
        i = next((n for n, x in enumerate(lst) if x["id"] == a.get("id")), -1)
        j = i + (-1 if (to_num(a.get("dir")) or 0) < 0 else 1)
        if i >= 0 and 0 <= j < len(lst):
            lst[i], lst[j] = lst[j], lst[i]
        return {}

    if action == "savePickup":
        v = a.get("pickup") or {}
        if not clean(v.get("label")):
            raise UserError("Give the pickup option a name.")
        p = next((x for x in state["pickups"] if x["id"] == v.get("id")), None)
        if not p:
            p = {"id": uid(6), "active": True}
            state["pickups"].append(p)
        p["label"] = clean(v.get("label"), 80)
        p["details"] = clean(v.get("details"), 200)
        p["capacity"] = opt_int(v.get("capacity"))
        if "windows" in v:
            p["windows"] = clean_windows(v.get("windows"), p.get("windows"))
        if "active" in v:
            p["active"] = bool(v["active"])
        return {"id": p["id"]}

    if action == "deletePickup":
        state["pickups"] = [p for p in state["pickups"] if p["id"] != a.get("id")]
        return {}

    if action == "markPaid":
        o = find_order(state, a.get("id"))
        if not o:
            raise UserError("Order not found.")
        mark_paid(state, o["id"], {"via": a.get("via") or ("app" if o.get("payMethod") == "app" else "manual")}, now)
        return {}

    if action == "setPickedUp":
        o = find_order(state, a.get("id"))
        if o:
            o["pickedUp"] = bool(a.get("pickedUp"))
        return {}

    if action == "cancelOrder":
        # Takes it off the list. Refunds are done in Stripe.
        o = find_order(state, a.get("id"))
        if o:
            o["status"] = "cancelled"
        return {}

    raise UserError("Unknown action.")


def summarize(state, season, now=None):
    now = now or now_ms()
    s = state["settings"]
    awaiting = []
    for o in state["orders"]:
        if o.get("season") == season and o["status"] == "awaiting":
            x = copy.deepcopy(o)
            x["overdue"] = not holds(o, s, now)
            x["payBy"] = iso(hold_until(o, s))
            awaiting.append(x)
    orders = [o for o in state["orders"] if o.get("season") == season and o["status"] == "paid"]
    items, pickups = {}, []
    for o in orders:
        if o["pickupLabel"] not in pickups:
            pickups.append(o["pickupLabel"])
        for l in o["lines"]:
            row = items.setdefault(l["itemId"], {"name": l["name"], "total": 0, "byPickup": {}})
            row["total"] += l["qty"]
            row["byPickup"][o["pickupLabel"]] = row["byPickup"].get(o["pickupLabel"], 0) + l["qty"]
    seasons = []
    for o in state["orders"]:
        if o.get("season") not in seasons:
            seasons.append(o.get("season"))
    return {"orders": orders, "awaiting": awaiting, "pickups": pickups, "bakeList": list(items.values()),
            "revenue": sum(o["total"] for o in orders), "seasons": seasons}


def orders_csv(state, season):
    orders = sorted(summarize(state, season)["orders"], key=lambda o: (o.get("pickupStart") or "~", o["number"]))
    names = []
    for o in orders:
        for l in o["lines"]:
            if l["name"] not in names:
                names.append(l["name"])

    def esc(v):
        return '"' + ("" if v is None else str(v)).replace('"', '""') + '"'

    head = ["Order #", "Name", "Email", "Phone", "Pickup", "Pickup time", "Allergies"] + names + \
           ["Discount", "Total", "Paid by", "Picked up", "Paid at"]
    rows = [head]
    for o in orders:
        counts = [sum(l["qty"] for l in o["lines"] if l["name"] == n) or "" for n in names]
        rows.append([o["number"], o["customer"]["name"], o["customer"]["email"], o["customer"]["phone"],
                     o["pickupLabel"], o.get("pickupWhen") or o.get("pickupTime", ""), o.get("allergyNote", "")] + counts +
                    ["{:.2f}".format(o["discount"] / 100) if o.get("discount") else "", "{:.2f}".format(o["total"] / 100),
                     "App (Venmo/Cash App/PayPal)" if o.get("payMethod") == "app" else "Card",
                     "yes" if o.get("pickedUp") else "", o.get("paidAt", "")])
    return "\n".join(",".join(esc(v) for v in r) for r in rows)
