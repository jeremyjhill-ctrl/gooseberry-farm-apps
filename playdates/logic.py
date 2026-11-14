"""Booking rules for Farm Baby Playdates.

Everything here is plain Python with no web code, so the rules can be read
(and tested) on their own:

* pricing  -> price_lines(), tax_for()
* schedule -> windows_for(), day_status()
* crowding -> slot_options(), fits()
"""
import datetime as dt
import math

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Every setting the app knows about, with the starting value used the first time it runs.
DEFAULTS = {
    # season & schedule
    "season_start": "2027-03-19",
    "season_end": "2027-05-31",
    "open_days": ["Fri", "Sat", "Sun"],          # the days you usually open (what "Release through" picks)
    "weekday_hours": {                            # first arrival / close, by weekday; change in Pricing & schedule
        "Fri": ["15:30", "18:00"],                # after school
        "Sat": ["10:00", "17:00"],
        "Sun": ["12:00", "17:00"],                # later start on Sundays
    },
    "first_arrival": "10:00",                     # hours for any other day you release (spring break, holidays)
    "close_time": "17:00",
    "window_minutes": 15,
    "groups_per_window": 1,
    "gap_every": 3,
    "stay_minutes": 60,
    "max_on_farm": 25,
    "hold_minutes": 30,
    "reschedule_cutoff_hours": 48,
    "reminder_days": 1,
    "paused_all": False,
    "show_quick_add": True,  # test helper column on the Today page
    # wording guests see; {cutoff} becomes the reschedule cutoff in hours
    "policy_text": (
        "Your group gets one ticket, and everyone age 2 and up who comes onto the farm must be on it, including grown-ups "
        "who are just watching. Please count your whole group when you book. We'll always try to fit in extra people at the "
        "gate, but on busy days that can mean a wait. Adding them to your ticket online ahead is the only way to save their spot.\n\n"
        "Your ticket is good for your day and arrival window only. If you arrive early, we may ask you to wait until your "
        "window opens. If you arrive after your window, we'll try to fit you in, but we can't promise entry. Your spot may "
        "have gone to the next families.\n\n"
        "Tickets aren't refundable. You can move to another open time yourself from your ticket link up to {cutoff} "
        "hours before your window. Missed windows and no-shows aren't refunded, and rescheduling after that is up to the farm. "
        "If we close for weather, you'll pick a new day."
    ),
    "max_group_size": 25,              # biggest group guests can book online; bigger ones see big_group_message
    "big_group_message": "Bringing more than {max} people? Please contact us before booking so we can plan for your group. We'd also be happy to tell you about private parties.",
    "policy_short": "Your ticket is good for your arrival window only. Early arrivals may need to wait, and late arrivals aren't guaranteed entry.",
    # pricing (today's model: $70 covers up to 5 tickets, $10 each extra)
    "booking_fee": 0.0,
    "price_per_ticket": 0.0,
    "small_max": 0,
    "small_price": 0.0,
    "package_max": 5,
    "package_price": 70.0,
    "extra_price": 10.0,
    # tax: matches the Christian County rate on the farm's Square location
    "tax_rate": 6.475,
    "tax_mode": "added",  # "added" on top at checkout, or "included" in the price
}

PRESETS = {
    "package": {
        "name": "Family package (today)",
        "blurb": "$70 for up to 5 people, $10 each extra person",
        "values": {"booking_fee": 0, "price_per_ticket": 0, "small_max": 0, "small_price": 0,
                   "package_max": 5, "package_price": 70, "extra_price": 10},
    },
    "tiered": {
        "name": "Small party + package",
        "blurb": "$40 for 1-2 people, $70 up to 5, $10 each extra",
        "values": {"booking_fee": 0, "price_per_ticket": 0, "small_max": 2, "small_price": 40,
                   "package_max": 5, "package_price": 70, "extra_price": 10},
    },
    "per_ticket": {
        "name": "Per person + fee",
        "blurb": "$12 a person plus $10 to reserve",
        "values": {"booking_fee": 10, "price_per_ticket": 12, "small_max": 0, "small_price": 0,
                   "package_max": 0, "package_price": 0, "extra_price": 0},
    },
}


# ---------------------------------------------------------------- money

def money(x):
    return "${:,.2f}".format(x) if round(x, 2) != int(round(x, 2)) else "${:,.0f}".format(x)


