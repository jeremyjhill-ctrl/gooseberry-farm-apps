// Mollie's admin: orders + baking list, Bakes (each preorder's menu, pickups and dates), the Menu, shop settings.
window.Admin = (function () {
  // What one preorder is called on Mollie's side. Change these two words to rename it everywhere in her admin.
  const BAKE = 'Bake';
  const BAKES = 'Bakes';

  const app = () => document.getElementById('app');
  let st = null; // full store state from the server
  let tab = store.get('mfb_admin_tab', 'orders');
  if (tab === 'pickup') tab = 'bakes'; // pickups moved into each Bake
  let bakeSel = store.get('mfb_admin_bake', ''); // the Bake the Orders tab shows
  let openBake = null; // the Bake open on the Bakes tab ('new' while making one)
  let editing = null; // { kind: 'item'|'pickup', id, draft }
  let copying = null; // Bake id whose "Copy with dates…" choice is showing
  let showOld = false;
  let armed = null; // id of a delete button waiting for a second tap
  let saving = false;

  const act = (body) => api('POST', '/api/admin/action', body);
  const bakeById = (id) => st.bakes.find((b) => b.id === id);
  const tz = () => st.settings.timezone;

  async function refresh() {
    st = await api('GET', '/api/admin/state');
    if (!bakeById(bakeSel)) bakeSel = defaultBake();
    if (openBake && openBake !== 'new' && !bakeById(openBake)) openBake = null;
  }

  // The Bake to show first: the next pickup still taking orders, else the most recent one.
  function defaultBake() {
    const live = sorted(st.bakes.filter((b) => b.phase === 'open'), (b) => b.firstPickup);
    if (live.length) return live[0].id;
    const rest = newestFirst(st.bakes);
    return rest.length ? rest[0].id : '';
  }

  function sorted(list, key) {
    return list.slice().sort((a, b) => (key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0));
  }
  function newestFirst(list) {
    const k = (b) => (b.firstPickup !== '~' ? b.firstPickup : b.deadline || b.createdAt || '');
    return sorted(list, k).reverse();
  }

  // "Taking orders until Sat, Nov 21, 11:59 PM", "Opens Sun, Nov 29, 6:00 PM", "Switched off", "Closed Sat, Nov 21, 11:59 PM"
  function phaseText(b) {
    if (b.phase === 'open') return b.deadline ? `Taking orders until ${fmtDate(b.deadline, tz(), true)}` : 'Taking orders (no deadline)';
    if (b.phase === 'upcoming') return `Opens ${fmtDate(b.opensAt, tz(), true)}`;
    if (b.phase === 'closed') return `Closed ${fmtDate(b.deadline, tz(), true)}`;
    return 'Switched off';
  }

  function pickupDays(b) {
    const days = [...new Set(b.pickups.filter((p) => p.active).flatMap((p) => (p.windows || []).map((w) => w.date)))].sort();
    if (!days.length) return '';
    const label = (d) => windowLabel({ date: d });
    return 'Pickup ' + (days.length <= 3 ? days.map(label).join(' & ') : `${label(days[0])} – ${label(days[days.length - 1])}`);
  }

  // ---------- views ----------
  async function loginView(msg) {
    let accounts = false;
    try { accounts = (await api('GET', '/api/admin/login-mode')).accounts; } catch (e) {}
    app().innerHTML = `<div class="wrap"><form class="panel login" id="login">
      <h2>Baker login</h2><p class="muted small">For Mollie only.</p>
      ${accounts ? `<label class="field"><span>Email</span><input type="email" name="email" autocomplete="username" required /></label>` : ''}
      <label class="field"><span>Password</span><input type="password" name="password" autocomplete="current-password" required /></label>
      ${msg ? `<div class="form-error">${h(msg)}</div>` : ''}
      <button class="btn block" type="submit">Sign in</button>
      ${accounts ? `<p class="small" style="text-align:center"><a href="account/forgot">Forgot your password?</a></p>` : ''}
      <p class="small" style="text-align:center"><a href="#">Back to the shop</a></p>
    </form></div>`;
    document.getElementById('login').onsubmit = async (e) => {
      e.preventDefault();
      try {
        const r = await api('POST', '/api/admin/login', { email: e.target.email ? e.target.email.value : '', password: e.target.password.value });
        if (r && r.mustChange) { location.href = 'account/password?first=1&next=' + encodeURIComponent(location.pathname + '#admin'); return; }
        start();
      } catch (err) {
        loginView(err.message);
      }
    };
  }

  function statusStrip(b) {
    const open = b.phase === 'open';
    return `<div class="status-strip ${open ? 'open' : 'closed'}"><span><span class="dot"></span>${h(phaseText(b))}</span>
      <button type="button" class="btn small ${b.open ? 'danger' : ''}" data-set-open="${b.id}" data-on="${b.open ? '' : '1'}">${b.open ? 'Stop orders now' : 'Turn orders on'}</button></div>`;
  }

  function bakeOptions(selected) {
    const groups = [
      ['Taking orders', sorted(st.bakes.filter((b) => b.phase === 'open'), (b) => b.firstPickup)],
      ['Opens later', sorted(st.bakes.filter((b) => b.phase === 'upcoming'), (b) => b.opensAt)],
      ['Not taking orders', newestFirst(st.bakes.filter((b) => b.phase === 'off' || b.phase === 'closed'))]
    ];
    return groups
      .filter(([, list]) => list.length)
      .map(([label, list]) => `<optgroup label="${label}">${list.map((b) => `<option value="${b.id}" ${b.id === selected ? 'selected' : ''}>${h(b.name)}</option>`).join('')}</optgroup>`)
      .join('');
  }

  function ordersTab() {
    const b = bakeById(bakeSel);
    if (!b) return `<div class="panel"><h2>Orders</h2><p class="empty">No ${BAKES.toLowerCase()} yet. Start one on the ${BAKES} tab.</p></div>`;
    const sum = StoreCore.summarize(st, b.id, Date.now());
    const itemCount = sum.bakeList.reduce((t, r) => t + r.total, 0);
    const bake = sum.bakeList.length
      ? `<div class="table-scroll"><table class="bake"><thead><tr><th>Item</th>${sum.pickups.map((p) => `<th class="n">${h(p)}</th>`).join('')}<th class="n">Total</th></tr></thead><tbody>
        ${sum.bakeList.map((r) => `<tr><td>${h(r.name)}</td>${sum.pickups.map((p) => `<td class="n">${r.byPickup[p] || '–'}</td>`).join('')}<td class="n"><b>${r.total}</b></td></tr>`).join('')}
        </tbody></table></div>`
      : '<p class="empty">No paid orders yet.</p>';
    const groups = sum.slots
      .map((p) => {
        const os = sum.orders.filter((o) => StoreCore.slotOf(o) === p).sort((a, c) => a.customer.name.localeCompare(c.customer.name));
        const done = os.filter((o) => o.pickedUp).length;
        return `<div class="group-title">${h(p)} <span class="muted small">${done}/${os.length} picked up</span></div>${os.map(orderCard).join('')}`;
      })
      .join('');
    return `<div class="panel-head"><select data-bake-sel aria-label="Which ${BAKE.toLowerCase()}" style="max-width:300px">${bakeOptions(b.id)}</select>
        <div class="row-actions"><button class="btn ghost small" data-copy>Copy baking list</button><a class="btn ghost small" href="api/admin/orders.csv?bake=${encodeURIComponent(b.id)}">Download spreadsheet</a></div></div>
      ${statusStrip(b)}
      <div class="stats"><div class="stat"><b>${sum.orders.length}</b><span>paid orders</span></div><div class="stat"><b>${itemCount}</b><span>items to bake</span></div><div class="stat"><b>${money(sum.revenue)}</b><span>collected</span></div></div>
      ${sum.awaiting.length ? `<div class="panel awaiting-box"><h2>Waiting on payment</h2><p class="muted small">Venmo, Cash App or PayPal orders. Tap <b>Mark paid</b> when the money shows up. Their items are held until the deadline shown.</p>${sum.awaiting.map(awaitingCard).join('')}</div>` : ''}
      <div class="panel"><h2>Baking list</h2><p class="muted small">Everything paid for in ${h(b.name)}, split by pickup.${sum.awaiting.length ? ` Not counting ${sum.awaiting.length} order${sum.awaiting.length > 1 ? 's' : ''} still waiting on payment.` : ''}</p>${bake}</div>
      <div class="panel"><h2>Orders</h2>${groups || '<p class="empty">Orders show up here once they’re paid.</p>'}</div>`;
  }

  function awaitingCard(o) {
    const tel = o.customer.phone.replace(/[^\d+]/g, '');
    return `<div class="order">
      <div class="top"><div class="who"><b>${h(o.customer.name)}</b> <span class="muted small">#${o.number}</span>
        <div class="meta"><a href="tel:${h(tel)}">${h(o.customer.phone)}</a> · ${h(StoreCore.slotOf(o))}</div></div>
        <b>${money(o.total)}</b></div>
      <ul>${o.lines.map((l) => `<li>${l.qty} × ${h(l.name)}</li>`).join('')}</ul>
      <div class="meta" style="margin-top:6px">${o.overdue ? '<span class="pill warn" style="margin:0">Past due, items released</span>' : `Pay by ${h(fmtDate(o.payBy, tz(), true))}`}</div>
      <div class="actions"><button class="btn small" data-mark-paid="${o.id}">Mark paid</button>
        <span style="flex:1"></span><button class="btn link small" data-cancel-order="${o.id}">${armed === 'o' + o.id ? 'Tap again to remove' : 'Remove'}</button></div>
    </div>`;
  }

  function orderCard(o) {
    const tel = o.customer.phone.replace(/[^\d+]/g, '');
    return `<div class="order ${o.pickedUp ? 'done' : ''}">
      <div class="top"><div class="who"><b>${h(o.customer.name)}</b> <span class="muted small">#${o.number}</span>
        <div class="meta"><a href="tel:${h(tel)}">${h(o.customer.phone)}</a> · ${h(o.customer.email)}${o.pickupTime ? ` · pickup time: ${h(o.pickupTime)}` : ''}</div></div>
        <div style="text-align:right"><b>${money(o.total)}</b><br /><span class="pill ${o.payMethod === 'app' ? 'berry' : ''}" style="margin:0">${o.payMethod === 'app' ? 'App' : 'Card'}</span></div></div>
      <ul>${o.lines.map((l) => `<li>${l.qty} × ${h(l.name)}</li>`).join('')}</ul>
      ${o.allergyNote ? `<div class="note"><b>Allergy note:</b> ${h(o.allergyNote)}</div>` : ''}
      ${o.mailProblems && o.mailProblems.length ? `<div class="note" style="background:#f6e2dd">The confirmation email didn’t send. You may want to text them.</div>` : ''}
      <div class="actions"><label class="picked"><input type="checkbox" data-picked="${o.id}" ${o.pickedUp ? 'checked' : ''} /> Picked up</label>
        <span style="flex:1"></span><button class="btn link small" data-cancel-order="${o.id}">${armed === 'o' + o.id ? 'Tap again to remove' : 'Remove'}</button></div>
    </div>`;
  }

  // ---------- Bakes ----------
  function bakesTab() {
    if (openBake) return bakePage(openBake === 'new' ? null : bakeById(openBake));
    const row = (b) => {
      const items = b.menu.length;
      const meta = [pickupDays(b), `${items} item${items === 1 ? '' : 's'}`, `${b.orderCount} paid order${b.orderCount === 1 ? '' : 's'}`].filter(Boolean).join(' · ');
      const copyChoice = copying === b.id
        ? `<div class="copy-choice"><span class="small">Copy with dates:</span><button class="btn small" data-copy-bake="${b.id}" data-days="7">One week later</button><button class="btn ghost small" data-copy-bake="${b.id}" data-days="0">Same dates</button><button class="btn link small" data-copy-cancel>Cancel</button></div>`
        : '';
      return `<div class="bake-row ${b.phase}"><div class="info"><b>${h(b.name)}</b><span class="phase"><span class="dot"></span>${h(phaseText(b))}</span><span>${h(meta)}</span></div>
        <div class="bake-actions"><button class="btn ghost small" data-open-bake="${b.id}">Open</button><button class="btn link small" data-ask-copy="${b.id}">Copy</button></div>${copyChoice}</div>`;
    };
    const live = sorted(st.bakes.filter((b) => b.phase === 'open'), (b) => b.firstPickup);
    const later = sorted(st.bakes.filter((b) => b.phase === 'upcoming'), (b) => b.opensAt);
    const rest = newestFirst(st.bakes.filter((b) => b.phase === 'off' || b.phase === 'closed'));
    const shown = showOld ? rest : rest.slice(0, 8);
    const section = (title, list, empty) => `<div class="group-title">${title}</div>${list.length ? list.map(row).join('') : `<p class="empty">${empty}</p>`}`;
    return `<div class="panel"><div class="panel-head"><div><h2>${BAKES}</h2><p class="muted small" style="margin:0">Each ${BAKE.toLowerCase()} is one preorder: its own menu, how many of each you can make, pickup times and order dates. Several can take orders at once.</p></div>
        <button class="btn small" data-new-bake>+ New ${BAKE.toLowerCase()}</button></div>
      ${section('Taking orders', live, `No ${BAKES.toLowerCase()} are taking orders right now.`)}
      ${later.length ? section('Opens later', later, '') : ''}
      ${rest.length ? section('Not taking orders', shown, '') : ''}
      ${rest.length > shown.length ? `<button class="btn link small" data-show-old>Show ${rest.length - shown.length} older</button>` : ''}</div>`;
  }

  function bakePage(b) {
    const back = `<p style="margin:0 0 12px"><button class="btn link small" data-close-bake>← All ${BAKES.toLowerCase()}</button></p>`;
    const d = b || { name: '', note: '', opensAt: '', deadline: '', open: false };
    const details = `<form class="panel" data-form="bake"><h2>${b ? h(b.name) : `New ${BAKE.toLowerCase()}`}</h2>
      ${b ? statusStrip(b) : ''}
      <label class="field"><span>Name</span><input type="text" name="name" maxlength="80" required value="${h(d.name)}" placeholder="e.g. Market – Sat, Dec 5 or Christmas 2026" /><small>Customers see this. Orders are grouped by it.</small></label>
      <label class="field"><span>Note for customers</span><textarea name="note" maxlength="1000" rows="3" placeholder="e.g. Preorder for pickup at the Saturday market. Order by Thursday night.">${h(d.note)}</textarea><small>Shown at the top of this ${BAKE.toLowerCase()}’s menu. Press Return for a new paragraph.</small></label>
      <div class="grid2">
        <label class="field"><span>Orders open <span class="muted">(optional)</span></span><input type="datetime-local" name="opensAt" value="${localInput(d.opensAt)}" /><small>Blank: open as soon as it’s switched on. Before this, customers see the menu with “Opens …”.</small></label>
        <label class="field"><span>Orders close</span><input type="datetime-local" name="deadline" value="${localInput(d.deadline)}" /><small>Orders stop automatically after this.</small></label>
      </div>
      <label class="switch"><input type="checkbox" name="open" ${d.open ? 'checked' : ''} /> Taking orders</label>
      <div class="row-actions"><button class="btn" type="submit" ${saving ? 'disabled' : ''}>${saving ? 'Saving…' : b ? 'Save' : `Create ${BAKE.toLowerCase()}`}</button>
        ${b ? '' : '<button class="btn ghost" type="button" data-close-bake>Cancel</button>'}<span class="spacer"></span>
        ${b && !b.orderCount && !st.orders.some((o) => o.bakeId === b.id && (o.status === 'awaiting' || o.status === 'pending')) ? `<button class="btn link" type="button" data-delete-bake="${b.id}" style="color:var(--danger)">${armed === 'b' + b.id ? 'Tap again to delete' : 'Delete'}</button>` : ''}</div>
      ${b ? `<div class="share-link"><span class="small muted">Link straight to this ${BAKE.toLowerCase()}:</span> <code>${h(bakeLink(b))}</code> <button type="button" class="btn ghost small" data-copy-link="${b.id}">Copy link</button></div>` : ''}
    </form>`;
    if (!b) return back + details + `<p class="muted small">After you create it, you’ll pick its menu and pickup times here.</p>`;
    return back + details + bakeMenu(b) + bakePickups(b);
  }

  function bakeLink(b) {
    return location.origin + location.pathname + '?bake=' + b.id;
  }

  function bakeMenu(b) {
    const rows = st.items
      .map((i) => {
        const on = b.menu.includes(i.id);
        const limit = b.limits[i.id];
        const left = b.stock[i.id];
        return `<div class="item-row bake-item${on ? '' : ' off-sale'}">
          <label class="switch insale"><input type="checkbox" data-bake-item="${i.id}" ${on ? 'checked' : ''} /> <span class="sr-only">On the menu</span></label>
          <div class="thumb">${i.image ? `<img src="${h(i.image)}" alt="" />` : bakeArt(i.name)}</div>
          <div class="info"><b>${h(i.name)}${on && left === 0 ? '<span class="pill warn">Sold out</span>' : ''}</b><span>${money(i.price)}${on && limit != null ? ` · ${left} of ${limit} left` : ''}</span></div>
          ${on ? `<label class="howmany"><span>How many</span><input type="number" min="0" step="1" inputmode="numeric" data-bake-limit="${i.id}" value="${limit == null ? '' : limit}" placeholder="No limit" /></label>` : ''}
        </div>`;
      })
      .join('');
    return `<div class="panel"><div class="panel-head"><div><h2>Menu for ${h(b.name)}</h2><p class="muted small" style="margin:0">Tick what you’re making (${b.menu.length} of ${st.items.length}). <b>How many</b> is how many you can make for this ${BAKE.toLowerCase()}; blank means no limit. Changes save right away.</p></div></div>
      ${rows || `<p class="empty">Your Menu is empty. Add items on the Menu tab.</p>`}
      <p class="muted small" style="margin-bottom:0">New items and photos go on the <button class="btn link small" data-tab="menu" style="padding:0">Menu tab</button>.</p></div>`;
  }

  // Places used in other Bakes, to add here in one tap.
  function placesUsed(b) {
    const seen = new Map();
    for (const x of newestFirst(st.bakes))
      for (const p of x.pickups)
        if (!b.pickups.some((q) => q.id === p.id || q.label === p.label) && !seen.has(p.label)) seen.set(p.label, p);
    return [...seen.values()];
  }

  function bakePickups(b) {
    const rows = b.pickups
      .map((p, idx) => {
        if (editing && editing.kind === 'pickup' && editing.id === p.id) return pickupEditor(editing.draft);
        const n = st.orders.filter((o) => o.bakeId === b.id && o.pickupId === p.id && o.status === 'paid').length;
        return `<div class="item-row"><div class="mover"><button data-move-pickup="${p.id}" data-dir="-1" ${idx === 0 ? 'disabled' : ''} aria-label="Move up">▲</button><button data-move-pickup="${p.id}" data-dir="1" ${idx === b.pickups.length - 1 ? 'disabled' : ''} aria-label="Move down">▼</button></div>
          <div class="info"><b>${h(p.label)}${p.active ? '' : '<span class="pill">Off</span>'}</b><span>${h(p.details)}</span><br /><span>${n} order${n === 1 ? '' : 's'}${p.capacity == null ? '' : ` of ${p.capacity} max`}</span>
          ${(p.windows || []).map((w) => {
            const k = st.orders.filter((o) => o.bakeId === b.id && o.pickupWindowId === w.id && o.status === 'paid').length;
            return `<br /><span class="small">${h(windowLabel(w))} · ${k}${w.capacity == null ? '' : ` of ${w.capacity}`} order${k === 1 && w.capacity == null ? '' : 's'}</span>`;
          }).join('')}</div>
          <button class="btn ghost small" data-edit-pickup="${p.id}">Edit</button></div>`;
      })
      .join('');
    const adding = editing && editing.kind === 'pickup' && editing.isNew;
    const used = placesUsed(b);
    return `<div class="panel"><div class="panel-head"><div><h2>Pickup</h2><p class="muted small" style="margin:0">Customers choose one of these at checkout.</p></div>
      ${adding ? '' : '<button class="btn small" data-add-pickup>+ Add pickup</button>'}</div>
      ${!adding && used.length ? `<label class="field" style="max-width:420px"><span>Or add a place you’ve used before</span><select data-reuse-place><option value="">Choose…</option>${used.map((p, i) => `<option value="${i}">${h(p.label)}</option>`).join('')}</select></label>` : ''}
      ${adding ? pickupEditor(editing.draft) : ''}${rows || '<p class="empty">No pickup options yet. Customers can’t check out until there’s at least one.</p>'}</div>`;
  }

  function pickupEditor(d) {
    return `<form class="editor" data-form="pickup">
      <label class="field"><span>Name</span><input type="text" name="label" value="${h(d.label)}" maxlength="80" required placeholder="e.g. Gooseberry Bridge Farm" /></label>
      <label class="field"><span>Address & details</span><input type="text" name="details" value="${h(d.details)}" maxlength="200" placeholder="e.g. 177 Elm Grove Rd, Rogersville" /><small>${(d.windows || []).length ? 'Shown under the pickup time customers choose.' : 'Without pickup windows, include the day and hours here.'}</small></label>
      <div class="field"><span>Pickup windows</span><small>Customers choose one window. Leave this empty to offer the place without set times.</small></div>
      <div class="windows">${(d.windows || []).map(windowRow).join('')}</div>
      <button class="btn ghost small" type="button" data-add-window style="margin-bottom:14px">+ Add window</button>
      <label class="field"><span>Max orders for this place</span><input type="number" name="capacity" min="0" step="1" value="${d.capacity == null ? '' : d.capacity}" placeholder="No limit" /><small>Leave blank for no limit.</small></label>
      <label class="switch"><input type="checkbox" name="active" ${d.active ? 'checked' : ''} /> Offer this pickup</label>
      ${editorButtons(d.isNew ? null : d.id)}
    </form>`;
  }

  function windowRow(w, i) {
    return `<div class="win">
      <label><span>Date</span><input type="date" data-w="${i}" data-f="date" value="${h(w.date || '')}" /></label>
      <label><span>From</span><input type="time" data-w="${i}" data-f="start" step="900" value="${h(w.start || '')}" /></label>
      <label><span>To</span><input type="time" data-w="${i}" data-f="end" step="900" value="${h(w.end || '')}" /></label>
      <label><span>Max orders</span><input type="number" data-w="${i}" data-f="capacity" min="0" step="1" inputmode="numeric" placeholder="No limit" value="${w.capacity == null ? '' : w.capacity}" /></label>
      <button class="btn link small" type="button" data-rm-window="${i}" aria-label="Remove this window" style="color:var(--danger)">Remove</button>
    </div>`;
  }

  // Same wording customers see: "Wed, Nov 25, 9–11am".
  function windowLabel(w) {
    const [y, m, d] = String(w.date || '').split('-').map(Number);
    const day = y ? new Intl.DateTimeFormat('en-US', { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC' }).format(new Date(Date.UTC(y, m - 1, d))) : '';
    const hm = (t) => (/^\d{1,2}:\d{2}$/.test(t || '') ? t.split(':').map(Number) : null);
    const clock = ([hh, mm], ampm) => `${hh % 12 || 12}${mm ? ':' + String(mm).padStart(2, '0') : ''}${ampm ? (hh < 12 ? 'am' : 'pm') : ''}`;
    const a = hm(w.start), b = hm(w.end);
    if (!a) return day;
    if (!b) return `${day}, from ${clock(a, true)}`;
    const same = (a[0] < 12) === (b[0] < 12);
    return `${day}, ${clock(a, !same)}–${clock(b, true)}`;
  }

  function addWindow() {
    syncDraft();
    const ws = (editing.draft.windows = editing.draft.windows || []);
    const last = ws[ws.length - 1];
    const plus2 = (t) => { const [hh, mm] = t.split(':').map(Number); const e = Math.min(hh * 60 + mm + 120, 23 * 60 + 45); return `${String(Math.floor(e / 60)).padStart(2, '0')}:${String(e % 60).padStart(2, '0')}`; };
    if (last && last.end) ws.push({ date: last.date, start: last.end, end: plus2(last.end), capacity: last.capacity });
    else ws.push({ date: last ? last.date : '', start: '09:00', end: '11:00', capacity: null });
    render();
  }

  function editorButtons(id) {
    return `<div class="row-actions"><button class="btn" type="submit" ${saving ? 'disabled' : ''}>${saving ? 'Saving…' : 'Save'}</button>
      <button class="btn ghost" type="button" data-cancel-edit>Cancel</button><span class="spacer"></span>
      ${id ? `<button class="btn link" type="button" data-delete="${id}" style="color:var(--danger)">${armed === id ? 'Tap again to delete' : 'Delete'}</button>` : ''}</div>`;
  }

  // ---------- the Menu: everything she makes ----------
  const current = () => st.bakes.filter((b) => b.phase === 'open' || b.phase === 'upcoming');

  function itemEditor(d) {
    const bakes = current();
    return `<form class="editor" data-form="item">
      <div class="photo-pick"><div class="thumb">${d.image ? `<img src="${h(d.image)}" alt="" />` : bakeArt(d.name)}</div>
        <div><label class="btn ghost small" style="cursor:pointer">${d.image ? 'Change photo' : 'Add photo'}<input type="file" accept="image/*" data-photo hidden /></label>
        ${d.image ? '<button type="button" class="btn link small" data-rmphoto>Remove photo</button>' : ''}
        <div class="muted small" style="margin-top:6px">A square-ish or landscape photo looks best.</div></div></div>
      <label class="field"><span>Name</span><input type="text" name="name" value="${h(d.name)}" maxlength="80" required /></label>
      <label class="field"><span>Short description</span><textarea name="description" maxlength="400" rows="2">${h(d.description)}</textarea><small>One line on the menu card, e.g. “Whole 9-inch pie.”</small></label>
      <label class="field"><span>Full description <span class="muted">(optional)</span></span><textarea name="details" maxlength="2000" rows="5" placeholder="Tell customers why they’ll love it.">${h(d.details || '')}</textarea><small>Shown when a customer taps the item. Press Return for a new paragraph.</small></label>
      <label class="field"><span>Contains <span class="muted">(optional)</span></span><input type="text" name="contains" maxlength="120" value="${h(d.contains || '')}" placeholder="e.g. coconut, eggs" /><small>Shown as a tag on the menu card so allergies are easy to spot.</small></label>
      <label class="field" style="max-width:200px"><span>Price ($)</span><input type="number" name="price" inputmode="decimal" step="0.01" min="0.5" value="${d.price}" required /></label>
      ${bakes.length ? `<div class="field"><span>On the menu for</span>${bakes.map((b) => `<label class="switch"><input type="checkbox" data-in-bake="${b.id}" ${(d.inBakes || []).includes(b.id) ? 'checked' : ''} /> ${h(b.name)}</label>`).join('')}<small>How many you can make is set on each ${BAKE.toLowerCase()}’s page.</small></div>` : ''}
      ${editorButtons(d.id)}
    </form>`;
  }

  function menuTab() {
    const rows = st.items
      .map((i, idx) => {
        if (editing && editing.kind === 'item' && editing.id === i.id) return itemEditor(editing.draft);
        const inBakes = current().filter((b) => b.menu.includes(i.id)).map((b) => b.name);
        return `<div class="item-row${inBakes.length ? '' : ' off-sale'}"><div class="mover"><button data-move-item="${i.id}" data-dir="-1" ${idx === 0 ? 'disabled' : ''} aria-label="Move up">▲</button><button data-move-item="${i.id}" data-dir="1" ${idx === st.items.length - 1 ? 'disabled' : ''} aria-label="Move down">▼</button></div>
          <div class="thumb">${i.image ? `<img src="${h(i.image)}" alt="" />` : bakeArt(i.name)}</div>
          <div class="info"><b>${h(i.name)}</b><span>${money(i.price)} · ${inBakes.length ? `in ${h(inBakes.join(', '))}` : 'saved for later'}</span></div>
          <button class="btn ghost small" data-edit-item="${i.id}">Edit</button></div>`;
      })
      .join('');
    const adding = editing && editing.kind === 'item' && !editing.id;
    return `<div class="panel"><div class="panel-head"><div><h2>Menu</h2><p class="muted small" style="margin:0">Everything you make. Each ${BAKE.toLowerCase()} picks from this list, and customers see items in this order.</p></div>
      ${adding ? '' : '<button class="btn small" data-add-item>+ Add item</button>'}</div>
      ${adding ? itemEditor(editing.draft) : ''}${rows || '<p class="empty">No items yet.</p>'}</div>`;
  }

  function localInput(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function settingsTab() {
    const s = st.settings;
    const f = (name, label, help, type) =>
      `<label class="field"><span>${label}</span>${type === 'area' ? `<textarea name="${name}">${h(s[name])}</textarea>` : `<input type="text" name="${name}" value="${h(s[name])}" />`}${help ? `<small>${help}</small>` : ''}</label>`;
    return `<form class="panel" data-form="settings"><h2>Shop settings</h2><p class="muted small">These apply to every ${BAKE.toLowerCase()}. Dates, menus and pickup times are on the ${BAKES} tab.</p>
      ${f('shopName', 'Shop name')}
      ${f('tagline', 'Tagline')}
      ${f('intro', 'Welcome message <span class="muted">(optional)</span>', `Shown at the top of the shop, above every ${BAKE.toLowerCase()}. Each ${BAKE.toLowerCase()} has its own note too.`, 'area')}
      ${f('dietaryNote', 'Dietary note', 'Shown in the green box at the top.', 'area')}
      ${f('contactNote', 'Contact line', 'Shown at the bottom of the page.')}
      <label class="field"><span>Most of one item per order</span><input type="number" name="perOrderMax" min="1" max="99" value="${s.perOrderMax}" /><small>Customers who reach this see “Limit ${h(s.perOrderMax)} per order. Need more? Contact Mollie.”</small></label>
      <label class="field"><span>Show “Only X left” at</span><input type="number" name="lowStockAt" min="0" max="999" value="${s.lowStockAt == null ? 5 : s.lowStockAt}" /><small>Items with this many or fewer left show the count. Use 0 to never show it.</small></label>
      <label class="switch"><input type="checkbox" name="askPickupTime" ${s.askPickupTime ? 'checked' : ''} /> Ask for an estimated pickup time</label>
      <label class="switch"><input type="checkbox" name="askAllergies" ${s.askAllergies ? 'checked' : ''} /> Ask about allergies</label>
      ${f('allergyPrompt', 'Allergy question', '', 'area')}
      ${f('thankYouNote', 'Thank-you message', 'Shown after someone pays.', 'area')}
      <h2 style="margin-top:26px">Venmo, Cash App & PayPal</h2>
      <p class="muted small">Customers can reserve their order and send payment by app. You mark it paid when it arrives. Fill in only the apps you use. Leave all three blank to turn this off.</p>
      <label class="switch"><input type="checkbox" name="appPayEnabled" ${s.appPayEnabled ? 'checked' : ''} /> Offer paying by app</label>
      <div class="grid2">
        ${f('venmo', 'Venmo username', 'e.g. @Mollies-Bakery')}
        ${f('cashapp', 'Cash App $cashtag', 'e.g. $MolliesBakery')}
        ${f('paypal', 'PayPal email or PayPal.me name', 'A PayPal.me name lets the button fill in the amount. With an email, customers copy it into PayPal.')}
        <label class="field"><span>Discount for paying by app (%)</span><input type="number" name="appPayDiscountPct" min="0" max="10" step="0.5" value="${h(s.appPayDiscountPct)}" /><small>Card prices stay as listed. App payers get this much off.</small></label>
        <label class="field"><span>Hours to send payment</span><input type="number" name="appPayHours" min="1" max="168" value="${h(s.appPayHours)}" /><small>Unpaid orders release their items after this.</small></label>
      </div>
      ${f('appPayNote', 'Note on the payment screen', '', 'area')}
      <button class="btn" type="submit" ${saving ? 'disabled' : ''}>${saving ? 'Saving…' : 'Save settings'}</button>
    </form>`;
  }

  function render() {
    const tabs = [['orders', 'Orders'], ['bakes', BAKES], ['menu', 'Menu'], ['settings', 'Settings']];
    const body = tab === 'menu' ? menuTab() : tab === 'bakes' ? bakesTab() : tab === 'settings' ? settingsTab() : ordersTab();
    app().innerHTML = `<div class="admin-top"><div class="wrap"><h1>${h(st.settings.shopName)}</h1>
        <a class="btn ghost small" href="#">View shop</a><button class="btn link small" data-logout>Sign out</button></div>
        <nav class="tabs">${tabs.map(([k, l]) => `<button class="tab ${tab === k ? 'on' : ''}" data-tab="${k}">${l}</button>`).join('')}</nav></div>
      <main class="wrap admin-main">${st.demo ? '<div class="banner demo">Demo mode: no Stripe key is set, so orders are marked paid without a payment.</div>' : ''}${body}</main>${st.build ? `<p class="muted small" style="text-align:center;padding:20px 0 28px">Build ${h(st.build)}</p>` : ''}`;
  }

  // ---------- actions ----------
  async function run(body, okMsg) {
    try {
      const r = await act(body);
      await refresh();
      if (okMsg) toast(okMsg);
      return r;
    } catch (e) {
      toast(e.message, 'error');
      throw e;
    }
  }

  function readForm(form) {
    const out = {};
    for (const el of form.elements) {
      if (!el.name) continue;
      out[el.name] = el.type === 'checkbox' ? el.checked : el.value;
    }
    return out;
  }

  async function resizePhoto(file) {
    const url = URL.createObjectURL(file);
    try {
      const img = await new Promise((res, rej) => {
        const i = new Image();
        i.onload = () => res(i);
        i.onerror = () => rej(new Error("That photo couldn't be opened. Try a JPG or PNG."));
        i.src = url;
      });
      const scale = Math.min(1, 1400 / Math.max(img.width, img.height));
      const c = document.createElement('canvas');
      c.width = Math.round(img.width * scale);
      c.height = Math.round(img.height * scale);
      c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
      return c.toDataURL('image/jpeg', 0.82);
    } finally {
      URL.revokeObjectURL(url);
    }
  }

  function arm(id) {
    armed = id;
    render();
    setTimeout(() => {
      if (armed === id) { armed = null; render(); }
    }, 3500);
  }

  // Keep the page where it was when a change re-draws it.
  async function quiet(fn) {
    const y = window.scrollY;
    try { await fn(); } catch (e) { /* run() already showed the problem */ }
    render();
    window.scrollTo(0, y);
  }

  function bind() {
    const el = app();
    el.onclick = async (e) => {
      const t = e.target.closest('button, a');
      if (!t) return;
      const d = t.dataset;
      if (d.tab) { tab = d.tab; editing = null; openBake = null; copying = null; store.set('mfb_admin_tab', tab); render(); window.scrollTo(0, 0); }
      else if ('logout' in d) { await api('POST', '/api/admin/logout'); location.hash = ''; }
      else if (d.setOpen) { const on = !!d.on; await quiet(() => run({ action: 'setBakeOpen', bakeId: d.setOpen, open: on }, on ? 'Orders are on' : 'Orders stopped')); }
      else if ('copy' in d) copyBakeList();
      else if (d.markPaid) { await quiet(() => run({ action: 'markPaid', id: d.markPaid }, 'Marked paid')); }
      // Bakes
      else if ('newBake' in d) { openBake = 'new'; editing = null; render(); window.scrollTo(0, 0); }
      else if (d.openBake) { openBake = d.openBake; editing = null; copying = null; render(); window.scrollTo(0, 0); }
      else if ('closeBake' in d) { openBake = null; editing = null; render(); }
      else if (d.askCopy) { copying = d.askCopy; render(); }
      else if ('copyCancel' in d) { copying = null; render(); }
      else if (d.copyBake) {
        const days = Number(d.days);
        try {
          const r = await run({ action: 'copyBake', bakeId: d.copyBake, days }, days ? 'Copied with dates one week later. It’s switched off until you turn it on.' : 'Copied. It’s switched off until you turn it on.');
          copying = null;
          openBake = r.id;
          render();
          window.scrollTo(0, 0);
        } catch (err) { render(); }
      } else if ('showOld' in d) { showOld = true; render(); }
      else if (d.copyLink) {
        const b = bakeById(d.copyLink);
        navigator.clipboard.writeText(bakeLink(b)).then(() => toast('Link copied. Share it anywhere.'), () => toast("Couldn't copy on this device.", 'error'));
      } else if (d.deleteBake) {
        if (armed !== 'b' + d.deleteBake) return arm('b' + d.deleteBake);
        armed = null;
        try { await run({ action: 'deleteBake', bakeId: d.deleteBake }, 'Deleted'); openBake = null; } catch (err) {}
        render();
      }
      // Menu items
      else if (d.editItem) { const i = st.items.find((x) => x.id === d.editItem); editing = { kind: 'item', id: i.id, draft: { ...i, price: (i.price / 100).toFixed(2), inBakes: current().filter((b) => b.menu.includes(i.id)).map((b) => b.id) } }; render(); }
      else if ('addItem' in d) { editing = { kind: 'item', id: null, draft: { name: '', description: '', details: '', contains: '', price: '', image: '', inBakes: [] } }; render(); }
      else if (d.moveItem) { await quiet(() => run({ action: 'moveItem', id: d.moveItem, dir: Number(d.dir) })); }
      // Pickups (on a Bake's page)
      else if (d.editPickup) { const p = bakeById(openBake).pickups.find((x) => x.id === d.editPickup); editing = { kind: 'pickup', id: p.id, draft: { ...p, windows: (p.windows || []).map((w) => ({ ...w })) } }; render(); }
      else if ('addPickup' in d) { editing = { kind: 'pickup', id: null, isNew: true, draft: { label: '', details: '', capacity: null, active: true, windows: [], isNew: true } }; render(); }
      else if (d.movePickup) { await quiet(() => run({ action: 'movePickup', bakeId: openBake, id: d.movePickup, dir: Number(d.dir) })); }
      else if ('addWindow' in d) addWindow();
      else if (d.rmWindow) { syncDraft(); editing.draft.windows.splice(Number(d.rmWindow), 1); render(); }
      else if ('cancelEdit' in d) { editing = null; render(); }
      else if ('rmphoto' in d) { syncDraft(); editing.draft.image = ''; render(); }
      else if (d.delete) {
        if (armed !== d.delete) return arm(d.delete);
        armed = null;
        const body = editing.kind === 'item' ? { action: 'deleteItem', id: d.delete } : { action: 'deletePickup', bakeId: openBake, id: d.delete };
        await quiet(async () => { await run(body, 'Deleted'); editing = null; });
      } else if (d.cancelOrder) {
        if (armed !== 'o' + d.cancelOrder) return arm('o' + d.cancelOrder);
        armed = null;
        await quiet(() => run({ action: 'cancelOrder', id: d.cancelOrder }, 'Order removed. Refund it in Stripe if needed.'));
      }
    };
    el.oninput = (e) => {
      const t = e.target;
      if (t.dataset.w != null && editing && editing.draft.windows) editing.draft.windows[Number(t.dataset.w)][t.dataset.f] = t.value;
    };
    el.onchange = async (e) => {
      const t = e.target;
      if (t.dataset.w != null) return el.oninput(e);
      if (t.dataset.picked) { await quiet(() => run({ action: 'setPickedUp', id: t.dataset.picked, pickedUp: t.checked })); }
      else if ('bakeSel' in t.dataset) { bakeSel = t.value; store.set('mfb_admin_bake', bakeSel); render(); }
      else if (t.dataset.bakeItem) {
        const on = t.checked;
        await quiet(() => run({ action: 'setBakeItem', bakeId: openBake, itemId: t.dataset.bakeItem, on }, on ? 'On the menu' : 'Off this menu'));
      } else if (t.dataset.bakeLimit) {
        await quiet(() => run({ action: 'setBakeItem', bakeId: openBake, itemId: t.dataset.bakeLimit, limit: t.value }, 'Saved'));
      } else if ('reusePlace' in t.dataset && t.value !== '') {
        const p = placesUsed(bakeById(openBake))[Number(t.value)];
        editing = { kind: 'pickup', id: null, isNew: true, draft: { id: p.id, label: p.label, details: p.details, capacity: p.capacity, active: true, windows: [], isNew: true } };
        render();
      } else if ('photo' in t.dataset && t.files[0]) {
        syncDraft();
        try {
          editing.draft.image = await resizePhoto(t.files[0]);
          render();
        } catch (err) { toast(err.message, 'error'); }
      }
    };
    el.onsubmit = async (e) => {
      e.preventDefault();
      const kind = e.target.dataset.form;
      if (!kind) return; // the login form handles itself
      const v = readForm(e.target);
      saving = true;
      render();
      try {
        if (kind === 'settings') {
          await run({ action: 'saveSettings', settings: v }, 'Settings saved');
        } else if (kind === 'bake') {
          v.opensAt = v.opensAt ? new Date(v.opensAt).toISOString() : '';
          v.deadline = v.deadline ? new Date(v.deadline).toISOString() : '';
          const isNew = openBake === 'new';
          const r = await run({ action: 'saveBake', bake: { ...v, id: isNew ? null : openBake } }, isNew ? `Created. Now pick its menu and pickup times.` : 'Saved');
          if (isNew) { openBake = r.id; bakeSel = r.id; store.set('mfb_admin_bake', bakeSel); }
        } else if (kind === 'item') {
          const chosen = [...e.target.querySelectorAll('[data-in-bake]')].map((x) => [x.dataset.inBake, x.checked]);
          const r = await run({ action: 'saveItem', item: { ...v, id: editing.id, image: editing.draft.image } }, 'Saved');
          const changes = chosen.filter(([bid, on]) => bakeById(bid) && bakeById(bid).menu.includes(r.id) !== on);
          for (const [bid, on] of changes) await act({ action: 'setBakeItem', bakeId: bid, itemId: r.id, on });
          if (changes.length) await refresh();
          editing = null;
        } else if (kind === 'pickup') {
          await run({ action: 'savePickup', bakeId: openBake, pickup: { ...v, id: editing.id || editing.draft.id || null, windows: editing.draft.windows || [] } }, 'Saved');
          editing = null;
        }
      } catch {
        if (editing) Object.assign(editing.draft, v);
      }
      saving = false;
      render();
    };
  }

  // Keep typed-in values when the editor re-renders (e.g. after picking a photo).
  function syncDraft() {
    const f = app().querySelector('form.editor');
    if (f && editing) Object.assign(editing.draft, readForm(f));
  }

  async function copyBakeList() {
    const b = bakeById(bakeSel);
    const sum = StoreCore.summarize(st, b.id);
    let text = `${b.name} baking list\n`;
    for (const r of sum.bakeList) text += `${r.total} × ${r.name}${sum.pickups.length > 1 ? ' (' + sum.pickups.map((p) => `${p}: ${r.byPickup[p] || 0}`).join(', ') + ')' : ''}\n`;
    for (const p of sum.slots) {
      text += `\n${p}\n`;
      for (const o of sum.orders.filter((x) => StoreCore.slotOf(x) === p))
        text += `- ${o.customer.name} (${o.customer.phone}): ${o.lines.map((l) => `${l.qty} ${l.name}`).join(', ')}${o.allergyNote ? ' | ALLERGY: ' + o.allergyNote : ''}\n`;
    }
    try {
      await navigator.clipboard.writeText(text);
      toast('Copied. Paste it into Notes or a text.');
    } catch {
      toast("Couldn't copy on this device.", 'error');
    }
  }

  async function start() {
    document.body.style.overflow = '';
    bind();
    app().innerHTML = '<div class="wrap"><p class="muted" style="padding:40px 0">Loading…</p></div>';
    try {
      await refresh();
      render();
    } catch (e) {
      if (e.status === 401) loginView();
      else app().innerHTML = `<div class="wrap"><div class="panel" style="margin-top:30px"><h2>Couldn't load</h2><p>${h(e.message)}</p></div></div>`;
    }
  }

  return { start };
})();
