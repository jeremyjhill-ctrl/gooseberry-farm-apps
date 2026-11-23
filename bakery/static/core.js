/*
 * The little bit of store math Mollie's admin page does in the browser: one Bake's paid orders,
 * its baking list (how many of each item, per pickup) and the pickup groups for the order list.
 * Every rule about what's for sale and whether an order is valid lives on the server (store.py).
 * Money is always whole cents.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.StoreCore = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // When an unpaid order stops holding its items.
  function holdUntil(order, settings) {
    const ms = order.status === 'awaiting' ? (settings.appPayHours || 24) * 3600000 : (settings.holdMinutes || 30) * 60000;
    return Date.parse(order.createdAt) + ms;
  }

  // An order counts against stock if it's paid, or if it's still waiting on payment (card checkout or app payment).
  function holds(order, settings, now) {
    if (order.status === 'paid') return true;
    if (order.status !== 'pending' && order.status !== 'awaiting') return false;
    return now < holdUntil(order, settings);
  }

  function slotOf(o) {
    return o.pickupWhen ? `${o.pickupWhen} · ${o.pickupLabel}` : o.pickupLabel;
  }

  // One Bake's orders plus its baking list.
  function summarize(state, bakeId, now) {
    now = now || Date.now();
    const mine = state.orders.filter((o) => o.bakeId === bakeId);
    const awaiting = mine
      .filter((o) => o.status === 'awaiting')
      .map((o) => Object.assign({}, o, { overdue: !holds(o, state.settings, now), payBy: new Date(holdUntil(o, state.settings)).toISOString() }));
    const orders = mine.filter((o) => o.status === 'paid');
    const items = {};
    for (const o of orders)
      for (const l of o.lines) {
        const row = (items[l.itemId] = items[l.itemId] || { name: l.name, total: 0, byPickup: {} });
        row.total += l.qty;
        row.byPickup[o.pickupLabel] = (row.byPickup[o.pickupLabel] || 0) + l.qty;
      }
    const pickups = [...new Set(orders.map((o) => o.pickupLabel))];
    // Order-list groups: one per pickup window (in time order), or per place when it has no windows.
    const key = (o) => o.pickupStart || '~';
    const slots = [...new Set(orders.slice().sort((a, b) => (key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0)).map(slotOf))];
    return { orders, awaiting, pickups, slots, bakeList: Object.values(items), revenue: orders.reduce((t, o) => t + o.total, 0) };
  }

  return { holdUntil, holds, slotOf, summarize };
});
