(function () {
  try {
    var pref = localStorage.getItem('minicode.theme');
    var theme = pref;
    if (!theme || theme === 'system') {
      theme = matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    }
    if (theme !== 'light' && theme !== 'dark') theme = 'dark';
    document.documentElement.setAttribute('data-theme', theme);
  } catch (e) {
    document.documentElement.setAttribute('data-theme', 'dark');
  }
})();
