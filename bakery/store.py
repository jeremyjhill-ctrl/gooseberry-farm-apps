"""Mollie's bakery: the store rules (what's for sale, what's left, is this order valid).

The Menu is everything Mollie makes. A "Bake" is one preorder: its own menu (picked from the Menu), how many of each
she can make, its pickup places and times, and when orders open and close. Several Bakes can take orders at once
(say, Christmas and this Saturday's market); each order belongs to one Bake. Added Nov 23, 2026.

Money is always whole cents. Times are milliseconds. Works on Python 3.9 (the Mac's version).
"""
import copy
import datetime as dt
import hashlib
import math
import re
import secrets
from urllib.parse import quote

ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
DAY_MS = 86400000


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


# Shop-wide settings. Anything about one preorder (its name, note, deadline, menu, pickups) lives on its Bake.
DEFAULT_SETTINGS = {
    "shopName": "Mollie's Farmhouse Bakery",
    "tagline": "Gluten-free holiday baking from Gooseberry Bridge Farm",
    "intro": "",  # shop-wide welcome; each Bake has its own note
    "dietaryNote": "Everything is gluten-free, dairy-free, soy-free, seed-oil-free and free of artificial dyes, made in a 100% gluten-free kitchen.",
    "contactEmail": "mollie@gooseberrybridge.com",
    "contactNote": "Questions? Email mollie@gooseberrybridge.com or message Mollie's Farmhouse Bakery on Facebook.",
    "timezone": "America/Chicago",
    "holdMinutes": 30,
    "perOrderMax": 12,
    "lowStockAt": 5,  # show "Only X left" at this many or fewer (0 = never)
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
}
# Settings that belonged to the one-sale-at-a-time shop; they move onto a Bake.
OLD_SALE_SETTINGS = ["season", "deadline", "orderingOpen", "saleMenus"]


