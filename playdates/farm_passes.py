"""Passes, credits and gift codes, shared by the farm shop and the playdate booking app.

A pass is a code (shown as a QR) holding credits of one or more kinds. Staci defines the kinds
("credit types") in the shop admin; each kind says where it can be used:

  booking_role = "package"  covers the playdate package (up to package_max people) when booking online
  booking_role = "person"   covers one extra person when booking online
  at_farm      = 1          can be ticked off by staff at the farm (walk-in admission, flowers, coffee...)
  available    = 0          seasonal switch: "season's over" (e.g. flower picking in a drought)

Passes come from shop sales or are made by hand in the admin (donations, giveaways). Every use
is a row in shop_pass_uses (a returned credit is a negative row), so the history is never edited,
only added to. The tables live in the same database as the bookings, so the booking app can
use a pass inside the same transaction that takes the payment.
"""
import datetime as dt
import json
import secrets
import time
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Chicago")

CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L mix-ups

SCHEMA = """
CREATE TABLE IF NOT EXISTS shop_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shop_credit_types (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  value REAL DEFAULT 0,             -- what one credit is worth, for reports and "value" totals
  booking_role TEXT DEFAULT '',     -- '' | 'package' | 'person'
  at_farm INTEGER DEFAULT 0,
  available INTEGER DEFAULT 1,
  unavailable_note TEXT DEFAULT '',
  sort INTEGER DEFAULT 0,
  archived INTEGER DEFAULT 0,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS shop_pages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT NOT NULL,
  title TEXT NOT NULL,
  intro TEXT DEFAULT '',
  starts_at TEXT DEFAULT '',
  ends_at TEXT DEFAULT '',
  hidden INTEGER DEFAULT 0,
  sort INTEGER DEFAULT 0,
  archived INTEGER DEFAULT 0,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS shop_products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL DEFAULT 'item',   -- 'pass' (makes a pass with credits) | 'item'
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  photo TEXT DEFAULT '',
  photo_view TEXT DEFAULT '',          -- how the photo sits in its box: fit, position, zoom (see shop photo_view)
  custom_fields TEXT DEFAULT '',       -- up to 3 questions for the buyer, e.g. how to inscribe a book (JSON)
  price REAL NOT NULL DEFAULT 0,
  value_price REAL,                    -- "a $150 value"
  stock_limit INTEGER,                 -- blank = no limit
  per_order_limit INTEGER,
  hidden INTEGER DEFAULT 0,
  page_id INTEGER,                     -- sale page it belongs to (blank = the main shop)
  fulfillment TEXT DEFAULT 'pickup',   -- items: 'pickup' | 'ship' | 'both' (buyer chooses) | 'none', passes: 'pass'
  taxable INTEGER DEFAULT 1,
  season_label TEXT DEFAULT '',        -- passes: "Good during the 2027 season"
  valid_until TEXT DEFAULT '',         -- passes: last day credits work (YYYY-MM-DD)
  terms TEXT DEFAULT '',               -- extra wording the buyer agrees to
  sort INTEGER DEFAULT 0,
  archived INTEGER DEFAULT 0,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS shop_product_credits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER NOT NULL,
  credit_type_id INTEGER NOT NULL,
  qty INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS shop_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL,
  status TEXT NOT NULL,                -- pending | paid | cancelled | expired
  name TEXT, email TEXT, phone TEXT,
  ship_name TEXT, ship_line1 TEXT, ship_line2 TEXT, ship_city TEXT, ship_state TEXT, ship_zip TEXT,
  subtotal REAL DEFAULT 0, shipping REAL DEFAULT 0, tax REAL DEFAULT 0, total REAL DEFAULT 0,
  method TEXT, agreed_terms TEXT, agreed_at TEXT, note TEXT,
  created_at TEXT, hold_expires TEXT, paid_at TEXT, fulfilled_at TEXT, fulfilled_by TEXT,
  hold_ip TEXT
);
CREATE TABLE IF NOT EXISTS shop_order_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL,
  product_id INTEGER,
  kind TEXT, name TEXT, unit_price REAL, qty INTEGER,
  fulfillment TEXT, taxable INTEGER, gift_for TEXT,
  answers TEXT DEFAULT ''              -- the buyer's answers to the product's questions (JSON)
);
CREATE TABLE IF NOT EXISTS shop_passes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL,
  order_id INTEGER, product_id INTEGER,
  title TEXT NOT NULL,
  holder TEXT DEFAULT '', gift_for TEXT DEFAULT '', email TEXT DEFAULT '',
  season_label TEXT DEFAULT '', valid_until TEXT DEFAULT '',
  note TEXT DEFAULT '',                -- private (e.g. "Rogersville Elementary auction")
  source TEXT DEFAULT 'sale',          -- sale | made
  void INTEGER DEFAULT 0,
  created_at TEXT, created_by TEXT
);
CREATE TABLE IF NOT EXISTS shop_pass_credits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pass_id INTEGER NOT NULL,
  credit_type_id INTEGER NOT NULL,
  name TEXT NOT NULL,                  -- as sold, so renaming a credit type later doesn't rewrite old passes
  qty INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS shop_pass_uses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pass_id INTEGER NOT NULL,
  credit_type_id INTEGER NOT NULL,
  qty INTEGER NOT NULL,                -- positive = used, negative = given back
  where_used TEXT,                     -- booking | farm | returned | undo
  booking_id INTEGER,
  by_whom TEXT, note TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS shop_pass_holds (
  booking_id INTEGER PRIMARY KEY,      -- credits picked at checkout, used when the booking is paid
  pass_id INTEGER NOT NULL,
  detail TEXT NOT NULL,                -- {"credit_type_id": qty}
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_shop_passes_code ON shop_passes(code);
CREATE INDEX IF NOT EXISTS ix_shop_uses_pass ON shop_pass_uses(pass_id);
CREATE INDEX IF NOT EXISTS ix_shop_orders_code ON shop_orders(code)
"""

