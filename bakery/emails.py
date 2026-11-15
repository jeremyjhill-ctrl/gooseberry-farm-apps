"""The two emails a bakery customer gets.

  reserved: a Venmo / Cash App / PayPal order is held; here's exactly how to pay.
  paid:     the order is paid (card, or Mollie tapped Mark paid); here's what and where to pick up.

Plain text, short, and written to be replied to: replies go to Mollie's inbox.
"""
import datetime as dt

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from . import store


def _when(iso_text, tz_name):
    ms = store.parse_ms(iso_text)
    if ms is None:
        return ""
    d = dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)
    try:
        d = d.astimezone(ZoneInfo(tz_name or "America/Chicago"))
    except Exception:  # no time zone data: show Central time by fixed offset rather than fail
        d = d.astimezone(dt.timezone(dt.timedelta(hours=-6)))
    return d.strftime("%a, %b %d at %I:%M %p").replace(" 0", " ").replace("at 0", "at ")


def _lines(o):
    out = ["  {} x {}   {}".format(l["qty"], l["name"], store.money(l["qty"] * l["price"])) for l in o["lines"]]
    if o.get("discount"):
        out.append("  App payment discount   -{}".format(store.money(o["discount"])))
    out.append("  Total   {}".format(store.money(o["total"])))
    return "\n".join(out)


def _pickup(o):
    text = "Pickup: {}".format(o["pickupLabel"])
    if o.get("pickupDetails"):
        text += "\n  {}".format(o["pickupDetails"])
    if o.get("pickupTime"):
        text += "\n  You said you'd come: {}".format(o["pickupTime"])
    return text


def compose(kind, o, s, order_url):
    shop = s.get("shopName") or "Mollie's Farmhouse Bakery"
    name = o["customer"]["name"].split(" ")[0] or o["customer"]["name"]
    if kind == "reserved":
        pay_by = _when(store.iso(store.hold_until(o, s)), s.get("timezone"))
        how = []
        for a in store.app_links(s, o):
            if a.get("copy"):
                how.append("  {}: send to {}".format(a["app"], a["handle"]))
            else:
                how.append("  {} ({}): {}".format(a["app"], a["handle"], a["url"]))
        subject = "Order #{} is reserved: please send {}".format(o["number"], store.money(o["total"]))
        body = "\n\n".join([
            "Hi {},".format(name),
            "Thanks for your order! It's reserved for you until {}. To finish it, send {} with any of these:".format(pay_by, store.money(o["total"])),
            "\n".join(how),
            'Please put "Order #{}" in the payment note so Mollie can match it up.'.format(o["number"]),
            "Your order:\n" + _lines(o),
            _pickup(o),
            "If it isn't paid by {}, the items go back on sale.".format(pay_by),
            "Your order page: {}".format(order_url),
            "Questions? Just reply to this email.\n{}".format(shop),
        ])
    else:
        subject = "Order #{} is confirmed - {}".format(o["number"], shop)
        body = "\n\n".join([
            "Hi {},".format(name),
            "Your order is paid and confirmed. Thank you!",
            "Your order:\n" + _lines(o),
            _pickup(o),
        ] + ([s["thankYouNote"]] if s.get("thankYouNote") else []) + [
            "Your order page: {}".format(order_url),
            "Questions? Just reply to this email.\n{}".format(shop),
        ])
    return subject, body