def legacy_default_state():
    """What a brand-new store looked like before Bakes (the starting point for a new store, too)."""
    s = dict(DEFAULT_SETTINGS)
    s.update({"season": "Thanksgiving 2026", "orderingOpen": True, "deadline": "2026-11-21T23:59:00-06:00",
              "intro": "Preorder for pickup on Wednesday, November 25th. Order by November 21st, or until sold out."})
    return {
        "settings": s,
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


def default_state():
    return normalize(legacy_default_state())


def new_bake(name, created_ms=0):
    return {"id": uid(8), "name": name, "note": "", "opensAt": "", "deadline": "", "open": False,
            "menu": [], "limits": {}, "pickups": [], "createdAt": iso(created_ms or now_ms())}


def migrate(state):
    """One sale at a time -> Bakes. The current sale becomes a Bake with its menu, stock limits, pickups, deadline,
    welcome message and on/off switch, and keeps its orders. Each earlier sale (by name, from its orders or its saved
    menu) becomes a switched-off Bake holding its own orders. Safe to run on data that's already been moved."""
    if isinstance(state.get("bakes"), list):
        return state
    s = state.get("settings") or {}
    items = state.get("items") or []
    current = s.get("season") or "Orders"
    menus = s.get("saleMenus") or {}
    names = []
    for o in state.get("orders") or []:
        if (o.get("season") or current) not in names:
            names.append(o.get("season") or current)
    for n in list(menus) + [current]:
        if n not in names:
            names.append(n)
    bakes, by_name = [], {}
    for n in names:
        b = new_bake(n, 0)
        # The same old data always gets the same ids, so a page read before the move is saved still matches.
        digest = hashlib.sha1(n.encode("utf-8")).digest()
        b["id"] = "".join(ALPHABET[x % len(ALPHABET)] for x in digest[:8])
        b["createdAt"] = ""
        b["menu"] = [x for x in menus.get(n, []) if any(i["id"] == x for i in items)]
        b["pickups"] = copy.deepcopy(state.get("pickups") or [])
        if n == current:
            b["note"] = s.get("intro") or ""
            b["deadline"] = s.get("deadline") or ""
            b["open"] = bool(s.get("orderingOpen"))
            b["menu"] = [i["id"] for i in items if i.get("active")]
            b["limits"] = {i["id"]: i["limit"] for i in items if i.get("limit") is not None}
        bakes.append(b)
        by_name[n] = b
    for o in state.get("orders") or []:
        b = by_name[o.get("season") or current]
        o["bakeId"], o["bakeName"] = b["id"], b["name"]
    for i in items:
        i.pop("active", None)
        i.pop("limit", None)
    for k in OLD_SALE_SETTINGS:
        s.pop(k, None)
    s["intro"] = ""  # its words were about the one sale; they're that Bake's note now
    state.pop("pickups", None)
    # The current sale goes first: it's the one Mollie is working on.
    state["bakes"] = [by_name[current]] + [b for b in bakes if b is not by_name[current]]
    return state


def normalize(state):
    """Moves old data to Bakes, then fills in settings added after a store was first created."""
    migrate(state)
    merged = dict(DEFAULT_SETTINGS)
    merged.update(state.get("settings") or {})
    state["settings"] = merged
    state.setdefault("items", [])
    state.setdefault("orders", [])
    state.setdefault("nextOrderNumber", 1001)
    for b in state["bakes"]:
        b.setdefault("menu", [])
        b.setdefault("limits", {})
        b.setdefault("pickups", [])
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


def find_bake(state, bake_id):
    return next((b for b in state["bakes"] if b["id"] == bake_id), None)


def find_item(state, item_id):
    return next((i for i in state["items"] if i["id"] == item_id), None)


def holding_orders(state, bake, now):
    s = state["settings"]
    return [o for o in state["orders"] if o.get("bakeId") == bake["id"] and holds(o, s, now)]


def reserved(state, bake, item_id, now):
    return sum(l["qty"] for o in holding_orders(state, bake, now) for l in o["lines"] if l["itemId"] == item_id)


def remaining(state, bake, item_id, now):
    limit = (bake.get("limits") or {}).get(item_id)
    if limit is None:
        return None  # no limit
    return max(0, limit - reserved(state, bake, item_id, now))


def bake_items(state, bake):
    """This Bake's items, in the Menu's order."""
    on = set(bake.get("menu") or [])
    return [i for i in state["items"] if i["id"] in on]


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


def day_label(date_text):
    """'2026-11-25' -> 'Wed, Nov 25'."""
    try:
        d = dt.date(*[int(x) for x in str(date_text).split("-")])
    except (ValueError, TypeError):
        return str(date_text or "")
    return "{}, {} {}".format(d.strftime("%a"), d.strftime("%b"), d.day)


def window_label(w):
    """'Wed, Nov 25, 9-11am' (or '11am-1pm' when the window crosses noon)."""
    day = day_label(w.get("date"))
    a, b = _hm(w.get("start")), _hm(w.get("end"))
    if not a:
        return day
    if not b:
        return "{}, from {}".format(day, _clock(a))
    same = (a[0] < 12) == (b[0] < 12)
    return "{}, {}–{}".format(day, _clock(a, not same), _clock(b))


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


def window_taken(state, bake, window_id, now):
    return len([o for o in holding_orders(state, bake, now) if o.get("pickupWindowId") == window_id])


def pickup_taken(state, bake, pickup_id, now):
    return len([o for o in holding_orders(state, bake, now) if o.get("pickupId") == pickup_id])


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


# ---------------------------------------------------------------- is a Bake taking orders?

def bake_phase(b, now):
    """'open' (taking orders), 'upcoming' (switched on, opens later), 'closed' (its deadline passed) or 'off'."""
    if not b.get("open"):
        return "off"
    opens = parse_ms(b.get("opensAt"))
    if opens is not None and now < opens:
        return "upcoming"
    deadline = parse_ms(b.get("deadline"))
    if deadline is not None and now > deadline:
        return "closed"
    return "open"


def ordering_status(state, bake, now):
    phase = bake_phase(bake, now)
    if phase == "off":
        return {"open": False, "reason": "Preorders are closed right now."}
    if phase == "upcoming":
        return {"open": False, "reason": "Orders aren't open yet.", "opensAt": bake.get("opensAt")}
    if phase == "closed":
        return {"open": False, "reason": "The preorder deadline has passed."}
    for_sale = bake_items(state, bake)
    if not for_sale:
        return {"open": False, "reason": "Nothing is on the menu right now. Check back soon!"}
    if all(remaining(state, bake, i["id"], now) == 0 for i in for_sale):
        return {"open": False, "reason": "We're sold out. Thank you!"}
    return {"open": True}


def first_pickup(bake):
    """The earliest pickup window ('2026-11-25T09:00'), for putting Bakes in order. '~' sorts last."""
    starts = [window_start(w) for p in bake.get("pickups") or [] if p.get("active") for w in p.get("windows") or []]
    return min(starts) if starts else "~"


# ---------------------------------------------------------------- customer side

def public_shop(state, now):
    """Shop-wide wording plus every Bake customers can see: those taking orders and those opening later."""
    s = state["settings"]
    keys = ["shopName", "tagline", "intro", "dietaryNote", "contactNote", "timezone",
            "perOrderMax", "lowStockAt", "askPickupTime", "askAllergies", "allergyPrompt", "thankYouNote"]
    shown = [b for b in state["bakes"] if bake_phase(b, now) in ("open", "upcoming")]
    shown.sort(key=lambda b: (bake_phase(b, now) != "open", first_pickup(b), parse_ms(b.get("deadline")) or 9e15, b["name"]))
    return {
        "settings": {k: s.get(k) for k in keys},
        "appPay": ({"discountPct": to_num(s.get("appPayDiscountPct")) or 0, "hours": s.get("appPayHours"), "apps": apps_on(s)}
                   if app_pay_open(s) else None),
        "bakes": [public_bake(state, b, now) for b in shown],
    }


def public_bake(state, b, now):
    pickups = public_pickups(state, b, now)
    dates = []
    for p in pickups:
        for w in p["windows"]:
            if w["start"][:10] not in dates:
                dates.append(w["start"][:10])
    dates.sort()
    return {
        "id": b["id"], "name": b["name"], "note": b.get("note", ""), "deadline": b.get("deadline", ""),
        "opensAt": b.get("opensAt", ""),
        "ordering": ordering_status(state, b, now),
        "pickupDays": [day_label(d) for d in dates],
        "items": [{"id": i["id"], "name": i["name"], "description": i.get("description", ""), "price": i["price"],
                   "details": i.get("details", ""), "contains": i.get("contains", ""),
                   "image": i.get("image", ""), "remaining": remaining(state, b, i["id"], now)}
                  for i in bake_items(state, b)],
        "pickups": pickups,
    }


def public_pickups(state, bake, now):
    s = state["settings"]
    out = []
    for p in bake.get("pickups") or []:
        if not p.get("active"):
            continue
        full = p.get("capacity") is not None and pickup_taken(state, bake, p["id"], now) >= p["capacity"]
        wins = [{"id": w["id"], "label": window_label(w), "start": window_start(w),
                 "full": full or (w.get("capacity") is not None and window_taken(state, bake, w["id"], now) >= w["capacity"])}
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
    bake = find_bake(state, data.get("bakeId"))
    if not bake and not data.get("bakeId"):
        # A page loaded before Bakes existed sends no Bake: fine when only one is taking orders.
        live = [b for b in state["bakes"] if bake_phase(b, now) == "open"]
        bake = live[0] if len(live) == 1 else None
    if not bake:
        raise UserError("That preorder isn't available anymore. Please refresh the page.")
    status = ordering_status(state, bake, now)
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

    pickup = next((p for p in bake["pickups"] if p["id"] == data.get("pickupId") and p.get("active")), None)
    if not pickup:
        raise UserError("Please choose a pickup option.")
    if pickup.get("capacity") is not None and pickup_taken(state, bake, pickup["id"], now) >= pickup["capacity"]:
        raise UserError(pickup["label"] + " is full. Please choose another pickup option.")
    window = None
    if pickup.get("windows"):
        window = next((w for w in pickup["windows"] if w["id"] == data.get("pickupWindowId")), None)
        if not window:
            raise UserError("Please choose a pickup time.")
        if not window_open(window, s, now):
            raise UserError("That pickup time has passed. Please choose another.")
        if window.get("capacity") is not None and window_taken(state, bake, window["id"], now) >= window["capacity"]:
            raise UserError("That pickup time just filled up. Please choose another.")

    lines = []
    most = s.get("perOrderMax") or 12
    on_menu = {i["id"]: i for i in bake_items(state, bake)}
    cart = data.get("cart") if isinstance(data.get("cart"), list) else []
    for entry in cart:
        entry = entry if isinstance(entry, dict) else {}
        qty = to_int(entry.get("qty"))
        if qty <= 0:
            continue
        item = on_menu.get(entry.get("itemId"))
        if not item:
            raise UserError("Something in your order is no longer available. Please refresh the page.")
        if qty > most:
            raise UserError("You can order up to {} of each item. Need more? Contact Mollie.".format(most))
        left = remaining(state, bake, item["id"], now)
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
        "bakeId": bake["id"],
        "bakeName": bake["name"],
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
        "name": o["customer"]["name"], "email": o["customer"]["email"], "bakeName": o.get("bakeName", ""),
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

def _date_shift(date_text, days):
    try:
        d = dt.date(*[int(x) for x in date_text.split("-")])
    except (ValueError, TypeError, AttributeError):
        return date_text
    return (d + dt.timedelta(days=days)).isoformat()


def _time_shift(iso_text, days):
    ms = parse_ms(iso_text)
    return iso(ms + days * DAY_MS) if ms is not None else ""


def copy_bake(state, bake, days, now):
    """A new, switched-off Bake with the same menu, stock and pickups; every date moved by `days`."""
    b = copy.deepcopy(bake)
    b["id"] = uid(8)
    b["name"] = (bake["name"] + " (copy)")[:80]
    b["open"] = False
    b["createdAt"] = iso(now)
    b["opensAt"] = _time_shift(bake.get("opensAt"), days)
    b["deadline"] = _time_shift(bake.get("deadline"), days)
    for p in b["pickups"]:
        p["windows"] = sorted(({**w, "id": uid(6), "date": _date_shift(w.get("date"), days)} for w in p.get("windows") or []),
                              key=window_start)
    state["bakes"].append(b)
    return b


def bake_order_count(state, bake):
    return len([o for o in state["orders"] if o.get("bakeId") == bake["id"] and o["status"] in ("paid", "awaiting", "pending")])


def _need_bake(state, a):
    b = find_bake(state, a.get("bakeId"))
    if not b:
        raise UserError("That Bake wasn't found. Refresh the page and try again.")
    return b


def admin_action(state, a, now):
    """Every change Mollie makes goes through here."""
    s = state["settings"]
    action = a.get("action")
    if action == "saveSettings":
        v = a.get("settings") or {}
        for k in ["shopName", "tagline", "intro", "dietaryNote", "contactEmail", "contactNote",
                  "allergyPrompt", "thankYouNote", "appPayNote"]:
            if k in v:
                s[k] = clean(v[k], 1000)
        for k in ["venmo", "cashapp", "paypal"]:
            if k in v:
                s[k] = re.sub(r"\s+", "", clean(v[k], 80))
        for k in ["askPickupTime", "askAllergies", "appPayEnabled"]:
            if k in v:
                s[k] = bool(v[k])
        if "appPayDiscountPct" in v:
            s["appPayDiscountPct"] = min(10, max(0, to_num(v["appPayDiscountPct"]) or 0))
        if "appPayHours" in v:
            s["appPayHours"] = min(168, max(1, to_int(v["appPayHours"]) or 24))
        if "lowStockAt" in v:
            s["lowStockAt"] = min(999, max(0, to_int(v["lowStockAt"])))
        if "perOrderMax" in v:
            s["perOrderMax"] = min(99, max(1, to_int(v["perOrderMax"]) or 12))
        if not s.get("shopName"):
            s["shopName"] = "Bakery"
        return {}

    # ---- Bakes
    if action == "saveBake":
        v = a.get("bake") or {}
        b = find_bake(state, v.get("id")) if v.get("id") else None
        if v.get("id") and not b:
            raise UserError("That Bake wasn't found. Refresh the page and try again.")
        name = clean(v.get("name"), 80)
        if not name:
            raise UserError("Give it a name, like “Market – Sat, Dec 5” or “Christmas 2026”.")
        opens = parse_ms(v.get("opensAt")) if "opensAt" in v else parse_ms((b or {}).get("opensAt"))
        deadline = parse_ms(v.get("deadline")) if "deadline" in v else parse_ms((b or {}).get("deadline"))
        if opens is not None and deadline is not None and deadline <= opens:
            raise UserError("Orders close before they open. Check the two dates.")
        if not b:
            b = new_bake(name, now)
            state["bakes"].append(b)
        b["name"] = name
        if "note" in v:
            b["note"] = clean(v.get("note"), 1000)
        b["opensAt"] = iso(opens) if opens is not None else ""
        b["deadline"] = iso(deadline) if deadline is not None else ""
        if "open" in v:
            b["open"] = bool(v["open"])
        for o in state["orders"]:  # a renamed Bake renames its orders' label too
            if o.get("bakeId") == b["id"]:
                o["bakeName"] = name
        return {"id": b["id"]}

    if action == "setBakeOpen":
        b = _need_bake(state, a)
        b["open"] = bool(a.get("open"))
        return {}

    if action == "copyBake":
        b = _need_bake(state, a)
        days = min(366, max(0, to_int(a.get("days"))))
        return {"id": copy_bake(state, b, days, now)["id"]}

    if action == "deleteBake":
        b = _need_bake(state, a)
        if bake_order_count(state, b):
            raise UserError("This Bake has orders, so it can’t be deleted. Switch it off instead.")
        state["bakes"] = [x for x in state["bakes"] if x["id"] != b["id"]]
        return {}

    if action == "setBakeItem":  # tick an item on or off this Bake's menu, and/or set how many she can make
        b = _need_bake(state, a)
        item = find_item(state, a.get("itemId"))
        if not item:
            raise UserError("That item wasn't found.")
        if "on" in a:
            if a["on"] and item["id"] not in b["menu"]:
                b["menu"].append(item["id"])
            elif not a["on"]:
                b["menu"] = [x for x in b["menu"] if x != item["id"]]
        if "limit" in a:
            limit = opt_int(a.get("limit"))
            if limit is None:
                b["limits"].pop(item["id"], None)
            else:
                b["limits"][item["id"]] = limit
        return {}

    # ---- the Menu (everything she makes)
    if action == "saveItem":
        v = a.get("item") or {}
        num = to_num(re.sub(r"[$,]", "", str(v.get("price", ""))))
        price = js_round(num * 100) if num is not None else None
        if not clean(v.get("name")):
            raise UserError("Give the item a name.")
        if price is None or price < 50:
            raise UserError("Price needs to be at least $0.50 (a Stripe minimum).")
        item = find_item(state, v.get("id"))
        if not item:
            item = {"id": uid(6), "image": ""}
            state["items"].append(item)
        item["name"] = clean(v.get("name"), 80)
        item["description"] = clean(v.get("description"), 400)
        if "details" in v:
            item["details"] = clean(v.get("details"), 2000)
        if "contains" in v:
            # "coconut, eggs" (a leading "Contains:" is dropped so it isn't said twice)
            item["contains"] = re.sub(r"^contains\s*:?\s*", "", clean(v.get("contains"), 120), flags=re.I)
        item["price"] = price
        if "image" in v:
            item["image"] = v["image"] or ""
        return {"id": item["id"]}

    if action == "deleteItem":
        state["items"] = [i for i in state["items"] if i["id"] != a.get("id")]
        for b in state["bakes"]:
            b["menu"] = [x for x in b["menu"] if x != a.get("id")]
            b["limits"].pop(a.get("id"), None)
        return {}

    if action in ("moveItem", "movePickup"):
        lst = state["items"] if action == "moveItem" else _need_bake(state, a)["pickups"]
        i = next((n for n, x in enumerate(lst) if x["id"] == a.get("id")), -1)
        j = i + (-1 if (to_num(a.get("dir")) or 0) < 0 else 1)
        if i >= 0 and 0 <= j < len(lst):
            lst[i], lst[j] = lst[j], lst[i]
        return {}

    # ---- a Bake's pickup places and times
    if action == "savePickup":
        b = _need_bake(state, a)
        v = a.get("pickup") or {}
        if not clean(v.get("label")):
            raise UserError("Give the pickup option a name.")
        p = next((x for x in b["pickups"] if x["id"] == v.get("id")), None)
        if not p:
            # The same place used before keeps its identity, so customers' last choice still matches.
            pid = v.get("id") if v.get("id") and re.match(r"^[a-z0-9]{1,16}$", str(v.get("id"))) else uid(6)
            p = {"id": pid, "active": True}
            b["pickups"].append(p)
        p["label"] = clean(v.get("label"), 80)
        p["details"] = clean(v.get("details"), 200)
        p["capacity"] = opt_int(v.get("capacity"))
        if "windows" in v:
            p["windows"] = clean_windows(v.get("windows"), p.get("windows"))
        if "active" in v:
            p["active"] = bool(v["active"])
        return {"id": p["id"]}

    if action == "deletePickup":
        b = _need_bake(state, a)
        b["pickups"] = [p for p in b["pickups"] if p["id"] != a.get("id")]
        return {}

    # ---- orders
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


def admin_view(state, now):
    """What Mollie's admin page gets: the whole store, plus each Bake's status, stock left and order count."""
    out = copy.deepcopy(state)
    for b in out["bakes"]:
        b["phase"] = bake_phase(b, now)
        b["stock"] = {i: remaining(state, b, i, now) for i in b["menu"]}
        b["orderCount"] = len([o for o in state["orders"] if o.get("bakeId") == b["id"] and o["status"] == "paid"])
        b["firstPickup"] = first_pickup(b)
    return out


def summarize(state, bake_id, now=None):
    now = now or now_ms()
    s = state["settings"]
    awaiting = []
    for o in state["orders"]:
        if o.get("bakeId") == bake_id and o["status"] == "awaiting":
            x = copy.deepcopy(o)
            x["overdue"] = not holds(o, s, now)
            x["payBy"] = iso(hold_until(o, s))
            awaiting.append(x)
    orders = [o for o in state["orders"] if o.get("bakeId") == bake_id and o["status"] == "paid"]
    items, pickups = {}, []
    for o in orders:
        if o["pickupLabel"] not in pickups:
            pickups.append(o["pickupLabel"])
        for l in o["lines"]:
            row = items.setdefault(l["itemId"], {"name": l["name"], "total": 0, "byPickup": {}})
            row["total"] += l["qty"]
            row["byPickup"][o["pickupLabel"]] = row["byPickup"].get(o["pickupLabel"], 0) + l["qty"]
    return {"orders": orders, "awaiting": awaiting, "pickups": pickups, "bakeList": list(items.values()),
            "revenue": sum(o["total"] for o in orders)}


def orders_csv(state, bake_id):
    orders = sorted(summarize(state, bake_id)["orders"], key=lambda o: (o.get("pickupStart") or "~", o["number"]))
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
