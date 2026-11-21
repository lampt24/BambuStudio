/* Applies the saved theme before first paint (loaded synchronously in <head>). */
(function () {
  try {
    var t = localStorage.getItem('printpilot_theme');
    if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t);
  } catch (e) { /* storage blocked: follow the OS */ }
})();