def price_lines(tickets, s):
    """Return (lines, subtotal) for a number of tickets under settings s.

    All three pricing ideas reduce to the same seven numbers, which is why
    switching models is a settings change rather than a code change.
    """
    n = int(tickets)
    lines = []
    total = 0.0
    fee = float(s["booking_fee"])
    pp = float(s["price_per_ticket"])
    small_max, pkg_max = int(s["small_max"]), int(s["package_max"])
    small_price, pkg_price, extra = float(s["small_price"]), float(s["package_price"]), float(s["extra_price"])

    if fee:
        lines.append(("Reservation fee", fee)); total += fee
    if pp:
        lines.append(("{} {} × {}".format(n, "person" if n == 1 else "people", money(pp)), pp * n)); total += pp * n
    if n <= small_max:
        lines.append(("Small party (up to {} people)".format(small_max), small_price)); total += small_price
    elif n <= pkg_max:
        lines.append(("Playdate package (up to {} people)".format(pkg_max), pkg_price)); total += pkg_price
    elif pkg_max > 0:
        x = n - pkg_max
        lines.append(("Playdate package ({} people)".format(pkg_max), pkg_price))
        lines.append(("{} extra {} × {}".format(x, "person" if x == 1 else "people", money(extra)), extra * x))
        total += pkg_price + extra * x
    return lines, round(total, 2)


def subtotal_for(tickets, s):
    return price_lines(tickets, s)[1]


def tax_for(subtotal, s):
    """Return (tax, total). 'added' puts tax on top; 'included' backs it out of the price."""
    rate = float(s["tax_rate"]) / 100.0
    if s["tax_mode"] == "included":
        tax = round(subtotal - subtotal / (1 + rate), 2)
        return tax, round(subtotal, 2)
    tax = round(subtotal * rate, 2)
    return tax, round(subtotal + tax, 2)


def rule_text(s):
    parts = []
    small_max, pkg_max = int(s["small_max"]), int(s["package_max"])
    if small_max > 0:
        parts.append(("1 person" if small_max == 1 else "1-{} people".format(small_max)) + " " + money(float(s["small_price"])))
    if pkg_max > 0:
        rng = "{}-{} people".format(small_max + 1, pkg_max) if small_max > 0 else "Up to {} people".format(pkg_max)
        parts.append(rng + " " + money(float(s["package_price"])))
        if float(s["extra_price"]) > 0:
            parts.append("each extra person " + money(float(s["extra_price"])))
    if float(s["price_per_ticket"]) > 0:
        parts.append(money(float(s["price_per_ticket"])) + " per person")
    if float(s["booking_fee"]) > 0:
        parts.append("plus a " + money(float(s["booking_fee"])) + " reservation fee")
    text = " · ".join(parts) + "." if parts else "Free."
    if float(s["tax_rate"]) > 0:
        text += " Sales tax is added at checkout." if s["tax_mode"] == "added" else " Sales tax included."
    return text


# ---------------------------------------------------------------- time helpers

def to_min(hhmm):
    h, m = str(hhmm).split(":")
    return int(h) * 60 + int(m)