STARTER_TYPES = [
    # (name, value, booking_role, at_farm)
    ("Playdate (up to 5 people)", 70, "package", 0),
    ("Extra person / walk-in admission", 10, "person", 1),
]


def stamp():
    return dt.datetime.now(TZ).replace(microsecond=0).isoformat()


def today_iso():
    return dt.datetime.now(TZ).date().isoformat()


def ensure_schema(con):
    """Create the shop tables if they're new, and two starter credit types the first time."""
    con.executescript(SCHEMA)
    add_column(con, "shop_products", "photo_view", "TEXT DEFAULT ''")  # added Nov 20, 2026
    add_column(con, "shop_products", "custom_fields", "TEXT DEFAULT ''")  # added Nov 20, 2026
    add_column(con, "shop_order_lines", "answers", "TEXT DEFAULT ''")  # added Nov 20, 2026
    if not con.execute("SELECT 1 FROM shop_settings WHERE key='seeded'").fetchone():
        if not con.execute("SELECT 1 FROM shop_credit_types").fetchone():
            for i, (name, value, role, farm) in enumerate(STARTER_TYPES):
                con.execute("INSERT INTO shop_credit_types(name, value, booking_role, at_farm, sort, created_at) VALUES (?,?,?,?,?,?)",
                            (name, value, role, farm, i, stamp()))
        con.execute("INSERT OR IGNORE INTO shop_settings(key, value) VALUES ('seeded', 'true')")
    if hasattr(con, "commit"):
        con.commit()


def add_column(con, table, column, decl):
    """Adds a column to a table made by an earlier version (works on SQLite and Postgres)."""
    try:
        con.execute("SELECT {} FROM {} WHERE 1=0".format(column, table)).fetchall()
    except Exception:  # noqa: BLE001  the column isn't there yet
        if hasattr(con, "rollback"):
            try:
                con.rollback()
            except Exception:  # noqa: BLE001
                pass
        con.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, column, decl))
        if hasattr(con, "commit"):
            con.commit()


