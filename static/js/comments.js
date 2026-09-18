/* Shared comment-section behavior: AJAX vote/flag/edit, collapse toggles.
 * Progressive enhancement — every form also works as a plain POST
 * (redirects back) when JS is off or the visitor is signed out.
 */
(function () {
  'use strict';

  function csrfToken() {
    var m = document.querySelector('meta[name="csrf-token"]');
    if (m && m.content) return m.content;
    var inp = document.querySelector('input[name="csrf_token"]');
    return inp ? inp.value : '';
  }

  function postJSON(url, payload) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(payload)
    }).then(function (r) {
      return r.json().then(function (d) { return { status: r.status, body: d }; });
    });
  }

  function signedIn() { return !!csrfToken(); }

  /* ---- voting ---- */
  function refreshVoteUI(scope, myVote, score) {
    var up = scope.querySelector('.cvote-btn.up');
    var down = scope.querySelector('.cvote-btn.down');
    var scoreEl = scope.querySelector('.cvote-score');
    if (up) {
      up.classList.toggle('is-active', myVote === 1);
      up.setAttribute('aria-pressed', myVote === 1 ? 'true' : 'false');
    }
    if (down) {
      down.classList.toggle('is-active', myVote === -1);
      down.setAttribute('aria-pressed', myVote === -1 ? 'true' : 'false');
    }
    if (scoreEl && score != null) {
      scoreEl.textContent = score;
      scoreEl.setAttribute('aria-label', 'Score ' + score);
      scoreEl.classList.toggle('pos', score > 0);
      scoreEl.classList.toggle('neg', score < 0);
    }
  }

  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (!form.classList || !form.classList.contains('cvote-form')) return;
    if (!signedIn()) return;  // let the plain POST redirect to /login
    ev.preventDefault();
    var scope = form.closest('.cvote');
    var btn = form.querySelector('button[type="submit"]');
    if (btn) btn.disabled = true;
    postJSON('/vote', {
      csrf_token: csrfToken(),
      target_type: form.querySelector('[name="target_type"]').value,
      target_id: parseInt(form.querySelector('[name="target_id"]').value, 10),
      value: parseInt(form.querySelector('[name="value"]').value, 10)
    }).then(function (res) {
      if (btn) btn.disabled = false;
      if (!res.body.ok) return;
      refreshVoteUI(scope, res.body.my_vote, res.body.score);
    }).catch(function () { if (btn) btn.disabled = false; });
  });

  /* ---- flagging ---- */
  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (!form.classList || !form.classList.contains('cflag-form')) return;
    if (!signedIn()) return;  // plain POST -> login nudge
    ev.preventDefault();
    var btn = form.querySelector('.cflag-btn');
    if (btn) btn.disabled = true;
    postJSON('/flag', {
      csrf_token: csrfToken(),
      target_type: form.querySelector('[name="target_type"]').value,
      target_id: parseInt(form.querySelector('[name="target_id"]').value, 10),
      reason: 'other'
    }).then(function (res) {
      if (btn) btn.disabled = false;
      if (!res.body.ok) return;
      if (btn) {
        btn.classList.add('is-flagged');
        btn.setAttribute('aria-pressed', 'true');
        btn.setAttribute('aria-label', 'Flagged for review');
        btn.title = 'Flagged — in the mod queue';
        var svg = btn.querySelector('svg');
        if (svg) svg.setAttribute('fill', 'currentColor');
      }
    }).catch(function () { if (btn) btn.disabled = false; });
  });

  /* ---- collapse / reply toggles ---- */
  document.addEventListener('click', function (ev) {
    var t = ev.target.closest('[data-ctoggle]');
    if (!t) return;
    var card = t.closest('.comment');
    if (!card) return;
    var action = t.getAttribute('data-ctoggle');
    if (action === 'collapse') {
      var collapsed = card.classList.toggle('is-collapsed');
      t.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      t.textContent = collapsed ? 'Expand' : 'Collapse';
    } else if (action === 'reply') {
      var rf = card.querySelector(':scope > .c-reply-form');
      if (rf) {
        rf.hidden = !rf.hidden;
        t.setAttribute('aria-expanded', rf.hidden ? 'false' : 'true');
        var ta = rf.querySelector('textarea');
        if (ta && !rf.hidden) ta.focus();
      }
    } else if (action === 'edit') {
      var ef = card.querySelector(':scope > .c-edit-form');
      if (ef) {
        ef.hidden = !ef.hidden;
        var eta = ef.querySelector('textarea');
        if (eta && !ef.hidden) { eta.focus(); eta.setSelectionRange(eta.value.length, eta.value.length); }
      }
    }
  });

  /* ---- inline edit submit ---- */
  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (!form.classList || !form.classList.contains('cedit-form')) return;
    if (!signedIn()) return;
    ev.preventDefault();
    var card = form.closest('.comment');
    var ta = form.querySelector('textarea[name="body"]');
    var body = (ta.value || '').trim();
    if (!body) return;
    var btn = form.querySelector('button[type="submit"]');
    if (btn) btn.disabled = true;
    postJSON('/comment/edit', {
      csrf_token: csrfToken(),
      target_type: form.querySelector('[name="target_type"]').value,
      target_id: parseInt(form.querySelector('[name="target_id"]').value, 10),
      body: body
    }).then(function (res) {
      if (btn) btn.disabled = false;
      if (!res.body.ok || !card) return;
      var bodyEl = card.querySelector(':scope > .comment-body');
      if (bodyEl) bodyEl.textContent = body;
      var head = card.querySelector(':scope > .comment-head');
      if (head && !head.querySelector('.edited')) {
        var s = document.createElement('span');
        s.className = 'edited';
        s.textContent = '(edited)';
        head.appendChild(s);
      }
      form.closest('.c-edit-form').hidden = true;
    }).catch(function () { if (btn) btn.disabled = false; });
  });

  /* ---- relative-time hydration ----
   * Server renders reltime text; this keeps it fresh and converts any
   * data-ts timestamps the server left as ISO fallbacks. */
  function hydrateTimes(root) {
    (root || document).querySelectorAll('time[data-ts]').forEach(function (el) {
      if (el.dataset.hydrated) return;
      el.dataset.hydrated = '1';
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { hydrateTimes(); });
  } else { hydrateTimes(); }

  window.MuseFMComments = {
    csrfToken: csrfToken,
    refreshVoteUI: refreshVoteUI,
    hydrateTimes: hydrateTimes
  };
})();
