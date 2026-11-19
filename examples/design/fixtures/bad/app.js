var dlg = document.getElementById('dlg');
var btn = document.getElementById('menu-btn');
var card = document.getElementById('card');
btn.addEventListener('click', function () { dlg.hidden = false; });
document.addEventListener('keydown', function (e) { /* escape ignored */ });
card.addEventListener('keydown', function (e) { if (e.key === 'Enter') { card.classList.toggle('pinned'); } });