def new_code(con, table="shop_passes"):
    while True:
        code = "".join(secrets.choice(CODE_CHARS) for _ in range(8))
        if not con.execute("SELECT 1 FROM {} WHERE code=?".format(table), (code,)).fetchone():
            return code


def clean_code(text):
    return "".join(ch for ch in (text or "").upper() if ch.isalnum())


def pretty_code(code):
    return "{}-{}".format(code[:4], code[4:]) if code and len(code) == 8 else code


# ---------------------------------------------------------------- reading a pass

def find_pass(con, code):
    code = clean_code(code)
    if len(code) != 8:
        return None
    r = con.execute("SELECT * FROM shop_passes WHERE code=?", (code,)).fetchone()
    return dict(r) if r else None


def pass_problem(p, today=None):
    """Why a pass can't be used right now, or '' if it can."""
    if not p:
        return "We couldn't find that code. Check the letters and try again."
    if p["void"]:
        return "That pass is no longer active. Please contact us if you think that's a mistake."
    today = today or today_iso()
    if p.get("valid_until") and today > p["valid_until"]:
        return "That pass was good through {}.".format(nice_day(p["valid_until"]))
    return ""


def credits(con, pass_id):
    """Each kind of credit on a pass, with how many are left and where it can be used."""
    rows = con.execute("""
        SELECT pc.credit_type_id, MIN(pc.name) AS name, SUM(pc.qty) AS qty,
               MIN(ct.booking_role) AS booking_role, MIN(ct.at_farm) AS at_farm, MIN(ct.available) AS available,
               MIN(ct.unavailable_note) AS unavailable_note, MIN(ct.value) AS value, MIN(pc.id) AS first_row,
               COALESCE((SELECT SUM(u.qty) FROM shop_pass_uses u
                          WHERE u.pass_id = ? AND u.credit_type_id = pc.credit_type_id), 0) AS used
        FROM shop_pass_credits pc LEFT JOIN shop_credit_types ct ON ct.id = pc.credit_type_id
        WHERE pc.pass_id = ? GROUP BY pc.credit_type_id ORDER BY MIN(pc.id)""", (pass_id, pass_id))
    out = []
    for r in rows:
        c = dict(r)
        c["qty"] = int(c["qty"] or 0)
        c["used"] = int(c["used"] or 0)
        c["left"] = max(0, c["qty"] - c["used"])
        c["booking_role"] = c.get("booking_role") or ""
        c["at_farm"] = 1 if c.get("at_farm") else 0
        c["available"] = 0 if c.get("available") in (0, False) else 1
        out.append(c)
    return out


def history(con, pass_id):
    rows = con.execute("""
        SELECT u.*, COALESCE(pc.name, ct.name, 'credit') AS credit_name, b.code AS booking_code, b.date AS booking_date,
               b.window_start AS booking_start
        FROM shop_pass_uses u
        LEFT JOIN shop_credit_types ct ON ct.id = u.credit_type_id
        LEFT JOIN (SELECT pass_id, credit_type_id, MIN(name) AS name FROM shop_pass_credits GROUP BY pass_id, credit_type_id) pc
               ON pc.pass_id = u.pass_id AND pc.credit_type_id = u.credit_type_id
        LEFT JOIN bookings b ON b.id = u.booking_id
        WHERE u.pass_id = ? ORDER BY u.id DESC""", (pass_id,))
    return [dict(r) for r in rows]


def nice_day(iso):
    try:
        d = dt.date.fromisoformat(iso[:10])
    except (TypeError, ValueError):
        return iso or ""
    return d.strftime("%B ") + str(d.day) + d.strftime(", %Y")


# ---------------------------------------------------------------- using a pass for a playdate booking

