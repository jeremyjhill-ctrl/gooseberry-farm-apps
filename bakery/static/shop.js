// Customer-facing shop: menu, cart, checkout and the order confirmation.
window.Shop = (function () {
  const app = () => document.getElementById('app');
  let data = null;
  let cart = store.get('mfb_cart', {});
  let form = Object.assign({ name: '', email: '', phone: '', pickupId: '', pickupWindowId: '', pickupTime: '', allergyNote: '', payMethod: 'card' }, store.get('mfb_form', {}));
  let sheetOpen = false;
  let busy = false;
  let error = '';

  const leafIcon = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 20A7 7 0 0 1 9.8 6.1C15.5 5 17 4.48 19 2c1 2 2 4.18 2 8 0 5.5-4.78 10-10 10Z"/><path d="M2 21c0-3 1.85-5.36 5.08-6C9.5 14.52 12 13 13 12"/></svg>';
  const lockIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>';

  function saveLocal() {
    store.set('mfb_cart', cart);
    store.set('mfb_form', form);
  }

  async function load() {
    data = await api('GET', '/api/shop');
    // Drop anything from the saved cart that's gone or over what's left.
    for (const id of Object.keys(cart)) {
      const item = data.items.find((i) => i.id === id);
      if (!item) delete cart[id];
      else cart[id] = Math.min(cart[id], maxFor(item));
      if (!cart[id]) delete cart[id];
    }
    saveLocal();
  }

  function maxFor(item) {
    const per = data.settings.perOrderMax || 12;
    return item.remaining == null ? per : Math.min(per, item.remaining);
  }
  const lines = () => data.items.filter((i) => cart[i.id] > 0).map((i) => ({ item: i, qty: cart[i.id] }));
  const count = () => lines().reduce((t, l) => t + l.qty, 0);
  const total = () => lines().reduce((t, l) => t + l.qty * l.item.price, 0);
  const payByApp = () => !!data.appPay && form.payMethod === 'app';
  const appDiscount = () => (data.appPay ? Math.round((total() * data.appPay.discountPct) / 100) : 0);
  const due = () => total() - (payByApp() ? appDiscount() : 0);
  function appNames(list) {
    return list.length < 3 ? list.join(' or ') : list.slice(0, -1).join(', ') + ' or ' + list[list.length - 1];
  }

  function photo(item) {
    return item.image ? `<img src="${h(item.image)}" alt="${h(item.name)}" loading="lazy" />` : bakeArt(item.name);
  }

  function stepper(item, size) {
    const q = cart[item.id] || 0;
    return `<div class="stepper ${size || ''}">
      <button type="button" data-dec="${item.id}" aria-label="One less ${h(item.name)}">−</button>
      <output aria-live="polite">${q}</output>
      <button type="button" data-inc="${item.id}" aria-label="One more ${h(item.name)}" ${q >= maxFor(item) ? 'disabled' : ''}>+</button>
    </div>`;
  }

  function card(item) {
    const open = data.ordering.open;
    const soldOut = item.remaining === 0;
    const badge = soldOut
      ? '<span class="badge out">Sold out</span>'
      : item.remaining != null && item.remaining <= 10
      ? `<span class="badge">Only ${item.remaining} left</span>`
      : '';
    const q = cart[item.id] || 0;
    let control = '';
    if (open && !soldOut) control = q ? stepper(item) : `<button class="btn ghost small add-btn" data-inc="${item.id}">Add</button>`;
    return `<article class="card">
      <div class="photo">${photo(item)}${badge}</div>
      <div class="body">
        <h3>${h(item.name)}</h3>
        <p class="desc">${h(item.description)}</p>
        <div class="row"><span class="price">${money(item.price)}</span>${control}</div>
      </div>
    </article>`;
  }

  // Pickup choices. When any place has dated windows, customers pick from one list in time order.
  function usesWindows() {
    return data.pickups.some((p) => p.windows && p.windows.length);
  }

  function pickupChoices() {
    const out = [];
    for (const p of data.pickups) {
      if (p.windows && p.windows.length)
        for (const w of p.windows) out.push({ value: `${p.id}|${w.id}`, text: `${w.label} · ${p.label}`, full: w.full, place: p, start: w.start });
      else out.push({ value: `${p.id}|`, text: p.label, full: p.full, place: p, start: '~' });
    }
    return out.sort((a, b) => (a.start < b.start ? -1 : a.start > b.start ? 1 : 0)); // plain order: '~' (no window) sorts last
  }

  function chosenPickup() {
    return pickupChoices().find((c) => c.value === `${form.pickupId}|${form.pickupWindowId || ''}` && !c.full) || null;
  }

  function pickupPicker() {
    const choices = pickupChoices();
    const chosen = chosenPickup();
    const open = choices.filter((c) => !c.full);
    return `<select name="pickupChoice" class="pickup-select" aria-label="Pickup time and place">
        ${open.length === 1 ? '' : `<option value="" ${chosen ? '' : 'selected'}>Choose a pickup time…</option>`}
        ${choices.map((c) => `<option value="${h(c.value)}" ${chosen && chosen.value === c.value ? 'selected' : ''} ${c.full ? 'disabled' : ''}>${h(c.text)}${c.full ? ' (full)' : ''}</option>`).join('')}
      </select>
      ${chosen && chosen.place.details ? `<div class="pickbox pickup-where"><b>${h(chosen.place.label)}</b><br /><span class="muted">${h(chosen.place.details)}</span></div>` : ''}`;
  }

  function sheet() {
    const s = data.settings;
    const ls = lines();
    if (usesWindows() && !chosenPickup()) {
      const open = pickupChoices().filter((c) => !c.full);
      if (open.length === 1) [form.pickupId, form.pickupWindowId] = open[0].value.split('|');
    }
    const chosen = usesWindows() ? chosenPickup() : null;
    const askTime = s.askPickupTime && (!usesWindows() || (chosen && !(chosen.place.windows || []).length));
    const pickups = data.pickups
      .map((p) => {
        const on = form.pickupId === p.id;
        return `<label class="pick ${on ? 'on' : ''} ${p.full ? 'full' : ''}">
          <input type="radio" name="pickupId" value="${p.id}" ${on ? 'checked' : ''} ${p.full ? 'disabled' : ''} />
          <span><b>${h(p.label)}${p.full ? ' (full)' : ''}</b><small>${h(p.details)}</small></span>
        </label>`;
      })
      .join('');
    return `<div class="sheet-bg" data-close>
      <form class="sheet" id="checkout" novalidate role="dialog" aria-modal="true" aria-labelledby="co-title">
        <div class="sheet-head"><h2 id="co-title">Your order</h2><button type="button" class="x" data-close aria-label="Close">×</button></div>
        ${
          ls.length
            ? `<div class="lines">${ls
                .map((l) => `<div class="line"><span class="nm">${h(l.item.name)}</span>${stepper(l.item)}<span class="amt">${money(l.qty * l.item.price)}</span></div>`)
                .join('')}${payByApp() && appDiscount() ? `<div class="line"><span class="nm">${h(data.appPay.discountPct)}% off for paying by app</span><span class="amt">−${money(appDiscount())}</span></div>` : ''}<div class="total-row"><span>Total</span><span>${money(due())}</span></div></div>`
            : '<p class="empty">Your order is empty.</p>'
        }
        <div class="section-title">Pickup</div>
        ${usesWindows() ? pickupPicker() : `<div class="pickups">${pickups}</div>`}
        ${askTime ? `<label class="field" style="margin-top:12px"><span>Estimated pickup time <span class="muted">(optional)</span></span><input type="text" name="pickupTime" maxlength="60" placeholder="e.g. around 11am" value="${h(form.pickupTime)}" /></label>` : ''}
        <div class="section-title">Your details</div>
        <label class="field"><span>Name for the order</span><input type="text" name="name" autocomplete="name" required maxlength="80" value="${h(form.name)}" /></label>
        <div class="grid2">
          <label class="field"><span>Email</span><input type="email" name="email" autocomplete="email" inputmode="email" required value="${h(form.email)}" /><small>Your receipt goes here.</small></label>
          <label class="field"><span>Phone</span><input type="tel" name="phone" autocomplete="tel" inputmode="tel" required value="${h(form.phone)}" /><small>For last-minute pickup questions.</small></label>
        </div>
        ${s.askAllergies ? `<label class="field"><span>Allergies <span class="muted">(optional)</span></span><textarea name="allergyNote" maxlength="500" placeholder="${h(s.allergyPrompt)}">${h(form.allergyNote)}</textarea></label>` : ''}
        ${data.appPay ? payChoice() : ''}
        ${error ? `<div class="form-error" role="alert">${h(error)}</div>` : ''}
        <button class="btn block" type="submit" style="margin-top:16px;min-height:54px;font-size:17px" ${busy || !ls.length ? 'disabled' : ''}>${busy ? (payByApp() ? 'Placing order…' : 'Starting checkout…') : payByApp() ? `Place order · ${money(due())}` : `Continue to payment · ${money(total())}`}</button>
        <div class="secure">${payByApp() ? `You'll get ${h(appNames(data.appPay.apps))} payment details on the next screen.` : `${lockIcon} ${data.demo ? 'Demo mode: no card is charged.' : 'Secure payment by Stripe.'}`}</div>
      </form>
    </div>`;
  }

  function payChoice() {
    const a = data.appPay;
    const opt = (val, title, sub) =>
      `<label class="pick ${form.payMethod === val ? 'on' : ''}"><input type="radio" name="payMethod" value="${val}" ${form.payMethod === val ? 'checked' : ''} /><span><b>${title}</b><small>${sub}</small></span></label>`;
    return `<div class="section-title">Payment</div><div class="pickups">
      ${opt('card', 'Card, Apple Pay or Google Pay', `Pay now · ${money(total())}`)}
      ${opt('app', h(appNames(a.apps)), `${a.discountPct ? `Save ${h(a.discountPct)}% · ${money(total() - appDiscount())}. ` : ''}Send payment within ${h(a.hours)} hours to keep your order.`)}
    </div>`;
  }

  function render() {
    const s = data.settings;
    const n = count();
    const deadline = s.deadline ? `<span class="chip berry">Order by ${h(fmtDate(s.deadline, s.timezone))}</span>` : '';
    const pickupChips = data.pickups.map((p) => `<span class="chip">${h(p.label)}</span>`).join('');
    app().innerHTML = `
      <header class="hero wrap">
        <div class="eyebrow">${h(s.season)} preorders</div>
        <h1>${h(s.shopName)}</h1>
        ${s.tagline ? `<p class="tagline">${h(s.tagline)}</p>` : ''}
        ${s.intro ? `<p class="intro">${h(s.intro)}</p>` : ''}
        <div class="chips">${deadline}${pickupChips}</div>
        ${s.dietaryNote ? `<div class="dietary">${leafIcon}<span>${h(s.dietaryNote)}</span></div>` : ''}
      </header>
      <main class="wrap">
        ${data.demo ? '<div class="banner demo">Demo mode: orders go through without a real payment.</div>' : ''}
        ${data.ordering.open ? '' : `<div class="banner closed">${h(data.ordering.reason)}</div>`}
        <section class="menu" aria-label="Menu">${data.items.map(card).join('')}</section>
      </main>
      <footer class="site"><div class="wrap">
        ${s.contactNote ? `<p>${h(s.contactNote)}</p>` : ''}
        <p><a class="quiet" href="#admin">Baker login</a></p>
      </div></footer>
      ${n && data.ordering.open && !sheetOpen ? `<div class="cartbar"><button class="btn" data-open><span>Review order · ${n} item${n > 1 ? 's' : ''}</span><span>${money(total())} →</span></button></div>` : ''}
      ${sheetOpen ? sheet() : ''}`;
    document.body.style.overflow = sheetOpen ? 'hidden' : '';
  }

  function bump(id, d) {
    const item = data.items.find((i) => i.id === id);
    if (!item) return;
    cart[id] = Math.max(0, Math.min(maxFor(item), (cart[id] || 0) + d));
    if (!cart[id]) delete cart[id];
    if (d > 0 && cart[id] === maxFor(item) && item.remaining != null && item.remaining === cart[id]) toast(`That's the last of the ${item.name}!`);
    saveLocal();
    error = '';
    const scroll = sheetOpen && document.querySelector('.sheet') ? document.querySelector('.sheet').scrollTop : null;
    render();
    if (scroll != null && document.querySelector('.sheet')) document.querySelector('.sheet').scrollTop = scroll;
  }

  async function submit() {
    error = '';
    if (usesWindows() ? !chosenPickup() : !form.pickupId) error = usesWindows() ? 'Please choose a pickup time.' : 'Please choose a pickup option.';
    else if (!form.name.trim()) error = 'Please add a name for the order.';
    else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(form.email.trim())) error = 'Please check your email address.';
    else if (form.phone.replace(/\D/g, '').length < 10) error = 'Please add a phone number Mollie can reach you at.';
    if (error) return render();
    busy = true;
    render();
    try {
      const res = await api('POST', '/api/checkout', {
        ...form,
        pickupWindowId: usesWindows() ? form.pickupWindowId : '',
        payMethod: payByApp() ? 'app' : 'card',
        cart: Object.entries(cart).map(([itemId, qty]) => ({ itemId, qty }))
      });
      if (window.DemoBackend) {
        busy = false;
        sheetOpen = false;
        return showOrder(res.orderId);
      }
      location.href = res.url;
    } catch (e) {
      busy = false;
      error = e.message;
      try { await load(); } catch {}
      render();
    }
  }

  async function showOrder(id, sessionId) {
    document.body.style.overflow = '';
    app().innerHTML = '<div class="confirm"><p class="muted">Loading your order…</p></div>';
    let order;
    for (let i = 0; i < 10; i++) {
      try {
        order = await api('GET', `/api/orders/${encodeURIComponent(id)}${sessionId ? '?session_id=' + encodeURIComponent(sessionId) : ''}`);
      } catch (e) {
        app().innerHTML = `<div class="confirm"><h1>Order not found</h1><p class="muted">${h(e.message)}</p><a class="btn" href="./">Back to the shop</a></div>`;
        return;
      }
      if (order.status !== 'pending') break;
      app().innerHTML = '<div class="confirm"><p class="muted">Confirming your payment…</p></div>';
      await new Promise((r) => setTimeout(r, 2000));
    }
    if (order.status === 'paid' || order.status === 'awaiting') {
      cart = {};
      saveLocal();
    }
    if (order.status === 'awaiting') return showAwaiting(order);
    const paid = order.status === 'paid';
    const shop = data || (await api('GET', '/api/shop').catch(() => null));
    const thanks = (shop && shop.settings && shop.settings.thankYouNote) || '';
    app().innerHTML = `<div class="wrap"><div class="confirm">
      <div class="check"><svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg></div>
      <h1>${paid ? `Order #${order.number} is in!` : order.status === 'pending' ? 'Almost there…' : 'Order not completed'}</h1>
      <p class="muted">${
        paid
          ? `Thanks, ${h(order.name)}. A receipt is on its way to ${h(order.email)}.`
          : order.status === 'pending'
          ? "We're still waiting for the payment to confirm. Refresh this page in a minute."
          : "This order wasn't paid, so it isn't reserved. You're welcome to start a new one."
      }</p>
      ${orderLines(order)}
      ${pickupBox(order)}
      ${paid && thanks ? `<p>${h(thanks)}</p>` : ''}
      <a class="btn ghost" href="./" data-home>Back to the shop</a>
    </div></div>`;
  }

  function orderLines(order) {
    return `<div class="lines">${order.lines.map((l) => `<div class="line"><span class="nm">${l.qty} × ${h(l.name)}</span><span class="amt">${money(l.qty * l.price)}</span></div>`).join('')}
      ${order.discount ? `<div class="line"><span class="nm">App payment discount</span><span class="amt">−${money(order.discount)}</span></div>` : ''}
      <div class="total-row"><span>Total</span><span>${money(order.total)}</span></div></div>`;
  }
  function pickupBox(order) {
    return `<div class="pickbox"><b>Pickup: ${order.pickupWhen ? `${h(order.pickupWhen)}<br />${h(order.pickupLabel)}` : h(order.pickupLabel)}</b><br /><span class="muted">${h(order.pickupDetails)}</span>${order.pickupTime ? `<br /><span class="muted">You said: ${h(order.pickupTime)}</span>` : ''}</div>`;
  }

  // Order placed with Venmo / Cash App / PayPal: show exactly what to send and where.
  function showAwaiting(order) {
    const memo = `Order #${order.number}`;
    app().innerHTML = `<div class="wrap"><div class="confirm">
      <div class="check" style="background:var(--berry-soft);color:var(--berry)"><svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg></div>
      <h1>Order #${order.number} is reserved</h1>
      <p class="muted">Send <b style="color:var(--ink)">${money(order.total)}</b> by ${h(fmtDate(order.payBy, order.timezone, true))} to finish your order. After that, it's released.</p>
      <div class="pay-apps">${(order.appLinks || [])
        .map((a) =>
          a.url
            ? `<a class="btn block" href="${h(a.url)}" target="_blank" rel="noopener">Pay with ${h(a.app)} <span class="handle">${h(a.handle)}</span></a>`
            : `<button type="button" class="btn block" data-demo-pay="${h(a.app)}">Pay with ${h(a.app)} <span class="handle">${h(a.handle)}</span></button>`
        )
        .map((html, i) => {
          const a = order.appLinks[i];
          return a.copy ? `<div>${html}<button type="button" class="btn link small" data-copy-memo="${h(a.copy)}">Copy ${h(a.app)} email</button></div>` : html;
        })
        .join('')}</div>
      <div class="memo"><span>Payment note: <b>${h(memo)}</b></span><button type="button" class="btn ghost small" data-copy-memo="${h(memo)}">Copy</button></div>
      ${order.appPayNote ? `<p class="small muted">${h(order.appPayNote)}</p>` : ''}
      ${orderLines(order)}
      ${pickupBox(order)}
      <p class="small muted">Mollie will confirm once she sees your payment. Questions? Reply to her on Facebook or by email.</p>
      <a class="btn ghost" href="./" data-home>Back to the shop</a>
    </div></div>`;
  }

  function bind() {
    const el = app();
    el.onclick = (e) => {
      const t = e.target.closest('[data-inc],[data-dec],[data-open],[data-close],[data-home],[data-copy-memo],[data-demo-pay]');
      if (!t) return;
      if (t.dataset.demoPay) return toast(`In the real shop, this opens ${t.dataset.demoPay} with the amount filled in.`);
      if (t.dataset.copyMemo) {
        navigator.clipboard.writeText(t.dataset.copyMemo).then(() => toast('Copied'), () => toast('Type it in the note: ' + t.dataset.copyMemo));
        return;
      }
      if (t.dataset.inc) bump(t.dataset.inc, 1);
      else if (t.dataset.dec) bump(t.dataset.dec, -1);
      else if ('open' in t.dataset) { sheetOpen = true; error = ''; render(); }
      else if ('close' in t.dataset && (t === e.target || t.classList.contains('x'))) { sheetOpen = false; render(); }
      else if ('home' in t.dataset && window.DemoBackend) { e.preventDefault(); start(); }
    };
    el.oninput = (e) => {
      const f = e.target;
      if (f.name === 'pickupChoice') {
        [form.pickupId, form.pickupWindowId] = (f.value || '|').split('|');
        error = '';
        saveLocal();
        const sh = document.querySelector('.sheet');
        const top = sh ? sh.scrollTop : 0;
        render();
        if (document.querySelector('.sheet')) document.querySelector('.sheet').scrollTop = top;
        return;
      }
      if (f.name && f.name in form) {
        form[f.name] = f.value;
        saveLocal();
        if (f.name === 'pickupId' || f.name === 'payMethod') {
          error = '';
          const sh = document.querySelector('.sheet');
          const top = sh ? sh.scrollTop : 0;
          render();
          if (document.querySelector('.sheet')) document.querySelector('.sheet').scrollTop = top;
        }
      }
    };
    el.onchange = el.oninput;
    el.onsubmit = (e) => {
      e.preventDefault();
      if (!busy) submit();
    };
    document.onkeydown = (e) => {
      if (e.key === 'Escape' && sheetOpen) { sheetOpen = false; render(); }
    };
  }

  async function start() {
    bind();
    sheetOpen = false;
    const q = new URLSearchParams(location.search);
    if (q.get('order')) {
      const id = q.get('order');
      const sid = q.get('session_id');
      history.replaceState(null, '', location.pathname);
      return showOrder(id, sid);
    }
    if (q.get('cancelled')) {
      api('POST', `/api/orders/${encodeURIComponent(q.get('cancelled'))}/cancel`).catch(() => {});
      history.replaceState(null, '', location.pathname);
      setTimeout(() => toast('Checkout cancelled. Your order is still here when you’re ready.'), 300);
    }
    try {
      await load();
      render();
    } catch (e) {
      app().innerHTML = `<div class="wrap"><div class="confirm"><h1>We couldn't load the menu</h1><p class="muted">${h(e.message)}</p><button class="btn" onclick="location.reload()">Try again</button></div></div>`;
    }
  }

  return { start, showOrder };
})();
