// One save, once: a form's button locks the moment it's clicked, so a double-click (or an impatient
// second tap on a slow phone) never sends the same form twice. Coming back with the Back button unlocks it.
//
// Safety net (Nov 26, 2026): if the next page hasn't arrived after about 15 seconds (Safari sometimes stalls
// between pages), the button unlocks and says so, so nobody is left looking at a frozen "Working..." button.
// Pressing it again is safe: a repeated checkout or payment just starts fresh, and an unfinished one lets go
// of its items on its own.
(function () {
  var WAIT_MS = window.ONCE_WAIT_MS || 15000;  // tests can shorten this

  function unlock(form) {
    delete form.dataset.sending;
    clearTimeout(Number(form.dataset.onceTimer || 0));
    delete form.dataset.onceTimer;
    form.querySelectorAll('button, input[type=submit]').forEach(function (b) {
      b.disabled = false;
      if (b.dataset.label) { b.innerHTML = b.dataset.label; delete b.dataset.label; }
    });
  }

  function slowNotice(form, btn) {
    var old = form.querySelector('[data-once-slow]');
    if (old) old.remove();
    var note = document.createElement('div');
    note.setAttribute('data-once-slow', '');
    note.setAttribute('role', 'alert');
    note.style.cssText = 'border:2px solid #B5483B;background:#FBEDEA;color:#7A2C1F;border-radius:12px;padding:10px 14px;font-weight:700;margin:8px 0';
    note.textContent = 'This is taking longer than usual. Check your connection, then tap the button again.';
    var spot = btn && btn.parentNode && form.contains(btn) ? btn : null;
    if (spot) spot.parentNode.insertBefore(note, spot); else form.insertBefore(note, form.firstChild);
    note.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (e.defaultPrevented || form.hasAttribute('data-no-lock')) return;
    if ((form.getAttribute('method') || 'get').toLowerCase() !== 'post') return;  // searches just load a page
    if (form.dataset.sending === '1') { e.preventDefault(); return; }
    form.dataset.sending = '1';
    var btn = e.submitter || form.querySelector('button:not([type=button]), input[type=submit]');
    var old = form.querySelector('[data-once-slow]');
    if (old) old.remove();
    setTimeout(function () {  // after the browser has read the button's name/value into the form
      form.querySelectorAll('button:not([type=button]), input[type=submit]').forEach(function (b) {
        b.disabled = true;
        if (b === btn && b.tagName === 'BUTTON') { b.dataset.label = b.innerHTML; b.textContent = 'Working…'; }
      });
    }, 0);
    form.dataset.onceTimer = String(setTimeout(function () {
      if (form.dataset.sending !== '1') return;
      unlock(form);
      slowNotice(form, btn);
    }, WAIT_MS));
  });

  // Links too (Nov 26, 2026): a page that hasn't arrived after about 15 seconds gets a note at the top with a Try again
  // button. This happens when a click lands just as the app restarts for an update: Render's front door can sit on it
  // for close to two minutes, while a fresh request goes straight through. (Tapping the link itself again doesn't help:
  // browsers ignore a second click on a page that's already loading, so the button cancels the stuck load first.)
  var linkTimer = 0;
  function linkNotice(href) {
    var old = document.querySelector('[data-once-slow-link]');
    if (old) old.remove();
    var note = document.createElement('div');
    note.setAttribute('data-once-slow-link', '');
    note.setAttribute('role', 'alert');
    note.style.cssText = 'position:fixed;top:12px;left:12px;right:12px;margin:0 auto;z-index:9999;max-width:520px;' +
      'border:2px solid #B5483B;background:#FBEDEA;color:#7A2C1F;border-radius:12px;padding:10px 14px;font-weight:700;' +
      'font-family:inherit;box-shadow:0 4px 18px rgba(0,0,0,.15)';
    note.appendChild(document.createTextNode('This page is taking longer than usual to load. '));
    var again = document.createElement('button');
    again.type = 'button';
    again.textContent = 'Try again';
    again.style.cssText = 'margin-left:6px;border:0;border-radius:8px;background:#B5483B;color:#fff;font-weight:800;padding:6px 12px;' +
      'font-family:inherit;font-size:inherit;cursor:pointer';
    again.addEventListener('click', function () {
      try { window.stop(); } catch (err) { /* older browsers */ }  // let go of the stuck load, then ask afresh
      note.remove();
      linkTimer = setTimeout(function () { linkNotice(href); }, WAIT_MS);
      location.assign(href);
    });
    note.appendChild(again);
    document.body.appendChild(note);
  }
  document.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest ? e.target.closest('a[href]') : null;
    if (!a || a.hasAttribute('download') || (a.target && a.target !== '_self') || a.getAttribute('href').charAt(0) === '#') return;
    var url;
    try { url = new URL(a.href, location.href); } catch (err) { return; }
    if (url.origin !== location.origin) return;                        // other sites, mailto:, tel:
    if (url.pathname === location.pathname && url.search === location.search && url.hash) return;  // a jump on this page
    if (/\.(csv|ics|pdf|png|jpe?g|zip)$/i.test(url.pathname)) return;  // downloads leave the page where it is
    var old = document.querySelector('[data-once-slow-link]');
    if (old) old.remove();
    clearTimeout(linkTimer);
    setTimeout(function () {  // after any other click handler has had its say
      if (!e.defaultPrevented) linkTimer = setTimeout(function () { linkNotice(url.href); }, WAIT_MS);
    }, 0);
  });

  window.addEventListener('pageshow', function () {
    document.querySelectorAll('form[data-sending]').forEach(unlock);
    document.querySelectorAll('[data-once-slow], [data-once-slow-link]').forEach(function (n) { n.remove(); });
    clearTimeout(linkTimer);
  });
})();
