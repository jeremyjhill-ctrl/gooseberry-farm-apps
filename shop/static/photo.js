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
        var max = 1600, w = img.naturalWidth, h = img.naturalHeight, k = Math.min(1, max / Math.max(w, h));
        var c = document.createElement('canvas'); c.width = Math.round(w * k); c.height = Math.round(h * k);
        c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
        out.value = c.toDataURL('image/jpeg', 0.85);
        if (preview) { preview.style.backgroundImage = 'url(' + out.value + ')'; preview.textContent = ''; }
        URL.revokeObjectURL(img.src);
      };
      img.src = URL.createObjectURL(file);
    });
  });
})();
