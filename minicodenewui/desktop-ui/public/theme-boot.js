(function () {
  var pref;
  try {
    pref = localStorage.getItem('minicode.theme');
  } catch {}
  var theme = pref === 'light' || pref === 'dark'
    ? pref
    : matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', theme);
})();