def from_min(m):
    return "{:02d}:{:02d}".format(m // 60, m % 60)


def nice_time(hhmm):
    m = to_min(hhmm)
    h, mm = m // 60, m % 60
    suffix = "AM" if h < 12 else "PM"
    h12 = h - 12 if h > 12 else (12 if h == 0 else h)
    return "{}:{:02d} {}".format(h12, mm, suffix)


def window_label(start, s):
    end = from_min(to_min(start) + int(s["window_minutes"]))
    a, b = nice_time(start), nice_time(end)
    if a[-2:] == b[-2:]:
        a = a[:-3]
    return "{}–{}".format(a, b)


def parse_date(d):
    return dt.date.fromisoformat(d)


def nice_date(d, long=False):
    day = parse_date(d) if isinstance(d, str) else d
    if long:
        return day.strftime("%A, %B ") + str(day.day)
    return day.strftime("%a, %b ") + str(day.day)


# ---------------------------------------------------------------- schedule

def windows_for(s):
    """All arrival windows in a day, including planned empty 'gap' windows."""
    out = []
    length = max(5, int(s["window_minutes"]))
    start, end = to_min(s["first_arrival"]), to_min(s["close_time"])
    gap_every = int(s["gap_every"])
    i = 0
    t = start
    while t + length <= end:
        gap = gap_every > 0 and (i % (gap_every + 1)) == gap_every
        out.append({"start": from_min(t), "gap": gap, "index": i})
        t += length
        i += 1
    return out


def day_status(d, s, stopped, today, live):
    """Return (open?, reason) for a date string. `live` is the set of dates you've released."""
    day = parse_date(d)
    if s.get("paused_all"):
        return False, "Booking is paused right now. Please check back soon."
    if d not in live:
        return False, "That day isn't open for booking."
    if day < today:
        return False, "That day has passed."
    if d in stopped:
        return False, "This day is full."
    return True, ""


def open_dates(s, stopped, today, live, limit=60):
    """Released days from today on, each marked bookable or not (a stopped day shows as full)."""
    out = []
    for d in sorted(x for x in live if x >= today.isoformat())[:limit]:
        ok, why = day_status(d, s, stopped, today, live)
        out.append({"date": d, "open": ok, "why": why})
    return out


def hours_for(d, s, plan_row=None):
    """(first arrival, close) for a date: that day's own hours, else its weekday's, else the general default."""
    if plan_row and plan_row.get("first_arrival") and plan_row.get("close_time"):
        return plan_row["first_arrival"], plan_row["close_time"], "custom"
    wd = WEEKDAYS[parse_date(d).weekday()]
    wh = (s.get("weekday_hours") or {}).get(wd)
    if wh and wh[0] and wh[1]:
        return wh[0], wh[1], "weekday"
    return s["first_arrival"], s["close_time"], "default"


def short_range(start, end):
    """'3:30-6 PM' style label for calendar cells."""
    def part(t):
        m = to_min(t); h = m // 60; mm = m % 60
        h12 = h - 12 if h > 12 else (12 if h == 0 else h)
        return "{}{}".format(h12, ":{:02d}".format(mm) if mm else "")
    a_pm, b_pm = to_min(start) >= 720, to_min(end) >= 720
    if a_pm == b_pm:
        return "{}–{} {}".format(part(start), part(end), "PM" if b_pm else "AM")
    return "{} AM–{} PM".format(part(start), part(end))


# ---------------------------------------------------------------- crowding

def on_farm_at(point, bookings, stay):
    """Tickets on the farm at minute `point`, assuming each group stays `stay` minutes."""
    return sum(b["tickets"] for b in bookings if to_min(b["window_start"]) <= point < to_min(b["window_start"]) + stay)


def fits(start, tickets, bookings, s, cap):
    """Can a group of `tickets` start at `start` without breaking either rule?

    1. Each arrival window holds at most `groups_per_window` groups.
    2. For the whole time this group is assumed to be here, the farm never goes
       over `cap` tickets, counting everyone who arrived earlier and is still here
       and everyone who arrives later while this group is still here.
    """
    stay = int(s["stay_minutes"])
    st = to_min(start)
    windows = {w["start"]: w["gap"] for w in windows_for(s)}
    if start not in windows:
        return False, "Not one of the arrival times"
    if windows[start]:
        return False, "That's a planned break"
    same_window = [b for b in bookings if b["window_start"] == start]
    if len(same_window) >= int(s["groups_per_window"]):
        return False, "Already booked"
    points = {st} | {to_min(b["window_start"]) for b in bookings if st <= to_min(b["window_start"]) < st + stay}
    for p in points:
        if on_farm_at(p, bookings, stay) + tickets > cap:
            return False, "Not available for a group your size"
    return True, ""


def slot_options(tickets, bookings, s, cap):
    """Arrival windows grouped by hour, each marked available or not, gaps left out."""
    hours = []
    for w in windows_for(s):
        if w["gap"]:
            continue
        ok, why = fits(w["start"], tickets, bookings, s, cap)
        hour = to_min(w["start"]) // 60
        if not hours or hours[-1]["hour"] != hour:
            hours.append({"hour": hour, "label": nice_time(from_min(hour * 60)).replace(":00", ""), "slots": []})
        hours[-1]["slots"].append({"start": w["start"], "label": nice_time(w["start"])[:-3], "ok": ok, "why": why})
    return hours


def timeline(bookings, s, cap):
    """Per-window view for the admin: who arrives, and how many are on the farm."""
    stay = int(s["stay_minutes"])
    rows = []
    wins = windows_for(s)
    known = {w["start"] for w in wins}
    # A booking can sit outside today's windows (a test group dropped on a break, or settings changed
    # after people booked). Give those times a row too, so nothing is ever counted but hidden.
    extra = sorted({b["window_start"] for b in bookings} - known)
    wins = sorted(wins + [{"start": x, "gap": False, "offgrid": True} for x in extra], key=lambda w: to_min(w["start"]))
    for w in wins:
        st = to_min(w["start"])
        arriving = [b for b in bookings if b["window_start"] == w["start"]]
        here = on_farm_at(st, bookings, stay)
        rows.append({
            "start": w["start"], "label": nice_time(w["start"]), "gap": w["gap"], "offgrid": w.get("offgrid", False),
            "arriving": arriving, "on_farm": here, "cap": cap,
            "pct": min(100, int(round(100.0 * here / cap))) if cap else 0,
        })
    return rows


def policy_paragraphs(s):
    text = str(s.get("policy_text", "")).replace("{cutoff}", str(s.get("reschedule_cutoff_hours", 24)))
    return [p.strip() for p in text.replace("\r", "").split("\n\n") if p.strip()]


def lanes_for(bookings, s):
    """Lay out a day's groups like a calendar: each lane holds groups that don't overlap in time."""
    stay = int(s["stay_minutes"])
    lane_ends, placed = [], []
    for b in sorted(bookings, key=lambda b: (b["window_start"], b["id"])):
        st = to_min(b["window_start"])
        for i, end in enumerate(lane_ends):
            if end <= st:
                lane_ends[i] = st + stay
                placed.append((b, i))
                break
        else:
            lane_ends.append(st + stay)
            placed.append((b, len(lane_ends) - 1))
    return placed, max(1, len(lane_ends))


def stay_windows(s):
    return int(math.ceil(int(s["stay_minutes"]) / float(max(5, int(s["window_minutes"])))))
