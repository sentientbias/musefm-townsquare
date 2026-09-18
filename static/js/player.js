/* Muse FM — audio engine. One shared <audio>, sticky mini-player,
   up-next queue, speed, sleep timer, #t= deep links, clip saving. */
window.TSPlayer = (function () {
  var audio = new Audio();
  audio.preload = 'metadata';
  var state = {
    queue: [],            // [{slug, src, title, dur}]
    current: null,        // {slug, src, title, dur, el: audio}
    sleepTimer: null,
  };
  state.current = null;

  var mp = document.getElementById('miniplayer');
  var mpTitle = document.getElementById('mp-title');
  var mpTime = document.getElementById('mp-time');
  var mpPlay = document.getElementById('mp-play');
  var mpProg = document.querySelector('#mp-progress > div');

  // iOS Safari (and spec-compliant browsers) throw InvalidStateError if
  // currentTime is set while readyState is HAVE_NOTHING. The old code did
  // `audio.currentTime = 0` synchronously after `audio.src = ...`, which
  // aborted load() before play() ever ran — tapping Play did nothing.
  // Always seek after metadata is available instead.
  var loadSeq = 0;
  function seekSafe(t, seq) {
    if (audio.readyState >= 1) { try { audio.currentTime = t; } catch (e) {} return; }
    var once = function () {
      audio.removeEventListener('loadedmetadata', once);
      if (seq !== loadSeq) return; // superseded by a newer load()
      try { audio.currentTime = t; } catch (e2) {}
    };
    audio.addEventListener('loadedmetadata', once);
  }

  function fmt(s) {
    if (!isFinite(s)) return '0:00';
    s = Math.floor(s);
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }

  function show() { mp.classList.add('show'); mp.setAttribute('aria-hidden', 'false'); }

  function load(item, autoplay, startAt) {
    state.current = Object.assign({ el: audio }, item);
    var seq = ++loadSeq;
    audio.src = item.src;
    mpTitle.textContent = item.title;
    show();
    renderQueue();
    seekSafe(startAt || 0, seq);
    if (autoplay) {
      var p = audio.play();
      if (p) p.catch(function () { toast('Could not play — tap again'); });
    }
    updatePlayBtn();
  }

  function playItem(item, startAt) {
    // pull from queue if it matches, else just play
    load(item, true, startAt || 0);
  }

  function enqueue(item) {
    state.queue.push(item);
    if (!state.current) playItem(state.queue.shift());
    else { renderQueue(); toast('Added to up next'); }
    show();
  }

  function next() {
    if (state.queue.length) playItem(state.queue.shift());
    else { audio.pause(); updatePlayBtn(); }
  }

  function toggle() {
    if (!state.current) return;
    if (audio.paused) { var p = audio.play(); if (p) p.catch(function () { toast('Could not play — tap again'); }); }
    else audio.pause();
    updatePlayBtn();
  }

  function updatePlayBtn() {
    mpPlay.textContent = audio.paused ? '▶' : '⏸';
  }

  audio.addEventListener('play', updatePlayBtn);
  audio.addEventListener('pause', updatePlayBtn);
  audio.addEventListener('ended', next);
  audio.addEventListener('timeupdate', function () {
    var d = audio.duration || (state.current && state.current.dur) || 0;
    var c = audio.currentTime || 0;
    mpTime.textContent = fmt(c) + ' / ' + fmt(d);
    if (d) mpProg.style.width = (100 * c / d) + '%';
    // per-card progress
    if (state.current) {
      var bar = document.querySelector('[data-seek="' + state.current.slug + '"] > div');
      var el = document.querySelector('[data-elapsed="' + state.current.slug + '"]');
      if (bar && d) bar.style.width = (100 * c / d) + '%';
      if (el) el.textContent = fmt(c);
    }
  });
  audio.addEventListener('loadedmetadata', function () {
    var d = audio.duration;
    if (state.current && d) mpTime.textContent = '0:00 / ' + fmt(d);
  });

  // controls
  mpPlay.onclick = toggle;
  document.getElementById('mp-rew').onclick = function () { audio.currentTime = Math.max(0, audio.currentTime - 15); };
  document.getElementById('mp-fwd').onclick = function () { audio.currentTime += 30; };
  document.getElementById('mp-close').onclick = function () {
    audio.pause(); mp.classList.remove('show'); state.current = null;
  };
  document.getElementById('mp-queue').onclick = function () {
    document.getElementById('queue-panel').classList.toggle('open');
  };
  document.getElementById('mp-progress').onclick = function (e) {
    var r = this.getBoundingClientRect();
    var d = audio.duration; if (!d) return;
    audio.currentTime = d * ((e.clientX - r.left) / r.width);
  };
  document.getElementById('mp-speed').onchange = function () { audio.playbackRate = parseFloat(this.value); };
  document.getElementById('mp-sleep').onchange = function () {
    if (state.sleepTimer) { clearTimeout(state.sleepTimer); state.sleepTimer = null; }
    var m = parseInt(this.value, 10);
    if (m > 0) {
      state.sleepTimer = setTimeout(function () { audio.pause(); toast('Sleep timer — goodnight 🌙'); }, m * 60000);
      toast('Sleep in ' + m + ' min');
    }
  };
  document.getElementById('mp-clip').onclick = function () {
    if (!state.current) return;
    var t = Math.floor(audio.currentTime);
    var start = Math.max(0, t - 15), end = Math.min(Math.floor(audio.duration || 0), t + 15);
    var note = prompt('Name this clip (optional):', '');
    if (note === null) return;
    var handle = (document.cookie.match(/(?:^|;)\s*ts_handle=([^;]+)/) || [])[1];
    handle = handle ? decodeURIComponent(handle) : prompt('Your handle:', '') || 'anon';
    fetch('/api/episodes/' + state.current.slug + '/clips', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ handle: handle, start_sec: start, end_sec: end, note: note || '' })
    }).then(function (r) { return r.json(); }).then(function (j) {
      if (j.ok) { toast('Clip saved ✂'); if (j.share_url) copyText(j.share_url); }
      else toast('Clip failed: ' + (j.error || '?'));
    }).catch(function () { toast('Clip failed — network'); });
  };

  function renderQueue() {
    var q = document.getElementById('queue-panel');
    var html = '<div class="hint" style="margin:4px 0">UP NEXT</div>';
    if (!state.queue.length) html += '<div class="hint">Queue is empty.</div>';
    state.queue.forEach(function (it, i) {
      html += '<div class="q-item"><span>' + escapeHtml(it.title) + '</span>' +
        '<button data-qrm="' + i + '" title="remove">✕</button></div>';
    });
    q.innerHTML = html;
    q.querySelectorAll('[data-qrm]').forEach(function (b) {
      b.onclick = function () { state.queue.splice(parseInt(b.getAttribute('data-qrm'), 10), 1); renderQueue(); };
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // wire episode cards
  function itemFrom(el) {
    return {
      slug: el.getAttribute('data-play') || el.getAttribute('data-queue'),
      src: el.getAttribute('data-src'),
      title: el.getAttribute('data-title'),
      dur: parseInt(el.getAttribute('data-dur') || '0', 10),
    };
  }
  document.addEventListener('click', function (e) {
    var p = e.target.closest('[data-play]');
    if (p) {
      var it = itemFrom(p);
      var start = parseInt(p.getAttribute('data-start') || '0', 10);
      playItem(it, start);
      return;
    }
    var q = e.target.closest('[data-queue]');
    if (q) enqueue(itemFrom(q));
    var s = e.target.closest('[data-seek]');
    if (s && state.current && s.getAttribute('data-seek') === state.current.slug) {
      var bar = s.getBoundingClientRect();
      var d = audio.duration || state.current.dur;
      audio.currentTime = d * ((e.clientX - bar.left) / bar.width);
    }
  });

  // #t= deep links: /episodes#slug?t=90
  function deepLink() {
    var h = location.hash; // #slug?t=90
    if (!h || h.length < 2) return;
    var m = h.slice(1).match(/^([a-z0-9-]+)(\?t=(\d+))?$/);
    if (!m) return;
    var btn = document.querySelector('[data-play="' + m[1] + '"]');
    if (btn) {
      var it = itemFrom(btn);
      playItem(it, m[3] ? parseInt(m[3], 10) : 0);
      var card = document.getElementById(m[1]);
      if (card) setTimeout(function () { card.scrollIntoView({ behavior: 'smooth', block: 'center' }); }, 300);
    }
  }
  window.addEventListener('hashchange', deepLink);
  document.addEventListener('DOMContentLoaded', deepLink);

  return { enqueue: enqueue, playItem: playItem,
           get current() { return state.current; } };
})();