def cover_size(s):
    return int(s.get("package_max") or 0) or 5


def booking_choices(con, p, tickets, s):
    """What a pass can cover for a group of `tickets`: limits for each credit kind."""
    out = []
    extras_possible = max(0, int(tickets) - cover_size(s))
    package_taken = False
    for c in credits(con, p["id"]):
        if not c["available"] or c["left"] <= 0:
            continue
        if c["booking_role"] == "package":
            limit = 0 if package_taken else min(1, c["left"])
            package_taken = package_taken or limit > 0
        elif c["booking_role"] == "person":
            limit = min(c["left"], extras_possible)
        else:
            continue
        if limit > 0:
            out.append(dict(c, limit=limit))
    return out


def default_detail(choices):
    return {str(c["credit_type_id"]): c["limit"] for c in choices}


def credit_value(s, tickets, package_used, persons_used, subtotal_for):
    """How much of a booking's price the credits cover.

    A package credit covers the price for up to package_max people; each person credit covers one
    more person on top (the price difference of the last people in the group). Works the same under
    every pricing preset, so passes keep working if the pricing changes.
    """
    n = int(tickets)
    full = subtotal_for(n, s)
    covered = 0.0
    if package_used:
        covered += subtotal_for(min(n, cover_size(s)), s)
    p = max(0, min(int(persons_used), max(0, n - cover_size(s))))
    if p:
        covered += subtotal_for(n, s) - subtotal_for(n - p, s)
    return round(min(full, covered), 2)


def held(con, booking_id):
    r = con.execute("SELECT * FROM shop_pass_holds WHERE booking_id=?", (booking_id,)).fetchone()
    if not r:
        return None
    r = dict(r)
    r["detail"] = {k: int(v) for k, v in json.loads(r["detail"]).items() if int(v) > 0}
    p = con.execute("SELECT * FROM shop_passes WHERE id=?", (r["pass_id"],)).fetchone()
    r["pass"] = dict(p) if p else None
    return r


def hold(con, booking_id, pass_id, detail):
    con.execute("INSERT OR REPLACE INTO shop_pass_holds(booking_id, pass_id, detail, created_at) VALUES (?,?,?,?)",
                (booking_id, pass_id, json.dumps({str(k): int(v) for k, v in detail.items()}), stamp()))


def release(con, booking_id):
    con.execute("DELETE FROM shop_pass_holds WHERE booking_id=?", (booking_id,))


def booking_discount(con, b, s, subtotal_for):
    """(amount, label, hold) for the credits picked at checkout, after re-checking them."""
    h = held(con, b["id"])
    if not h or not h["pass"] or pass_problem(h["pass"]):
        return 0.0, "", None
    choices = {str(c["credit_type_id"]): c for c in booking_choices(con, h["pass"], b["tickets"], s)}
    detail = {k: min(v, choices[k]["limit"]) for k, v in h["detail"].items() if k in choices}
    pkg = sum(v for k, v in detail.items() if choices[k]["booking_role"] == "package")
    per = sum(v for k, v in detail.items() if choices[k]["booking_role"] == "person")
    amount = credit_value(s, b["tickets"], pkg > 0, per, subtotal_for)
    if amount <= 0:
        return 0.0, "", None
    bits = []
    if pkg:
        bits.append("playdate")
    if per:
        bits.append("{} extra {}".format(per, "person" if per == 1 else "people"))
    label = "Pass {} ({})".format(pretty_code(h["pass"]["code"]), " + ".join(bits))
    h["detail"] = detail
    return amount, label, h


