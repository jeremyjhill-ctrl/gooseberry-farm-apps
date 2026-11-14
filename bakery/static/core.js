/*
 * Store logic shared by the server (Node) and the in-browser demo.
 * Everything that decides "what's for sale, what's left, is this order valid"
 * lives here so the real site and the demo preview behave the same way.
 * Money is always whole cents.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.StoreCore = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function uid(n) {
    n = n || 12;
    const abc = 'abcdefghjkmnpqrstuvwxyz23456789';
    const bytes = new Uint8Array(n);
    globalThis.crypto.getRandomValues(bytes);
    let s = '';
    for (let i = 0; i < n; i++) s += abc[bytes[i] % abc.length];
    return s;
  }

  function defaultState() {
    return {
      settings: {
        shopName: "Mollie's Farmhouse Bakery",
        season: 'Thanksgiving 2026',
        tagline: 'Gluten-free holiday baking from Gooseberry Bridge Farm',
        intro:
          'Preorder for pickup on Wednesday, November 25th. Order by November 21st, or until sold out.',
        dietaryNote:
          'Everything is gluten-free, dairy-free, soy-free, seed-oil-free and free of artificial dyes, made in a 100% gluten-free kitchen.',
        contactEmail: 'mollie@gooseberrybridge.com',
        contactNote: "Questions? Email mollie@gooseberrybridge.com or message Mollie's Farmhouse Bakery on Facebook.",
        orderingOpen: true,
        deadline: '2026-11-21T23:59:00-06:00',
        timezone: 'America/Chicago',
        holdMinutes: 30,
        perOrderMax: 12,
        askPickupTime: true,
        askAllergies: true,
        allergyPrompt:
          "Any allergies besides dairy and wheat/gluten? Mollie will reach out if she can't make a modification.",
        thankYouNote: "Thank you! Mollie will have your order ready at pickup. Watch your email for your receipt.",
        // Pay-by-app (Venmo / Cash App / PayPal): order is reserved, customer sends payment, Mollie marks it paid.
        appPayEnabled: true,
        appPayDiscountPct: 3,
        appPayHours: 24,
        venmo: 'molliesfarmhousebakery',
        cashapp: 'molliesbakery',
        paypal: 'mollie@gooseberrybridge.com',
        appPayNote: 'Please put your order number in the payment note so Mollie can match it up.'
      },
      items: [
        { id: 'cinn', name: 'Take & Bake Cinnamon Rolls', description: '6 per order. Frozen, with baking instructions.', price: 1400, image: '', limit: null, active: true },
        { id: 'dinr', name: 'Take & Bake Dinner Rolls', description: '6 per order. Frozen, with baking instructions.', price: 1200, image: '', limit: null, active: true },
        { id: 'pump', name: 'Pumpkin Pie', description: 'Whole 9-inch pie.', price: 3500, image: '', limit: null, active: true },
        { id: 'pmps', name: 'Pumpkin Pie, 2 Slices', description: 'Two generous slices.', price: 1400, image: '', limit: null, active: true },
        { id: 'choc', name: 'Chocolate Pie', description: 'Whole 9-inch pie. Egg-free.', price: 3500, image: '', limit: null, active: true },
        { id: 'chcs', name: 'Chocolate Pie, 2 Slices', description: 'Two generous slices. Egg-free.', price: 1400, image: '', limit: null, active: true }
      ],
      pickups: [
        { id: 'farm', label: 'Gooseberry Bridge Farm', details: '177 Elm Grove Rd, Rogersville. Wed, Nov 25, 9am–2pm', capacity: null, active: true },
        { id: 'fmo', label: 'Farmers Market of the Ozarks', details: 'FMO Holiday Market, Springfield. Wed, Nov 25, 3–7pm', capacity: null, active: true }
      ],
      orders: [],
      nextOrderNumber: 1001
    };
  }

  // ---------- helpers ----------
  const clean = (v, max) => String(v == null ? '' : v).trim().slice(0, max || 500);
  const toInt = (v) => {
    const n = Math.floor(Number(v));
    return Number.isFinite(n) ? n : 0;
  };
  const optInt = (v) => (v === '' || v == null ? null : Math.max(0, toInt(v)));

  function userError(msg) {
    const e = new Error(msg);
    e.userFacing = true;
    return e;
  }

  function money(cents) {
    return '$' + (cents / 100).toFixed(cents % 100 === 0 ? 0 : 2);
  }

  // Fills in settings added after a store was first created.
  function normalize(state) {
    state.settings = Object.assign(defaultState().settings, state.settings);
    return state;
  }

  const APPS = [
    { key: 'venmo', label: 'Venmo' },
    { key: 'cashapp', label: 'Cash App' },
    { key: 'paypal', label: 'PayPal' }
  ];
  const appsOn = (s) => APPS.filter((a) => s[a.key]);
  const appPayOpen = (s) => !!s.appPayEnabled && appsOn(s).length > 0;

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

  function reserved(state, itemId, now) {
    let n = 0;
    for (const o of state.orders) {
      if (o.season !== state.settings.season || !holds(o, state.settings, now)) continue;
      for (const l of o.lines) if (l.itemId === itemId) n += l.qty;
    }
    return n;
  }

  function remaining(state, item, now) {
    if (item.limit == null) return null; // unlimited
    return Math.max(0, item.limit - reserved(state, item.id, now));
  }

  function pickupTaken(state, pickupId, now) {
    return state.orders.filter(
      (o) => o.season === state.settings.season && o.pickupId === pickupId && holds(o, state.settings, now)
    ).length;
  }

  function orderingStatus(state, now) {
    const s = state.settings;
    if (!s.orderingOpen) return { open: false, reason: 'Preorders are closed right now.' };
    if (s.deadline && now > Date.parse(s.deadline)) return { open: false, reason: 'The preorder deadline has passed.' };
    const forSale = state.items.filter((i) => i.active);
    if (!forSale.length) return { open: false, reason: 'Nothing is on the menu right now. Check back soon!' };
    if (forSale.every((i) => remaining(state, i, now) === 0)) return { open: false, reason: "We're sold out. Thank you!" };
    return { open: true };
  }

  // ---------- customer side ----------
  function publicShop(state, now) {
    const s = state.settings;
    return {
      settings: {
        shopName: s.shopName, season: s.season, tagline: s.tagline, intro: s.intro,
        dietaryNote: s.dietaryNote, contactNote: s.contactNote, deadline: s.deadline,
        timezone: s.timezone, perOrderMax: s.perOrderMax, askPickupTime: s.askPickupTime,
        askAllergies: s.askAllergies, allergyPrompt: s.allergyPrompt
      },
      appPay: appPayOpen(s)
        ? { discountPct: Number(s.appPayDiscountPct) || 0, hours: s.appPayHours, apps: appsOn(s).map((a) => a.label) }
        : null,
      ordering: orderingStatus(state, now),
      items: state.items
        .filter((i) => i.active)
        .map((i) => ({ id: i.id, name: i.name, description: i.description, price: i.price, image: i.image, remaining: remaining(state, i, now) })),
      pickups: state.pickups
        .filter((p) => p.active)
        .map((p) => ({ id: p.id, label: p.label, details: p.details, full: p.capacity != null && pickupTaken(state, p.id, now) >= p.capacity }))
    };
  }

  // Validates a checkout request and returns a new pending order (not yet saved).
  function buildOrder(state, input, now) {
    const s = state.settings;
    const status = orderingStatus(state, now);
    if (!status.open) throw userError(status.reason);

    const name = clean(input.name, 80);
    const email = clean(input.email, 120).toLowerCase();
    const phone = clean(input.phone, 30);
    if (!name) throw userError('Please add a name for the order.');
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) throw userError('Please check your email address.');
    if (phone.replace(/\D/g, '').length < 10) throw userError('Please add a phone number Mollie can reach you at.');

    const pickup = state.pickups.find((p) => p.id === input.pickupId && p.active);
    if (!pickup) throw userError('Please choose a pickup option.');
    if (pickup.capacity != null && pickupTaken(state, pickup.id, now) >= pickup.capacity)
      throw userError(pickup.label + ' is full. Please choose another pickup option.');

    const lines = [];
    const max = s.perOrderMax || 12;
    for (const entry of Array.isArray(input.cart) ? input.cart : []) {
      const qty = toInt(entry.qty);
      if (qty <= 0) continue;
      const item = state.items.find((i) => i.id === entry.itemId && i.active);
      if (!item) throw userError('Something in your order is no longer available. Please refresh the page.');
      if (qty > max) throw userError('You can order up to ' + max + ' of each item. Need more? Contact Mollie.');
      const left = remaining(state, item, now);
      if (left != null && qty > left)
        throw userError(left === 0 ? item.name + ' just sold out.' : 'Only ' + left + ' left of ' + item.name + '.');
      lines.push({ itemId: item.id, name: item.name, price: item.price, qty });
    }
    if (!lines.length) throw userError('Your order is empty.');

    const payMethod = input.payMethod === 'app' ? 'app' : 'card';
    if (payMethod === 'app' && !appPayOpen(s)) throw userError('Paying by app isn\u2019t available right now. Please pay by card.');
    const subtotal = lines.reduce((t, l) => t + l.price * l.qty, 0);
    const discount = payMethod === 'app' ? Math.round((subtotal * (Number(s.appPayDiscountPct) || 0)) / 100) : 0;

    return {
      id: uid(16),
      number: state.nextOrderNumber++,
      season: s.season,
      createdAt: new Date(now).toISOString(),
      status: payMethod === 'app' ? 'awaiting' : 'pending',
      payMethod,
      customer: { name, email, phone },
      pickupId: pickup.id,
      pickupLabel: pickup.label,
      pickupDetails: pickup.details,
      pickupTime: s.askPickupTime ? clean(input.pickupTime, 60) : '',
      allergyNote: s.askAllergies ? clean(input.allergyNote, 500) : '',
      lines,
      subtotal,
      discount,
      total: subtotal - discount,
      pickedUp: false
    };
  }

  function markPaid(state, orderId, info, now) {
    const o = state.orders.find((x) => x.id === orderId);
    if (!o || o.status === 'paid') return o;
    o.status = 'paid';
    o.paidAt = new Date(now).toISOString();
    if (info && info.amountPaid != null) o.amountPaid = info.amountPaid;
    if (info && info.via) o.paidVia = info.via;
    return o;
  }

  // Payment links that open the app with the amount (and note, where the app allows it) filled in.
  function appLinks(s, o) {
    const amt = (o.total / 100).toFixed(2);
    const note = 'Order #' + o.number;
    const strip = (v) => String(v).trim().replace(/^[@$]/, '').replace(/^https?:\/\/(www\.)?(venmo\.com|cash\.app|paypal\.me)\/(u\/)?/i, '').replace(/\/.*$/, '');
    const out = [];
    if (s.venmo) out.push({ app: 'Venmo', handle: '@' + strip(s.venmo), url: `https://venmo.com/?txn=pay&recipients=${encodeURIComponent(strip(s.venmo))}&amount=${amt}&note=${encodeURIComponent(note)}` });
    if (s.cashapp) out.push({ app: 'Cash App', handle: '$' + strip(s.cashapp), url: `https://cash.app/$${encodeURIComponent(strip(s.cashapp))}/${amt}` });
    if (s.paypal && s.paypal.includes('@'))
      // An email can't pre-fill PayPal, so open the Send page and let them copy the address.
      out.push({ app: 'PayPal', handle: s.paypal.trim(), copy: s.paypal.trim(), url: 'https://www.paypal.com/myaccount/transfer/homepage/pay' });
    else if (s.paypal) out.push({ app: 'PayPal', handle: 'paypal.me/' + strip(s.paypal), url: `https://paypal.me/${encodeURIComponent(strip(s.paypal))}/${amt}USD` });
    return out;
  }

  function publicOrder(o, settings) {
    const out = {
      id: o.id, number: o.number, status: o.status, payMethod: o.payMethod || 'card', name: o.customer.name, email: o.customer.email,
      pickupLabel: o.pickupLabel, pickupDetails: o.pickupDetails, pickupTime: o.pickupTime,
      lines: o.lines, subtotal: o.subtotal || o.total, discount: o.discount || 0, total: o.total
    };
    if (o.status === 'awaiting' && settings) {
      out.payBy = new Date(holdUntil(o, settings)).toISOString();
      out.timezone = settings.timezone;
      out.appLinks = appLinks(settings, o);
      out.appPayNote = settings.appPayNote;
    }
    return out;
  }

  // ---------- Mollie's side ----------
  // One entry point for every admin change, so the server and the demo share it.
  function adminAction(state, a, now) {
    const s = state.settings;
    switch (a.action) {
      case 'saveSettings': {
        const v = a.settings || {};
        for (const k of ['shopName', 'season', 'tagline', 'intro', 'dietaryNote', 'contactEmail', 'contactNote', 'allergyPrompt', 'thankYouNote', 'appPayNote'])
          if (k in v) s[k] = clean(v[k], 1000);
        for (const k of ['venmo', 'cashapp', 'paypal']) if (k in v) s[k] = clean(v[k], 80).replace(/\s+/g, '');
        for (const k of ['orderingOpen', 'askPickupTime', 'askAllergies', 'appPayEnabled']) if (k in v) s[k] = !!v[k];
        if ('appPayDiscountPct' in v) s.appPayDiscountPct = Math.min(10, Math.max(0, Number(v.appPayDiscountPct) || 0));
        if ('appPayHours' in v) s.appPayHours = Math.min(168, Math.max(1, toInt(v.appPayHours) || 24));
        if ('deadline' in v) s.deadline = v.deadline && !isNaN(Date.parse(v.deadline)) ? new Date(v.deadline).toISOString() : '';
        if ('perOrderMax' in v) s.perOrderMax = Math.min(99, Math.max(1, toInt(v.perOrderMax) || 12));
        if (!s.shopName) s.shopName = 'Bakery';
        if (!s.season) s.season = 'Orders';
        return {};
      }
      case 'saveItem': {
        const v = a.item || {};
        const price = Math.round(Number(String(v.price).replace(/[$,]/g, '')) * 100);
        if (!clean(v.name)) throw userError('Give the item a name.');
        if (!Number.isFinite(price) || price < 50) throw userError('Price needs to be at least $0.50 (a Stripe minimum).');
        let item = state.items.find((i) => i.id === v.id);
        if (!item) {
          item = { id: uid(6), active: true, image: '' };
          state.items.push(item);
        }
        item.name = clean(v.name, 80);
        item.description = clean(v.description, 400);
        item.price = price;
        item.limit = optInt(v.limit);
        if ('active' in v) item.active = !!v.active;
        if ('image' in v) item.image = v.image || '';
        return { id: item.id };
      }
      case 'deleteItem':
        state.items = state.items.filter((i) => i.id !== a.id);
        return {};
      case 'moveItem':
      case 'movePickup': {
        const list = a.action === 'moveItem' ? state.items : state.pickups;
        const i = list.findIndex((x) => x.id === a.id);
        const j = i + (a.dir < 0 ? -1 : 1);
        if (i >= 0 && j >= 0 && j < list.length) [list[i], list[j]] = [list[j], list[i]];
        return {};
      }
      case 'savePickup': {
        const v = a.pickup || {};
        if (!clean(v.label)) throw userError('Give the pickup option a name.');
        let p = state.pickups.find((x) => x.id === v.id);
        if (!p) {
          p = { id: uid(6), active: true };
          state.pickups.push(p);
        }
        p.label = clean(v.label, 80);
        p.details = clean(v.details, 200);
        p.capacity = optInt(v.capacity);
        if ('active' in v) p.active = !!v.active;
        return { id: p.id };
      }
      case 'deletePickup':
        state.pickups = state.pickups.filter((p) => p.id !== a.id);
        return {};
      case 'markPaid': {
        const o = state.orders.find((x) => x.id === a.id);
        if (!o) throw userError('Order not found.');
        markPaid(state, o.id, { via: a.via || (o.payMethod === 'app' ? 'app' : 'manual') }, now);
        return {};
      }
      case 'setPickedUp': {
        const o = state.orders.find((x) => x.id === a.id);
        if (o) o.pickedUp = !!a.pickedUp;
        return {};
      }
      case 'cancelOrder': {
        // Marks an order cancelled in this list. Refunds are done in Stripe.
        const o = state.orders.find((x) => x.id === a.id);
        if (o) o.status = 'cancelled';
        return {};
      }
      default:
        throw userError('Unknown action.');
    }
  }

  // Orders for a season plus the "bake list": how many of each item, per pickup.
  function summarize(state, season, now) {
    now = now || Date.now();
    const awaiting = state.orders
      .filter((o) => o.season === season && o.status === 'awaiting')
      .map((o) => Object.assign({}, o, { overdue: !holds(o, state.settings, now), payBy: new Date(holdUntil(o, state.settings)).toISOString() }));
    const orders = state.orders.filter((o) => o.season === season && o.status === 'paid');
    const items = {};
    for (const o of orders)
      for (const l of o.lines) {
        const row = (items[l.itemId] = items[l.itemId] || { name: l.name, total: 0, byPickup: {} });
        row.total += l.qty;
        row.byPickup[o.pickupLabel] = (row.byPickup[o.pickupLabel] || 0) + l.qty;
      }
    const pickups = [...new Set(orders.map((o) => o.pickupLabel))];
    return {
      orders,
      awaiting,
      pickups,
      bakeList: Object.values(items),
      revenue: orders.reduce((t, o) => t + o.total, 0),
      seasons: [...new Set(state.orders.map((o) => o.season))]
    };
  }

  function ordersCsv(state, season) {
    const { orders } = summarize(state, season);
    const itemNames = [...new Set(orders.flatMap((o) => o.lines.map((l) => l.name)))];
    const esc = (v) => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"';
    const head = ['Order #', 'Name', 'Email', 'Phone', 'Pickup', 'Pickup time', 'Allergies', ...itemNames, 'Discount', 'Total', 'Paid by', 'Picked up', 'Paid at'];
    const rows = orders.map((o) => [
      o.number, o.customer.name, o.customer.email, o.customer.phone, o.pickupLabel, o.pickupTime, o.allergyNote,
      ...itemNames.map((n) => o.lines.filter((l) => l.name === n).reduce((t, l) => t + l.qty, 0) || ''),
      o.discount ? (o.discount / 100).toFixed(2) : '', (o.total / 100).toFixed(2), o.payMethod === 'app' ? 'App (Venmo/Cash App/PayPal)' : 'Card', o.pickedUp ? 'yes' : '', o.paidAt
    ]);
    return [head, ...rows].map((r) => r.map(esc).join(',')).join('\n');
  }

  return { uid, defaultState, normalize, appLinks, holdUntil, money, publicShop, buildOrder, markPaid, publicOrder, adminAction, summarize, ordersCsv, remaining, holds };
});
