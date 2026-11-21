// Shrinks a chosen photo on the phone/computer before upload (like Mollie's bakery admin), so big phone photos upload fast.
(function () {
  document.querySelectorAll('input[type=file][data-photo]').forEach(function (input) {
    input.addEventListener('change', function () {
      var file = input.files && input.files[0];
      var out = document.getElementById(input.dataset.photo);
      var preview = document.getElementById(input.dataset.photo + '_preview');
      if (!file) return;
      var img = new Image();
      img.onload = function () {
        // 1400px wide is plenty for the shop; then lower the quality (and size) until it's under ~900 KB.
        var w = img.naturalWidth, h = img.naturalHeight, max = 1400, q = 0.85, url = '';
        for (var tries = 0; tries < 6; tries++) {
          var k = Math.min(1, max / Math.max(w, h));
          var c = document.createElement('canvas'); c.width = Math.round(w * k); c.height = Math.round(h * k);
          var ctx = c.getContext('2d'); ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, c.width, c.height);  // see-through PNGs get white, not black
          ctx.drawImage(img, 0, 0, c.width, c.height);
          url = c.toDataURL('image/jpeg', q);
          if (url.length < 900000) break;
          if (q > 0.7) { q -= 0.07; } else { max = Math.round(max * 0.8); }
        }
        out.value = url;
        if (preview) { preview.style.backgroundImage = 'url(' + out.value + ')'; preview.textContent = ''; }
        URL.revokeObjectURL(img.src);
      };
      img.src = URL.createObjectURL(file);
    });
  });
})();
