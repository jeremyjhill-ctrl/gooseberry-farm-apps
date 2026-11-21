// One save, once: a form's button locks the moment it's clicked, so a double-click (or an impatient
// second tap on a slow phone) never sends the same form twice. Coming back with the Back button unlocks it.
(function () {
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (e.defaultPrevented || form.hasAttribute('data-no-lock')) return;
    if ((form.getAttribute('method') || 'get').toLowerCase() !== 'post') return;  // searches just load a page
    if (form.dataset.sending === '1') { e.preventDefault(); return; }
    form.dataset.sending = '1';
    var btn = e.submitter || form.querySelector('button:not([type=button]), input[type=submit]');
    setTimeout(function () {  // after the browser has read the button's name/value into the form
      form.querySelectorAll('button:not([type=button]), input[type=submit]').forEach(function (b) {
        b.disabled = true;
        if (b === btn && b.tagName === 'BUTTON') { b.dataset.label = b.innerHTML; b.textContent = 'Working…'; }
      });
    }, 0);
  });
  window.addEventListener('pageshow', function () {
    document.querySelectorAll('form[data-sending]').forEach(function (form) {
      delete form.dataset.sending;
      form.querySelectorAll('button, input[type=submit]').forEach(function (b) {
        b.disabled = false;
        if (b.dataset.label) { b.innerHTML = b.dataset.label; delete b.dataset.label; }
      });
    });
  });
})();
