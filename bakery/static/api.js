// Small helpers shared by the shop and admin views.
window.api = async function (method, path, body) {
  if (window.DemoBackend) return window.DemoBackend.handle(method, path, body);
  // Paths are relative to the page, so the shop works at bakery.<domain>/ or at /bakery/ on one server.
  const res = await fetch(path.replace(/^\//, ''), {
    method,
    credentials: 'same-origin',
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || 'Something went wrong. Please try again.'), { status: res.status });
  return data;
};

window.h = function (s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
};
window.money = (c) => '$' + (c / 100).toFixed(c % 100 === 0 ? 0 : 2);

window.store = {
  get(k, fallback) {
    try { const v = localStorage.getItem(k); return v == null ? fallback : JSON.parse(v); } catch { return fallback; }
  },
  set(k, v) {
    try { localStorage.setItem(k, JSON.stringify(v)); } catch {}
  }
};

window.fmtDate = function (iso, tz, withTime) {
  if (!iso) return '';
  const opts = { weekday: 'short', month: 'short', day: 'numeric', timeZone: tz || undefined };
  if (withTime) Object.assign(opts, { hour: 'numeric', minute: '2-digit' });
  try { return new Intl.DateTimeFormat('en-US', opts).format(new Date(iso)); } catch { return new Date(iso).toDateString(); }
};

window.toast = function (msg, kind) {
  let el = document.getElementById('toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    el.setAttribute('role', 'status');
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.className = 'toast show ' + (kind || '');
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.className = 'toast'), 3800);
};

// Simple drawings used until Mollie adds a real photo.
window.bakeArt = function (name) {
  const n = (name || '').toLowerCase();
  const bg = '<rect width="400" height="300" fill="#F3E7D6"/><circle cx="200" cy="150" r="118" fill="#EFDCC4"/>';
  let art;
  if (n.includes('pie')) {
    const choc = n.includes('choc');
    const fill = choc ? '#5A3426' : '#D98A3D';
    if (n.includes('slice')) {
      art = `<g transform="translate(200 160)"><path d="M-95 45 L0 -80 L95 45 Z" fill="${fill}"/><path d="M-95 45 L95 45 L95 62 L-95 62 Z" fill="#E2B577"/><path d="M-95 45 L0 -80 L95 45" fill="none" stroke="#C99252" stroke-width="10" stroke-linejoin="round"/>${choc ? '<ellipse cx="0" cy="5" rx="30" ry="12" fill="#FFF8EC"/>' : ''}</g>`;
    } else {
      let crimp = '';
      for (let i = 0; i < 24; i++) {
        const a = (i / 24) * Math.PI * 2;
        crimp += `<circle cx="${(200 + Math.cos(a) * 98).toFixed(1)}" cy="${(150 + Math.sin(a) * 98).toFixed(1)}" r="13" fill="#E2B577"/>`;
      }
      art = `<circle cx="200" cy="150" r="104" fill="#C99252"/>${crimp}<circle cx="200" cy="150" r="84" fill="${fill}"/>${choc ? '<circle cx="200" cy="150" r="34" fill="#FFF8EC"/><circle cx="200" cy="150" r="20" fill="#F5EAD8"/>' : '<circle cx="200" cy="150" r="84" fill="url(#g)"/>'}`;
    }
  } else if (n.includes('cinnamon')) {
    let pts = '';
    for (let t = 0; t <= Math.PI * 7; t += 0.15) {
      const r = 6 + t * 3.6;
      pts += `${(200 + Math.cos(t) * r).toFixed(1)},${(150 + Math.sin(t) * r).toFixed(1)} `;
    }
    art = `<circle cx="200" cy="150" r="92" fill="#D9A262"/><polyline points="${pts}" fill="none" stroke="#9A5A2C" stroke-width="9" stroke-linecap="round" stroke-linejoin="round"/><path d="M140 118 q30 -16 60 0 q30 16 60 0" fill="none" stroke="#FFF8EC" stroke-width="9" stroke-linecap="round" opacity=".9"/>`;
  } else if (n.includes('roll') || n.includes('bread') || n.includes('biscuit')) {
    art = [[-62, 18], [62, 18], [0, -30]].map(([x, y]) => `<g transform="translate(${200 + x} ${150 + y})"><ellipse rx="64" ry="50" fill="#D9A262"/><ellipse rx="54" ry="38" cy="-8" fill="#E8BC7E"/><path d="M-28 -14 q28 -16 56 0" fill="none" stroke="#C48A4A" stroke-width="5" stroke-linecap="round"/></g>`).join('');
  } else {
    art = '<circle cx="200" cy="150" r="80" fill="#D9A262"/><circle cx="175" cy="130" r="9" fill="#5A3426"/><circle cx="222" cy="142" r="9" fill="#5A3426"/><circle cx="190" cy="178" r="9" fill="#5A3426"/>';
  }
  return `<svg viewBox="0 0 400 300" xmlns="http://www.w3.org/2000/svg" role="img" aria-label=""><defs><radialGradient id="g"><stop offset="0" stop-color="#E9A65A" stop-opacity=".0"/><stop offset="1" stop-color="#B86B2A" stop-opacity=".35"/></radialGradient></defs>${bg}${art}</svg>`;
};
