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

  window.addEventListener('pageshow', function () {
    document.querySelectorAll('form[data-sending]').forEach(unlock);
    document.querySelectorAll('[data-once-slow]').forEach(function (n) { n.remove(); });
  });
})();