def consume(con, b, s, subtotal_for, by_whom="guest"):
    """Use the held credits for a booking. Call inside the payment's transaction.

    Returns (amount_covered, error). On error nothing is written and the caller should roll back.
    """
    amount, label, h = booking_discount(con, b, s, subtotal_for)
    if not h:
        return 0.0, ""
    left = {str(c["credit_type_id"]): c["left"] for c in credits(con, h["pass"]["id"])}
    for k, v in h["detail"].items():
        if left.get(k, 0) < v:
            return 0.0, "Your pass doesn't have enough credits left for this booking anymore. Remove it and try again."
    now = stamp()
    for k, v in h["detail"].items():
        con.execute("INSERT INTO shop_pass_uses(pass_id, credit_type_id, qty, where_used, booking_id, by_whom, note, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)", (h["pass"]["id"], int(k), v, "booking", b["id"], by_whom, "", now))
    release(con, b["id"])
    return amount, ""


def booking_uses(con, booking_id):
    """Credits a booking used, net of any given back: [{code, pass_id, credit_type_id, name, qty}]."""
    rows = con.execute("""
        SELECT p.code, p.id AS pass_id, u.credit_type_id, SUM(u.qty) AS qty,
               (SELECT MIN(pc.name) FROM shop_pass_credits pc WHERE pc.pass_id = p.id AND pc.credit_type_id = u.credit_type_id) AS name
        FROM shop_pass_uses u JOIN shop_passes p ON p.id = u.pass_id
        WHERE u.booking_id = ? GROUP BY p.code, p.id, u.credit_type_id""", (booking_id,))
    return [dict(r, name=r["name"] or "credit") for r in rows if int(r["qty"] or 0) > 0]


def give_back(con, booking_id, by_whom, note):
    """The farm cancelled a booking: put its credits back on the pass."""
    n = 0
    for u in booking_uses(con, booking_id):
        con.execute("INSERT INTO shop_pass_uses(pass_id, credit_type_id, qty, where_used, booking_id, by_whom, note, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)", (u["pass_id"], u["credit_type_id"], -int(u["qty"]), "returned", booking_id,
                                                  by_whom, note, stamp()))
        n += int(u["qty"])
    release(con, booking_id)
    return n


# ---------------------------------------------------------------- at the farm

def use_at_farm(con, p, credit_type_id, qty, by_whom):
    """Staff tick-off at the farm. Returns an error message or ''."""
    problem = pass_problem(p)
    if problem:
        return problem
    qty = int(qty)
    c = next((c for c in credits(con, p["id"]) if c["credit_type_id"] == int(credit_type_id)), None)
    if not c or qty <= 0:
        return "Nothing to use."
    if not c["at_farm"]:
        return "{} is used when booking online, not at the gate.".format(c["name"])
    if not c["available"]:
        return "{}: {}".format(c["name"], c["unavailable_note"] or "not available right now.")
    if c["left"] < qty:
        return "Only {} {} left.".format(c["left"], c["name"])
    con.execute("INSERT INTO shop_pass_uses(pass_id, credit_type_id, qty, where_used, by_whom, note, created_at) VALUES (?,?,?,?,?,?,?)",
                (p["id"], c["credit_type_id"], qty, "farm", by_whom, "", stamp()))
    return ""


def undo_use(con, p, use_id, by_whom):
    u = con.execute("SELECT * FROM shop_pass_uses WHERE id=? AND pass_id=?", (use_id, p["id"])).fetchone()
    if not u or u["where_used"] != "farm":
        return "Only tick-offs at the farm can be undone here."
    if con.execute("SELECT 1 FROM shop_pass_uses WHERE pass_id=? AND where_used='undo' AND note=?", (p["id"], "undo #{}".format(use_id))).fetchone():
        return "That one was already undone."
    con.execute("INSERT INTO shop_pass_uses(pass_id, credit_type_id, qty, where_used, by_whom, note, created_at) VALUES (?,?,?,?,?,?,?)",
                (p["id"], u["credit_type_id"], -int(u["qty"]), "undo", by_whom, "undo #{}".format(use_id), stamp()))
    return ""


# ---------------------------------------------------------------- making passes

