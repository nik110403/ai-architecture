var dlg = document.getElementById('dlg');
var btn = document.getElementById('menu-btn');
var card = document.getElementById('card');
btn.addEventListener('click', function () { dlg.hidden = false; });
document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && e.isTrusted) { dlg.hidden = true; } });
card.addEventListener('keydown', function (e) { if (e.key === 'Enter') { card.classList.toggle('pinned'); } });
