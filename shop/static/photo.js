// Product photo panel in the shop admin.
// - A chosen photo is shrunk in the browser before upload (big phone photos upload fast, and stay under the size limit).
// - Fill the box: drag to choose what shows, zoom slider, Center. Show the whole photo: nothing cut off (book covers).
// - Rotate turns the photo a quarter turn (it's saved as a new, upright photo).
// The photo itself is never cropped: the position and zoom are saved separately, so they can be changed any time.
(function () {
  var input = document.querySelector('input[type=file][data-photo]');
  if (!input) return;
  var form = input.form;
  var out = document.getElementById(input.dataset.photo);
  var box = document.getElementById('photo_box');
  var tools = document.getElementById('photo_tools');
  var f = function (name) { return form.querySelector('[name=' + name + ']'); };

  function img() { return box.querySelector('img'); }
  function fit() { var r = form.querySelector('input[name=photo_fit]:checked'); return r ? r.value : 'fill'; }

  function apply() {
    var i = img(), x = f('photo_x').value, y = f('photo_y').value, z = fit() === 'fill' ? f('photo_zoom').value : 1;
    box.classList.toggle('whole', fit() === 'whole');
    form.querySelectorAll('[data-fill-only]').forEach(function (e) { e.hidden = fit() !== 'fill'; });
    if (!i) return;
    i.style.objectFit = fit() === 'whole' ? 'contain' : 'cover';
    i.style.objectPosition = x + '% ' + y + '%';
    i.style.transformOrigin = x + '% ' + y + '%';
    i.style.transform = 'scale(' + z + ')';
  }

  // Draw the picture onto a canvas (turned `quarter` quarter-turns), shrinking until it's under ~900 KB.
  function shrink(picture, quarter) {
    var w = picture.naturalWidth, h = picture.naturalHeight, max = 1400, q = 0.85, url = '';
    var turned = quarter % 2 === 1;
    for (var tries = 0; tries < 6; tries++) {
      var k = Math.min(1, max / Math.max(w, h));
      var dw = Math.round(w * k), dh = Math.round(h * k);
      var c = document.createElement('canvas');
      c.width = turned ? dh : dw; c.height = turned ? dw : dh;
      var ctx = c.getContext('2d');
      ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, c.width, c.height);  // see-through PNGs get white, not black
      ctx.translate(c.width / 2, c.height / 2);
      ctx.rotate(quarter * Math.PI / 2);
      ctx.drawImage(picture, -dw / 2, -dh / 2, dw, dh);
      url = c.toDataURL('image/jpeg', q);
      if (url.length < 900000) break;
      if (q > 0.7) { q -= 0.07; } else { max = Math.round(max * 0.8); }
    }
    return url;
  }

  function show(url, resetView) {
    out.value = url;
    var i = img();
    if (!i) {
      box.textContent = '';
      i = document.createElement('img'); i.alt = ''; i.draggable = false;
      box.appendChild(i);
    }
    i.src = url;
    if (resetView) { f('photo_x').value = 50; f('photo_y').value = 50; f('photo_zoom').value = 1; }
    tools.hidden = false;
    apply();
  }

  input.addEventListener('change', function () {
    var file = input.files && input.files[0];
    if (!file) return;
    var p = new Image();
    p.onload = function () { show(shrink(p, 0), true); URL.revokeObjectURL(p.src); };
    p.onerror = function () { alert("That photo couldn't be opened. Try a JPG or PNG."); };
    p.src = URL.createObjectURL(file);
  });

  document.getElementById('photo_rotate').addEventListener('click', function () {
    var i = img();
    if (!i) return;
    var p = new Image();
    p.onload = function () { show(shrink(p, 1), true); };
    p.src = i.src;  // the saved photo is on this same site, so the browser lets us redraw it
  });

  document.getElementById('photo_center').addEventListener('click', function () {
    f('photo_x').value = 50; f('photo_y').value = 50; f('photo_zoom').value = 1; apply();
  });

  form.querySelectorAll('input[name=photo_fit], input[name=photo_zoom]').forEach(function (e) {
    e.addEventListener('input', apply); e.addEventListener('change', apply);
  });

  // Drag to move the photo around inside the box (fill mode). Works with a mouse or a finger.
  var drag = null;
  box.addEventListener('pointerdown', function (e) {
    if (fit() !== 'fill' || !img()) return;
    drag = { x: e.clientX, y: e.clientY, px: +f('photo_x').value, py: +f('photo_y').value };
    box.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  box.addEventListener('pointermove', function (e) {
    if (!drag) return;
    var r = box.getBoundingClientRect(), z = +f('photo_zoom').value || 1;
    var nx = drag.px - (e.clientX - drag.x) / r.width * 100 / z;
    var ny = drag.py - (e.clientY - drag.y) / r.height * 100 / z;
    f('photo_x').value = Math.round(Math.min(100, Math.max(0, nx)) * 10) / 10;
    f('photo_y').value = Math.round(Math.min(100, Math.max(0, ny)) * 10) / 10;
    apply();
  });
  ['pointerup', 'pointercancel'].forEach(function (t) { box.addEventListener(t, function () { drag = null; }); });

  apply();
})();