def create_pass(con, title, credit_rows, holder="", gift_for="", email="", season_label="", valid_until="",
                note="", source="sale", order_id=None, product_id=None, by_whom=""):
    """credit_rows: [(credit_type_id, qty)]. Returns the new pass (dict)."""
    code = new_code(con)
    cur = con.execute("INSERT INTO shop_passes(code, order_id, product_id, title, holder, gift_for, email, season_label, valid_until, "
                      "note, source, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (code, order_id, product_id, title, holder, gift_for, email, season_label, valid_until, note, source,
                       stamp(), by_whom))
    pid = cur.lastrowid
    names = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM shop_credit_types")}
    for ct, qty in credit_rows:
        if int(qty) > 0 and int(ct) in names:
            con.execute("INSERT INTO shop_pass_credits(pass_id, credit_type_id, name, qty) VALUES (?,?,?,?)",
                        (pid, int(ct), names[int(ct)], int(qty)))
    return dict(con.execute("SELECT * FROM shop_passes WHERE id=?", (pid,)).fetchone())


def outstanding(con, today=None):
    """Credits sold or given but not used yet, on passes that still work, by credit type."""
    today = today or today_iso()
    active = "SELECT id FROM shop_passes WHERE void = 0 AND (valid_until = '' OR valid_until IS NULL OR valid_until >= ?)"
    out = []
    for ct in con.execute("SELECT id, name, value, archived FROM shop_credit_types ORDER BY sort, id").fetchall():
        issued = con.execute("SELECT COALESCE(SUM(qty), 0) FROM shop_pass_credits WHERE credit_type_id = ? AND pass_id IN ({})".format(active),
                             (ct["id"], today)).fetchone()[0]
        used = con.execute("SELECT COALESCE(SUM(qty), 0) FROM shop_pass_uses WHERE credit_type_id = ? AND pass_id IN ({})".format(active),
                           (ct["id"], today)).fetchone()[0]
        issued, used = int(issued or 0), int(used or 0)
        if issued or not ct["archived"]:
            out.append({"id": ct["id"], "name": ct["name"], "value": ct["value"] or 0, "issued": issued, "used": used, "left": issued - used})
    return out


# ---------------------------------------------------------------- hooks into the booking app

WRONG_TRIES = {}  # ip -> recent wrong-code times (stops people guessing codes)


def too_many_wrong(ip):
    recent = [t for t in WRONG_TRIES.get(ip, []) if time.time() - t < 900]
    WRONG_TRIES[ip] = recent
    return len(recent) >= 8


def note_wrong(ip):
    WRONG_TRIES.setdefault(ip, []).append(time.time())


