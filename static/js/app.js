/* Muse FM — shared UI helpers */
function toggleTheme() {
  var el = document.documentElement;
  var next = el.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  el.setAttribute('data-theme', next);
  try { localStorage.setItem('ts-theme', next); } catch (e) {}
}

// ---- left sidebar drawer (mobile) ----
function toggleSidebar(force) {
  var open = typeof force === 'boolean' ? force : !document.body.classList.contains('sidebar-open');
  document.body.classList.toggle('sidebar-open', open);
  var b = document.getElementById('sb-toggle');
  if (b) b.setAttribute('aria-expanded', open ? 'true' : 'false');
}
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape' && document.body.classList.contains('sidebar-open')) toggleSidebar(false);
});
// tapping a sidebar link closes the drawer
document.addEventListener('click', function (e) {
  var a = e.target && e.target.closest ? e.target.closest('.sidebar a') : null;
  if (a && document.body.classList.contains('sidebar-open')) toggleSidebar(false);
});

function toast(msg) {
  var t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._h);
  t._h = setTimeout(function(){ t.classList.remove('show'); }, 2200);
}

// show remembered handle in nav
(function () {
  var m = document.cookie.match(/(?:^|;)\s*ts_handle=([^;]+)/);
  var chip = document.getElementById('handle-chip');
  if (m && chip) chip.textContent = 'u/' + decodeURIComponent(m[1]);
})();

// remember handle from any form
document.addEventListener('submit', function (e) {
  var h = e.target.querySelector && e.target.querySelector('input[name="handle"]');
  if (h && h.value) {
    try { localStorage.setItem('ts-handle', h.value); } catch (e2) {}
  }
});
// prefill handle fields from memory
document.addEventListener('DOMContentLoaded', function () {
  var saved = null;
  try { saved = localStorage.getItem('ts-handle'); } catch (e) {}
  if (saved) document.querySelectorAll('input[name="handle"]').forEach(function (i) {
    if (!i.value) i.value = saved;
  });
});

// ---- share sheet ----
var shareCtx = null;
function openShare(slug, title) {
  shareCtx = { slug: slug, title: title };
  var t = 0;
  if (window.TSPlayer && TSPlayer.current && TSPlayer.current.slug === slug) {
    t = Math.floor(TSPlayer.current.el.currentTime || 0);
  }
  var url = location.origin + '/episodes#' + slug + (t > 3 ? '?t=' + t : '');
  var opts = document.getElementById('share-opts');
  opts.innerHTML = '';
  var items = [
    ['⧉', 'Copy link' + (t > 3 ? ' (at ' + fmtT(t) + ')' : ''), function () {
      copyText(url); toast('Link copied'); closeShare();
    }],
    ['𝕏', 'Share on X', function () {
      // NOTE: page URL goes inside `text` — X's composer pulls text
      // reliably but was dropping the separate `url` param.
      window.open('https://x.com/intent/tweet?text=' +
        encodeURIComponent('🎙️ ' + title + ' — Muse FM ' + url), '_blank');
      closeShare();
    }]
  ];
  if (navigator.share) items.push(['↗', 'More…', function () {
    navigator.share({ title: title, text: '🎙️ ' + title + ' — Muse FM', url: url });
    closeShare();
  }]);
  items.forEach(function (it) {
    var b = document.createElement('button');
    b.className = 'share-opt';
    b.innerHTML = '<span style="font-size:18px">' + it[0] + '</span><span>' + it[1] + '</span>';
    b.onclick = it[2];
    opts.appendChild(b);
  });
  document.getElementById('share-sheet').classList.add('open');
}
function closeShare() {
  document.getElementById('share-sheet').classList.remove('open');
}
document.getElementById('share-sheet').addEventListener('click', function (e) {
  if (e.target === this) closeShare();
});
function copyText(s) {
  if (navigator.clipboard) navigator.clipboard.writeText(s).catch(function(){ fallbackCopy(s); });
  else fallbackCopy(s);
}
function fallbackCopy(s) {
  var ta = document.createElement('textarea');
  ta.value = s; document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); } catch (e) {}
  document.body.removeChild(ta);
}
function fmtT(s) {
  s = Math.floor(s); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}
document.addEventListener('click', function (e) {
  var b = e.target.closest('[data-share]');
  if (b) openShare(b.getAttribute('data-share'), b.getAttribute('data-title'));
});
