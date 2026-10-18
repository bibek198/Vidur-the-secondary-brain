// Applies the saved appearance (light / dark) before the first paint, so the page never flashes the wrong theme.
// "system" (or nothing saved) leaves data-theme unset and the CSS follows the OS setting. app.js owns the switch.
(function () {
  var t = null;
  try { t = localStorage.getItem("db.theme"); } catch (e) { /* storage may be unavailable */ }
  if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
})();
