// Mollie's admin: orders + bake list, menu (with photos), pickup options, shop settings.
window.Admin = (function () {
  const app = () => document.getElementById('app');
  let st = null; // full store state from the server
  let tab = store.get('mfb_admin_tab', 'orders');
  let season = null;
  let editing = null; // { kind: 'item'|'pickup', id, draft }
  let armed = null; // id of a delete button waiting for a second tap
  let saving = false;

  const act = (body) => api('POST', '/api/admin/action', body);

  async function refresh() {
    st = await api('GET', '/api/admin/state');
    if (!season || !st.orders.some((o) => o.season === season)) season = st.settings.season;
  }

  // ---------- views ----------
  function loginView(msg) {
    app().innerHTML = `<div class="wrap"><form class="panel login" id="login">
      <h2>Baker login</h2><p class="muted small">For Mollie only.</p>
      <label class="field"><span>Password</span><input type="password" name="password" autocomplete="current-password" required /></label>
      ${msg ? `<div class="form-error">${h(msg)}</div>` : ''}
      <button class="btn block" type="submit">Sign in</button>
      <p class="small" style="text-align:center"><a href="#">Back to the shop</a></p>
    </form></div>`;
    document.getElementById('login').onsubmit = async (e) => {
      e.preventDefault();
      try {
        await api('POST', '/api/admin/login', { password: e.target.password.value });
        start();
      } catch (err) {
        loginView(err.message);
      }
    };
  }

  function statusStrip() {
    const s = st.settings;
    const past = s.deadline && Date.now() > Date.parse(s.deadline);
    const open = s.orderingOpen && !past;
    const why = !s.orderingOpen ? 'Preorders are turned off' : past ? `Deadline passed (${fmtDate(s.deadline, s.timezone, true)})` : `Preorders are open until ${fmtDate(s.deadline, s.timezone, true) || 'you close them'}`;
    return `<div class="status-strip ${open ? 'open' : 'closed'}"><span><span class="dot"></span>${h(why)}</span>
      <button class="btn small ${open ? 'danger' : ''}" data-toggle-open>${s.orderingOpen ? 'Stop orders now' : 'Turn orders on'}</button></div>`;
  }

  function ordersTab() {
    const sum = StoreCore.summarize(st, season, Date.now());
    const seasons = [...new Set([st.settings.season, ...sum.seasons])];
    const itemCount = sum.bakeList.reduce((t, r) => t + r.total, 0);
    const bake = sum.bakeList.length
      ? `<div class="table-scroll"><table class="bake"><thead><tr><th>Item</th>${sum.pickups.map((p) => `<th class="n">${h(p)}</th>`).join('')}<th class="n">Total</th></tr></thead><tbody>
        ${sum.bakeList.map((r) => `<tr><td>${h(r.name)}</td>${sum.pickups.map((p) => `<td class="n">${r.byPickup[p] || '–'}</td>`).join('')}<td class="n"><b>${r.total}</b></td></tr>`).join('')}
        </tbody></table></div>`
      : '<p class="empty">No paid orders yet.</p>';
    const groups = sum.slots
      .map((p) => {
        const os = sum.orders.filter((o) => StoreCore.slotOf(o) === p).sort((a, b) => a.customer.name.localeCompare(b.customer.name));
        const done = os.filter((o) => o.pickedUp).length;
        return `<div class="group-title">${h(p)} <span class="muted small">${done}/${os.length} picked up</span></div>${os.map(orderCard).join('')}`;
      })
      .join('');
    return `${statusStrip()}
      <div class="panel-head"><select data-season style="max-width:260px">${seasons.map((x) => `<option ${x === season ? 'selected' : ''}>${h(x)}</option>`).join('')}</select>
        <div class="row-actions"><button class="btn ghost small" data-copy>Copy bake list</button>${window.DemoBackend ? '' : `<a class="btn ghost small" href="api/admin/orders.csv?season=${encodeURIComponent(season)}">Download spreadsheet</a>`}</div></div>
      <div class="stats"><div class="stat"><b>${sum.orders.length}</b><span>paid orders</span></div><div class="stat"><b>${itemCount}</b><span>items to bake</span></div><div class="stat"><b>${money(sum.revenue)}</b><span>collected</span></div></div>
      ${sum.awaiting.length ? `<div class="panel awaiting-box"><h2>Waiting on payment</h2><p class="muted small">Venmo, Cash App or PayPal orders. Tap <b>Mark paid</b> when the money shows up. Their items are held until the deadline shown.</p>${sum.awaiting.map(awaitingCard).join('')}</div>` : ''}
      <div class="panel"><h2>Bake list</h2><p class="muted small">Everything paid for, split by pickup.${sum.awaiting.length ? ` Not counting ${sum.awaiting.length} order${sum.awaiting.length > 1 ? 's' : ''} still waiting on payment.` : ''}</p>${bake}</div>
      <div class="panel"><h2>Orders</h2>${groups || '<p class="empty">Orders show up here once they’re paid.</p>'}</div>`;
  }

  function awaitingCard(o) {
    const tel = o.customer.phone.replace(/[^\d+]/g, '');
    return `<div class="order">
      <div class="top"><div class="who"><b>${h(o.customer.name)}</b> <span class="muted small">#${o.number}</span>
        <div class="meta"><a href="tel:${h(tel)}">${h(o.customer.phone)}</a> · ${h(StoreCore.slotOf(o))}</div></div>
        <b>${money(o.total)}</b></div>
      <ul>${o.lines.map((l) => `<li>${l.qty} × ${h(l.name)}</li>`).join('')}</ul>
      <div class="meta" style="margin-top:6px">${o.overdue ? '<span class="pill warn" style="margin:0">Past due, items released</span>' : `Pay by ${h(fmtDate(o.payBy, st.settings.timezone, true))}`}</div>
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

  function itemEditor(d) {
    return `<form class="editor" data-form="item">
      <div class="photo-pick"><div class="thumb">${d.image ? `<img src="${h(d.image)}" alt="" />` : bakeArt(d.name)}</div>
        <div><label class="btn ghost small" style="cursor:pointer">${d.image ? 'Change photo' : 'Add photo'}<input type="file" accept="image/*" data-photo hidden /></label>
        ${d.image ? '<button type="button" class="btn link small" data-rmphoto>Remove photo</button>' : ''}
        <div class="muted small" style="margin-top:6px">A square-ish or landscape photo looks best.</div></div></div>
      <label class="field"><span>Name</span><input type="text" name="name" value="${h(d.name)}" maxlength="80" required /></label>
      <label class="field"><span>Short description</span><textarea name="description" maxlength="400" rows="2">${h(d.description)}</textarea><small>One line on the menu card, e.g. “Whole 9-inch pie.”</small></label>
      <label class="field"><span>Full description <span class="muted">(optional)</span></span><textarea name="details" maxlength="2000" rows="5" placeholder="Tell customers why they’ll love it.">${h(d.details || '')}</textarea><small>Shown when a customer taps the item. Press Return for a new paragraph.</small></label>
      <label class="field"><span>Contains <span class="muted">(optional)</span></span><input type="text" name="contains" maxlength="120" value="${h(d.contains || '')}" placeholder="e.g. coconut, eggs" /><small>Shown as a tag on the menu card so allergies are easy to spot.</small></label>
      <div class="grid2">
        <label class="field"><span>Price ($)</span><input type="number" name="price" inputmode="decimal" step="0.01" min="0.5" value="${d.price}" required /></label>
        <label class="field"><span>How many can you make?</span><input type="number" name="limit" inputmode="numeric" min="0" step="1" value="${d.limit == null ? '' : d.limit}" placeholder="No limit" /><small>Leave blank for no limit. It shows "sold out" when they're gone.</small></label>
      </div>
      <label class="switch"><input type="checkbox" name="active" ${d.active ? 'checked' : ''} /> Show on the shop</label>
      ${editorButtons(d.id)}
    </form>`;
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
      ${editorButtons(d.id)}
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

  function menuTab() {
    const rows = st.items
      .map((i, idx) => {
        if (editing && editing.kind === 'item' && editing.id === i.id) return itemEditor(editing.draft);
        const left = st.stock[i.id];
        return `<div class="item-row"><div class="mover"><button data-move-item="${i.id}" data-dir="-1" ${idx === 0 ? 'disabled' : ''} aria-label="Move up">▲</button><button data-move-item="${i.id}" data-dir="1" ${idx === st.items.length - 1 ? 'disabled' : ''} aria-label="Move down">▼</button></div>
          <div class="thumb">${i.image ? `<img src="${h(i.image)}" alt="" />` : bakeArt(i.name)}</div>
          <div class="info"><b>${h(i.name)}${i.active ? '' : '<span class="pill">Hidden</span>'}${left === 0 ? '<span class="pill warn">Sold out</span>' : ''}</b>
          <span>${money(i.price)} · ${i.limit == null ? 'no limit' : `${left} of ${i.limit} left`}</span></div>
          <button class="btn ghost small" data-edit-item="${i.id}">Edit</button></div>`;
      })
      .join('');
    const adding = editing && editing.kind === 'item' && !editing.id;
    return `<div class="panel"><div class="panel-head"><div><h2>Menu</h2><p class="muted small" style="margin:0">What customers can order, in this order.</p></div>
      ${adding ? '' : '<button class="btn small" data-add-item>+ Add item</button>'}</div>
      ${adding ? itemEditor(editing.draft) : ''}${rows || '<p class="empty">No items yet.</p>'}</div>`;
  }

  function pickupTab() {
    const rows = st.pickups
      .map((p, idx) => {
        if (editing && editing.kind === 'pickup' && editing.id === p.id) return pickupEditor(editing.draft);
        const n = st.orders.filter((o) => o.season === st.settings.season && o.pickupId === p.id && o.status === 'paid').length;
        return `<div class="item-row"><div class="mover"><button data-move-pickup="${p.id}" data-dir="-1" ${idx === 0 ? 'disabled' : ''} aria-label="Move up">▲</button><button data-move-pickup="${p.id}" data-dir="1" ${idx === st.pickups.length - 1 ? 'disabled' : ''} aria-label="Move down">▼</button></div>
          <div class="info"><b>${h(p.label)}${p.active ? '' : '<span class="pill">Off</span>'}</b><span>${h(p.details)}</span><br /><span>${n} order${n === 1 ? '' : 's'}${p.capacity == null ? '' : ` of ${p.capacity} max`}</span>
          ${(p.windows || []).map((w) => {
            const k = st.orders.filter((o) => o.season === st.settings.season && o.pickupWindowId === w.id && o.status === 'paid').length;
            return `<br /><span class="small">${h(windowLabel(w))} · ${k}${w.capacity == null ? '' : ` of ${w.capacity}`} order${k === 1 && w.capacity == null ? '' : 's'}</span>`;
          }).join('')}</div>
          <button class="btn ghost small" data-edit-pickup="${p.id}">Edit</button></div>`;
      })
      .join('');
    const adding = editing && editing.kind === 'pickup' && !editing.id;
    return `<div class="panel"><div class="panel-head"><div><h2>Pickup options</h2><p class="muted small" style="margin:0">Customers choose one of these at checkout.</p></div>
      ${adding ? '' : '<button class="btn small" data-add-pickup>+ Add pickup</button>'}</div>
      ${adding ? pickupEditor(editing.draft) : ''}${rows || '<p class="empty">No pickup options yet.</p>'}</div>`;
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
    return `<form class="panel" data-form="settings"><h2>Shop settings</h2><p class="muted small">Changes show on the shop as soon as you save.</p>
      ${f('season', 'Sale name', 'e.g. "Thanksgiving 2026" or "Christmas 2026". Orders are grouped by this, so change it when you start a new holiday and last season’s orders stay separate.')}
      <label class="field"><span>Order deadline</span><input type="datetime-local" name="deadline" value="${localInput(s.deadline)}" /><small>Orders close automatically after this.</small></label>
      <label class="switch"><input type="checkbox" name="orderingOpen" ${s.orderingOpen ? 'checked' : ''} /> Taking preorders</label>
      ${f('shopName', 'Shop name')}
      ${f('tagline', 'Tagline')}
      ${f('intro', 'Welcome message', 'Shown at the top of the order page. Say what you’re offering, the order deadline, and when and where pickup is. Press Return for a new paragraph.', 'area')}
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
      ${window.DemoBackend ? '<button class="btn ghost" type="button" data-reset-demo style="margin-left:8px">Reset demo</button>' : ''}
    </form>`;
  }

  function render() {
    const tabs = [['orders', 'Orders'], ['menu', 'Menu'], ['pickup', 'Pickup'], ['settings', 'Settings']];
    const body = tab === 'menu' ? menuTab() : tab === 'pickup' ? pickupTab() : tab === 'settings' ? settingsTab() : ordersTab();
    app().innerHTML = `<div class="admin-top"><div class="wrap"><h1>${h(st.settings.shopName)}</h1>
        <a class="btn ghost small" href="#">View shop</a>${window.DemoBackend ? '' : '<button class="btn link small" data-logout>Sign out</button>'}</div>
        <nav class="tabs">${tabs.map(([k, l]) => `<button class="tab ${tab === k ? 'on' : ''}" data-tab="${k}">${l}</button>`).join('')}</nav></div>
      <main class="wrap admin-main">${st.demo && !window.DemoBackend ? '<div class="banner demo">Demo mode: no Stripe key is set, so orders are marked paid without a payment.</div>' : ''}${body}</main>`;
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
    const max = window.DemoBackend ? 900 : 1400;
    const url = URL.createObjectURL(file);
    try {
      const img = await new Promise((res, rej) => {
        const i = new Image();
        i.onload = () => res(i);
        i.onerror = () => rej(new Error("That photo couldn't be opened. Try a JPG or PNG."));
        i.src = url;
      });
      const scale = Math.min(1, max / Math.max(img.width, img.height));
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

  function bind() {
    const el = app();
    el.onclick = async (e) => {
      const t = e.target.closest('button, a');
      if (!t) return;
      const d = t.dataset;
      if (d.tab) { tab = d.tab; editing = null; store.set('mfb_admin_tab', tab); render(); }
      else if ('logout' in d) { await api('POST', '/api/admin/logout'); location.hash = ''; }
      else if ('toggleOpen' in d) { await run({ action: 'saveSettings', settings: { orderingOpen: !st.settings.orderingOpen } }, st.settings.orderingOpen ? 'Orders stopped' : 'Orders are on'); render(); }
      else if ('copy' in d) copyBakeList();
      else if (d.markPaid) { await run({ action: 'markPaid', id: d.markPaid }, 'Marked paid'); render(); }
      else if (d.editItem) { const i = st.items.find((x) => x.id === d.editItem); editing = { kind: 'item', id: i.id, draft: { ...i, price: (i.price / 100).toFixed(2) } }; render(); }
      else if ('addItem' in d) { editing = { kind: 'item', id: null, draft: { name: '', description: '', details: '', contains: '', price: '', limit: null, active: true, image: '' } }; render(); }
      else if (d.editPickup) { const p = st.pickups.find((x) => x.id === d.editPickup); editing = { kind: 'pickup', id: p.id, draft: { ...p, windows: (p.windows || []).map((w) => ({ ...w })) } }; render(); }
      else if ('addPickup' in d) { editing = { kind: 'pickup', id: null, draft: { label: '', details: '', capacity: null, active: true, windows: [] } }; render(); }
      else if ('addWindow' in d) addWindow();
      else if (d.rmWindow) { syncDraft(); editing.draft.windows.splice(Number(d.rmWindow), 1); render(); }
      else if ('cancelEdit' in d) { editing = null; render(); }
      else if ('rmphoto' in d) { syncDraft(); editing.draft.image = ''; render(); }
      else if (d.moveItem) { await run({ action: 'moveItem', id: d.moveItem, dir: Number(d.dir) }); render(); }
      else if (d.movePickup) { await run({ action: 'movePickup', id: d.movePickup, dir: Number(d.dir) }); render(); }
      else if (d.delete) {
        if (armed !== d.delete) return arm(d.delete);
        armed = null;
        await run({ action: editing.kind === 'item' ? 'deleteItem' : 'deletePickup', id: d.delete }, 'Deleted');
        editing = null;
        render();
      } else if (d.cancelOrder) {
        if (armed !== 'o' + d.cancelOrder) return arm('o' + d.cancelOrder);
        armed = null;
        await run({ action: 'cancelOrder', id: d.cancelOrder }, 'Order removed. Refund it in Stripe if needed.');
        render();
      } else if ('resetDemo' in d) { window.DemoBackend.reset(); await refresh(); render(); toast('Demo reset'); }
    };
    el.oninput = (e) => {
      const t = e.target;
      if (t.dataset.w != null && editing && editing.draft.windows) editing.draft.windows[Number(t.dataset.w)][t.dataset.f] = t.value;
    };
    el.onchange = async (e) => {
      const t = e.target;
      if (t.dataset.w != null) return el.oninput(e);
      if (t.dataset.picked) { await run({ action: 'setPickedUp', id: t.dataset.picked, pickedUp: t.checked }); render(); }
      else if ('season' in t.dataset) { season = t.value; render(); }
      else if ('photo' in t.dataset && t.files[0]) {
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
          if (v.deadline) v.deadline = new Date(v.deadline).toISOString();
          await run({ action: 'saveSettings', settings: v }, 'Settings saved');
        } else if (kind === 'item') {
          await run({ action: 'saveItem', item: { ...v, id: editing.id, image: editing.draft.image } }, 'Saved');
          editing = null;
        } else if (kind === 'pickup') {
          await run({ action: 'savePickup', pickup: { ...v, id: editing.id, windows: editing.draft.windows || [] } }, 'Saved');
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
    const sum = StoreCore.summarize(st, season);
    let text = `${season} bake list\n`;
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
