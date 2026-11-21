"""Shipping labels through Shippo (https://goshippo.com), for the farm shop.

Uses only Python's built-in web tools, so nothing extra to install. Setting (Render > Environment):
  SHIPPO_API_KEY   shippo_test_... (free practice labels) or shippo_live_... (real postage, billed by Shippo)

Shippo's API: POST /shipments/ (addresses + parcel -> rates), POST /transactions/ (buy a rate -> label PDF + tracking),
GET /transactions/<id> (a fresh label link: links expire), POST /refunds/ (void an unused label).
Tests replace call() with a stand-in, so they never reach Shippo.
"""
import json
import os
import urllib.error
import urllib.request

BASE = "https://api.goshippo.com"
TIMEOUT = 30  # seconds; the server gives a page 60


class ShippoError(Exception):
    """Something Shippo refused or couldn't do; the message is fit to show staff."""


def key():
    return os.environ.get("SHIPPO_API_KEY", "").strip()


def mode():
    k = key()
    return "off" if not k else ("live" if k.startswith("shippo_live_") else "test")


def call(method, path, data=None):
    """One request to Shippo. Returns the JSON reply as a dict; raises ShippoError with a readable reason."""
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(BASE + path, data=body, method=method, headers={
        "Authorization": "ShippoToken " + key(), "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise ShippoError("Shippo said no ({}): {}".format(e.code, detail))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ShippoError("Couldn't reach Shippo: {}".format(e))


def messages(obj):
    """Shippo's own explanation (bad address, missing size...), as one line."""
    out = []
    for m in (obj or {}).get("messages") or []:
        t = m.get("text") if isinstance(m, dict) else str(m)
        if t and t not in out:
            out.append(t)
    return " ".join(out)


def rates(ship_from, ship_to, weight_oz, box):
    """Prices for one parcel. box = (length, width, height) in inches. Returns (shipment_id, [rate...]) cheapest first;
    each rate: id, amount (float), carrier, service, days, terms."""
    length, width, height = box
    shipment = call("POST", "/shipments/", {
        "address_from": ship_from, "address_to": ship_to, "async": False,
        "parcels": [{"length": "{:g}".format(length), "width": "{:g}".format(width), "height": "{:g}".format(height),
                     "distance_unit": "in", "weight": "{:g}".format(weight_oz), "mass_unit": "oz"}]})
    out = []
    for r in shipment.get("rates") or []:
        try:
            amount = float(r.get("amount"))
        except (TypeError, ValueError):
            continue
        level = r.get("servicelevel") or {}
        out.append({"id": r.get("object_id"), "amount": amount, "carrier": r.get("provider") or "",
                    "service": level.get("name") or level.get("token") or "", "days": r.get("estimated_days"),
                    "terms": r.get("duration_terms") or ""})
    out.sort(key=lambda x: x["amount"])
    if not out:
        raise ShippoError(messages(shipment) or "Shippo found no prices for this package. Check the address, weight and box size.")
    return shipment.get("object_id"), out


def buy(rate_id, label_size, order_code):
    """Buys the label. Returns dict: id, label_url, tracking, tracking_url. Raises ShippoError if it wasn't bought."""
    tx = call("POST", "/transactions/", {"rate": rate_id, "label_file_type": label_size, "async": False,
                                          "metadata": "Order {}".format(order_code)})
    if tx.get("status") != "SUCCESS" or not tx.get("label_url"):
        raise ShippoError(messages(tx) or "Shippo didn't make the label (status {}).".format(tx.get("status")))
    return {"id": tx.get("object_id"), "label_url": tx["label_url"], "tracking": tx.get("tracking_number") or "",
            "tracking_url": tx.get("tracking_url_provider") or ""}


def label_link(tx_id):
    """A fresh link to an existing label (Shippo's links expire after a while)."""
    tx = call("GET", "/transactions/{}".format(tx_id))
    if not tx.get("label_url"):
        raise ShippoError(messages(tx) or "Shippo has no label file for this one.")
    return tx["label_url"]


def void(tx_id):
    """Asks for the money back on an unused label. Returns Shippo's refund status (QUEUED / PENDING / SUCCESS / ERROR)."""
    r = call("POST", "/refunds/", {"transaction": tx_id, "async": False})
    status = r.get("status") or ""
    if status == "ERROR":
        raise ShippoError(messages(r) or "Shippo refused the refund (has the package already been scanned?).")
    return status
