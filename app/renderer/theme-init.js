(function () {
  try {
    var t = localStorage.getItem('ui.theme');
    if (t === 'light' || t === 'dark') {
      document.documentElement.setAttribute('data-theme', t);
    }
  } catch (_) { /* private mode / storage disabled — dark default is fine */ }
})();

window.addEventListener('load', function () {
  setTimeout(function () {
    if (window.__slBootStarted) return;
    var stage = document.getElementById('boot-stage');
    var retry = document.getElementById('boot-retry');
    if (stage) stage.textContent = '界面脚本未能启动，请重启应用。';
    if (retry) retry.classList.remove('hidden');
  }, 8000);
});