def register(pd):
    """Adds pass codes and the hide-the-booking-page switch to the playdate app (module `pd`)."""
    from flask import flash, redirect, render_template, request, session, url_for

    app = pd.app
    with app.app_context():
        con = pd.dbcompat.connect(pd.DB_PATH)
        ensure_schema(con)
        con.close()

    def pass_panel(b):
        """What the checkout page shows about a pass for this booking."""
        s = pd.get_settings()
        con = pd.db()
        h = held(con, b["id"])
        if not h or not h["pass"]:
            return None
        p = h["pass"]
        choices = booking_choices(con, p, b["tickets"], s)
        amount, label, _ = booking_discount(con, b, s, pd.logic.subtotal_for)
        return {"pass": p, "code": pretty_code(p["code"]), "choices": choices, "detail": h["detail"],
                "amount": amount, "label": label, "problem": pass_problem(p), "all": credits(con, p["id"])}

    def pass_uses_for(b):
        return [dict(u, code=pretty_code(u["code"])) for u in booking_uses(pd.db(), b["id"])]

    def shop_admin_url():
        """Where the farm shop's admin lives: its own address online, /shop/ on the Mac."""
        import os
        if os.environ.get("SHOP_URL"):
            return os.environ["SHOP_URL"].rstrip("/") + "/admin"
        host = request.host.split(":")[0]
        if host.startswith("booking."):
            return "https://shop." + host[len("booking."):] + "/admin"
        return request.host_url + "shop/admin"

    app.jinja_env.globals.update(pass_panel=pass_panel, pass_uses_for=pass_uses_for, pretty_code=pretty_code,
                                 shop_admin_url=shop_admin_url, booking_hidden_default=BOOKING_HIDDEN_MESSAGE)

    def holdable(code):
        b = pd.booking_by_code(code)
        return b if b["status"] == "held" else None

    @app.post("/checkout/<code>/pass", endpoint="apply_pass")
    def apply_pass(code):
        b = holdable(code)
        if not b:
            return redirect(url_for("checkout", code=code))
        ip = request.remote_addr or ""
        if too_many_wrong(ip):
            flash("Too many codes that didn't work. Please wait 15 minutes and try again.")
            return redirect(url_for("checkout", code=code) + "#pass")
        con = pd.db()
        p = find_pass(con, request.form.get("pass_code", ""))
        problem = pass_problem(p)
        if not p:
            note_wrong(ip)
        if problem:
            flash(problem)
            return redirect(url_for("checkout", code=code) + "#pass")
        choices = booking_choices(con, p, b["tickets"], pd.get_settings())
        if not choices:
            flash("That pass doesn't have any credits that work for this booking.")
            return redirect(url_for("checkout", code=code) + "#pass")
        hold(con, b["id"], p["id"], default_detail(choices))
        return redirect(url_for("checkout", code=code) + "#pass")

    @app.post("/checkout/<code>/pass/adjust", endpoint="adjust_pass")
    def adjust_pass(code):
        b = holdable(code)
        if not b:
            return redirect(url_for("checkout", code=code))
        con = pd.db()
        h = held(con, b["id"])
        if h and h["pass"]:
            choices = booking_choices(con, h["pass"], b["tickets"], pd.get_settings())
            detail = {}
            for c in choices:
                try:
                    v = int(request.form.get("use_{}".format(c["credit_type_id"]), 0))
                except ValueError:
                    v = 0
                detail[str(c["credit_type_id"])] = max(0, min(v, c["limit"]))
            hold(con, b["id"], h["pass"]["id"], detail)
        return redirect(url_for("checkout", code=code) + "#pass")

    @app.post("/checkout/<code>/pass/remove", endpoint="remove_pass")
    def remove_pass(code):
        b = pd.booking_by_code(code)
        release(pd.db(), b["id"])
        return redirect(url_for("checkout", code=code) + "#pass")

    # -------- hide the booking page from guests (admins still see it, with a banner)
    GUEST_PAGES = {"home", "book", "hold"}

    @app.before_request
    def hidden_booking_page():
        if request.endpoint not in GUEST_PAGES or session.get("admin"):
            return None
        s = pd.get_settings()
        if not s.get("booking_hidden"):
            return None
        return render_template("message.html", title=s.get("booking_hidden_title") or "Playdate booking isn't open yet",
                               body=s.get("booking_hidden_message") or BOOKING_HIDDEN_MESSAGE)

    @app.post("/admin/booking-visibility", endpoint="admin_booking_visibility")
    @pd.admin_required
    def admin_booking_visibility():
        hide = request.form.get("hidden") == "1"
        pd.save_setting("booking_hidden", hide)
        if "message" in request.form:
            pd.save_setting("booking_hidden_message", request.form.get("message", "").strip() or BOOKING_HIDDEN_MESSAGE)
        flash("The booking page is now hidden from guests. You can still see it while you're logged in." if hide
              else "The booking page is visible to guests again.")
        nxt = request.form.get("next") or ""
        return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("admin_settings"))


BOOKING_HIDDEN_MESSAGE = ("We're getting ready for the new season. Booking opens soon: follow us on Facebook and "
                          "Instagram so you're the first to know.")
