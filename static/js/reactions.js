/* Muse FM — Facebook-style reaction picker.
   Long-press (touch) or hover (mouse) on the Like button reveals the six;
   click toggles Like. Count button toggles the full breakdown. All updates
   go through POST /fb_react as JSON and re-render in place; the underlying
   form still works as a no-JS fallback. */
(function () {
  'use strict';
  var EMOJI = { like: '👍', love: '❤️', haha: '😂', wow: '😮', sad: '😢', angry: '😡' };
  var ORDER = ['like', 'love', 'haha', 'wow', 'sad', 'angry'];
  var LABEL = { like: 'Like', love: 'Love', haha: 'Haha', wow: 'Wow', sad: 'Sad', angry: 'Angry' };

  function closeAll(except) {
    document.querySelectorAll('.rxn .rxn-picker:not([hidden]), .rxn .rxn-breakdown:not([hidden])')
      .forEach(function (el) {
        if (el !== except) el.hidden = true;
      });
  }

  function renderWidget(w, d) {
    w.setAttribute('data-mine', d.mine || '');
    var likeBtn = w.querySelector('.rxn-like');
    likeBtn.querySelector('.rxn-like-emoji').textContent = EMOJI[d.mine] || '👍';
    likeBtn.querySelector('.rxn-like-label').textContent = d.mine ? LABEL[d.mine] : 'Like';
    likeBtn.classList.toggle('active', !!d.mine);
    // counts
    var countBtn = w.querySelector('.rxn-count');
    var topHtml = '';
    (d.top || []).forEach(function (t) {
      topHtml += '<span class="rxn-top-emoji">' + t[1] + '</span>';
    });
    countBtn.querySelector('.rxn-top').innerHTML = topHtml;
    countBtn.querySelector('.rxn-total').textContent = d.total;
    countBtn.classList.toggle('rxn-count-empty', !d.total);
    var title = ORDER.filter(function (r) { return d.counts[r]; })
      .map(function (r) { return EMOJI[r] + ' ' + d.counts[r] + ' ' + r; }).join(' · ');
    countBtn.title = title;
    countBtn.setAttribute('aria-label', d.total + ' reactions — see breakdown');
    // breakdown
    var bd = w.querySelector('.rxn-breakdown');
    var bdHtml = '';
    ORDER.forEach(function (r) {
      if (d.counts[r]) {
        bdHtml += '<div class="rxn-brow" data-reaction="' + r + '"><span>' +
          EMOJI[r] + ' ' + LABEL[r] + '</span><b>' + d.counts[r] + '</b></div>';
      }
    });
    bd.innerHTML = bdHtml;
  }

  function sendReaction(w, reaction) {
    var body = {
      target_type: w.getAttribute('data-target-type'),
      target_id: parseInt(w.getAttribute('data-target-id'), 10),
      reaction: reaction,
      next: w.getAttribute('data-next') || '/'
    };
    fetch('/fb_react', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json().then(function (d) { return {status: r.status, body: d}; }); }).then(function (res) {
      var d = res.body;
      if (!d.ok) {
        if (d.signin_url) { window.location.href = d.signin_url; return; }
        toast(d.error || 'reaction failed');
        return;
      }
      closeAll();
      renderWidget(w, d);
    }).catch(function () { toast('network hiccup — try again'); });
  }

  function openPicker(w) {
    closeAll();
    w.querySelector('.rxn-picker').hidden = false;
  }

  function wireWidget(w) {
    if (w._rxnWired) return;
    w._rxnWired = true;
    var likeBtn = w.querySelector('.rxn-like');
    var form = w.querySelector('.rxn-form');
    var picker = w.querySelector('.rxn-picker');
    var countBtn = w.querySelector('.rxn-count');
    var pressTimer = null, hoverTimer = null, longPressed = false;

    // click Like -> toggle like via fetch (progressive enhancement over the form).
    // The server toggles: sending 'like' while mine=='like' removes it.
    likeBtn.addEventListener('click', function (e) {
      if (longPressed) { longPressed = false; e.preventDefault(); return; }
      e.preventDefault();
      sendReaction(w, 'like');
    });

    // touch: long-press opens the picker
    likeBtn.addEventListener('pointerdown', function (e) {
      if (e.pointerType === 'mouse') return;
      longPressed = false;
      clearTimeout(pressTimer);
      pressTimer = setTimeout(function () { longPressed = true; openPicker(w); }, 450);
    });
    ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (ev) {
      likeBtn.addEventListener(ev, function () { clearTimeout(pressTimer); });
    });
    // mouse: hover opens the picker (touch uses long-press above)
    likeBtn.addEventListener('mouseenter', function (e) {
      if (e.pointerType && e.pointerType !== 'mouse') return;
      clearTimeout(hoverTimer);
      hoverTimer = setTimeout(function () { openPicker(w); }, 300);
    });
    w.addEventListener('mouseleave', function () {
      clearTimeout(hoverTimer);
      picker.hidden = true;
    });

    picker.querySelectorAll('.rxn-opt').forEach(function (opt) {
      opt.addEventListener('click', function () {
        sendReaction(w, opt.getAttribute('data-reaction'));
      });
    });

    countBtn.addEventListener('click', function () {
      var bd = w.querySelector('.rxn-breakdown');
      var willOpen = bd.hidden;
      closeAll();
      bd.hidden = !willOpen;
    });
  }
  document.querySelectorAll('.rxn').forEach(wireWidget);
  // Exposed so infinite-scroll feeds can wire reaction widgets on
  // dynamically inserted cards.
  window.wireRxnWidget = wireWidget;

  document.addEventListener('click', function (e) {
    if (!e.target.closest('.rxn')) closeAll();
  });
  document.addEventListener('scroll', function () { closeAll(); }, { passive: true });
})();
