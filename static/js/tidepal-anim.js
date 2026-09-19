/* Tidepal client-side animation engine (Track B, 2026-09-19).
 *
 * Layers procedural, client-side life on top of the server-rendered SVG
 * from pets.pet_svg(). The server SVG already carries SMIL idle motion;
 * this engine ADDS: squash-and-stretch on pat, cursor eye-tracking,
 * feed/play/rest reactions, celebration spins, deep sleep breathing,
 * mood-driven energy, and per-trait motion styles.
 *
 * Contract hooks (on svg root): data-tidepal, data-trait, data-mood,
 * data-species, data-stage. Inner groups: .tp-body, .tp-eyes.
 * Everything degrades gracefully: no hooks -> no-op. transform/opacity
 * only, one shared rAF loop, IntersectionObserver gating, honors
 * prefers-reduced-motion.
 */
(function () {
  "use strict";

  var REDUCED = (typeof window.matchMedia === "function") &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* Personality idle params: {bob amp, bob period, breath amp, breath
   * period, sway amp, sway period, wiggle deg, wiggle period} */
  var TRAITS = {
    playful:     { bob: 3.0, bobT: 2.2, breath: 0.030, breathT: 2.0, sway: 0.6, swayT: 3.0, wig: 1.2, wigT: 2.2 },
    calm:        { bob: 1.1, bobT: 3.6, breath: 0.020, breathT: 3.4, sway: 2.2, swayT: 4.6, wig: 0.0, wigT: 4.0 },
    mischievous: { bob: 1.9, bobT: 2.8, breath: 0.025, breathT: 2.6, sway: 1.2, swayT: 3.4, wig: 3.4, wigT: 2.8 },
    gentle:      { bob: 1.0, bobT: 4.2, breath: 0.025, breathT: 4.0, sway: 1.4, swayT: 5.0, wig: 0.0, wigT: 5.0 }
  };

  /* Mood energy multipliers. sleepy gets special deep-breath handling. */
  var MOOD_ENERGY = {
    happy: 1.25, overjoyed: 1.5, content: 1.0,
    peckish: 0.65, restless: 0.85, sleepy: 0.3
  };

  var pets = [];          // {svg, wrap, eyes, trait, mood, stage, energy, visible, rectT, rect, spring, fx, eyeCur, eyeTgt, hasSmil, isEgg}
  var visible = new Set();
  var rafId = 0;
  var mouse = { x: -1e9, y: -1e9 };
  var io = null;

  function traitParams(t) {
    return TRAITS[t] || TRAITS.calm;
  }

  /* Wrap a target element in a <g> so CSS transforms never clobber the
   * element's own transform attribute (e.g. .tp-body's centering). */
  function ensureWrap(svg, selector) {
    var el = svg.querySelector(selector);
    if (el && el.parentNode.classList &&
        el.parentNode.classList.contains("tp-js")) {
      return el.parentNode;   // already wrapped
    }
    var wrap = document.createElementNS("http://www.w3.org/2000/svg", "g");
    wrap.setAttribute("class", "tp-js");
    if (el) {
      el.parentNode.insertBefore(wrap, el);
      wrap.appendChild(el);
    } else {
      /* last-resort fallback: wrap every child so motion still works */
      var kids = [];
      for (var i = 0; i < svg.childNodes.length; i++) {
        if (svg.childNodes[i].nodeType === 1) kids.push(svg.childNodes[i]);
      }
      if (!kids.length) return null;
      svg.insertBefore(wrap, kids[0]);
      kids.forEach(function (k) { wrap.appendChild(k); });
    }
    return wrap;
  }

  function prepWrap(wrap) {
    wrap.style.transformBox = "fill-box";
    wrap.style.transformOrigin = "50% 88%";  // squash sits on the ground
    wrap.style.willChange = "transform";
  }

  function register(svg) {
    if (!svg || svg._tpInit || svg.tagName.toLowerCase() !== "svg") return null;
    svg._tpInit = true;
    var ds = svg.dataset || {};
    var trait = (ds.trait && TRAITS[ds.trait]) ? ds.trait : "calm";
    var mood = ds.mood || "content";
    var stage = parseInt(ds.stage || "0", 10) || 0;
    var wrap = ensureWrap(svg, ".tp-body");
    if (wrap) prepWrap(wrap);
    var eyes = svg.querySelector(".tp-eyes");
    if (eyes) {
      eyes.style.transformBox = "fill-box";
      eyes.style.transformOrigin = "50% 50%";
      eyes.style.willChange = "transform";
    }
    var p = {
      svg: svg, wrap: wrap, eyes: eyes, trait: trait, mood: mood,
      stage: stage, isEgg: stage === 0,
      energy: (mood in MOOD_ENERGY) ? MOOD_ENERGY[mood] : 1.0,
      visible: false, rect: null, rectT: 0,
      spring: { sy: 1, vy: 0 },       // squash & stretch spring
      fx: null,                        // active one-shot effect {kind,t0,dur}
      eyeCur: { x: 0, y: 0 }, eyeTgt: { x: 0, y: 0 },
      hasSmil: svg.querySelector("animateTransform") !== null,
      wigT: 4 + Math.random() * 6      // egg wiggle timer
    };
    pets.push(p);
    if (io) io.observe(svg);
    return p;
  }

  function findPet(el) {
    var svg = el && el.tagName && el.tagName.toLowerCase() === "svg"
      ? el : el && el.querySelector ? el.querySelector("svg[data-tidepal]") : null;
    if (!svg) return null;
    for (var i = 0; i < pets.length; i++) if (pets[i].svg === svg) return pets[i];
    return register(svg);
  }

  /* ---------- one shared rAF loop ---------- */
  function tick(nowMs) {
    var t = nowMs / 1000;
    var dt = Math.min(0.05, (tick._last ? (nowMs - tick._last) / 1000 : 0.016));
    tick._last = nowMs;
    visible.forEach(function (p) {
      try { update(p, t, dt); } catch (e) { /* never let one pet kill the loop */ }
    });
    if (visible.size > 0) {
      rafId = requestAnimationFrame(tick);
    } else {
      rafId = 0; tick._last = 0;
    }
  }
  function poke() {
    if (!rafId && visible.size > 0 && !REDUCED) rafId = requestAnimationFrame(tick);
  }

  function update(p, t, dt) {
    var prm = traitParams(p.trait);
    var idleScale = p.hasSmil ? 0.5 : 1.0;  // SMIL already moves; layer lightly
    var e = p.energy * idleScale;

    /* spring: squash & stretch (pat) */
    var s = p.spring;
    var k = 140, c = 9;
    var acc = -k * (s.sy - 1) - c * s.vy;
    s.vy += acc * dt;
    s.sy += s.vy * dt;
    if (Math.abs(s.sy - 1) < 0.001 && Math.abs(s.vy) < 0.01) { s.sy = 1; s.vy = 0; }
    var sx = 1 + (1 - s.sy) * 0.75;  // volume-preserving stretch

    var ty = 0, tx = 0, rot = 0, sc = 1;
    if (p.mood === "sleepy") {
      /* deep, slow, cozy breathing — never sad, just tucked in */
      var br = Math.sin(t * (Math.PI * 2 / 6.0));
      sc = 1 + br * 0.055;
      ty = 1.6 + Math.sin(t * (Math.PI * 2 / 6.0) + 1) * 0.5;
      sx *= 1 + br * 0.02;
    } else {
      ty = Math.sin(t * (Math.PI * 2 / prm.bobT)) * prm.bob * e;
      tx = Math.sin(t * (Math.PI * 2 / prm.swayT) + 0.7) * prm.sway * e;
      rot = Math.sin(t * (Math.PI * 2 / prm.wigT)) * prm.wig * e;
      sc = 1 + Math.sin(t * (Math.PI * 2 / prm.breathT) + 0.4) * prm.breath * e;
      if (p.mood === "restless") {
        /* fidgety little tremor — sitting out the fun, not suffering */
        tx += Math.sin(t * 23) * 0.35 * e;
      }
      if (p.isEgg) {
        /* eggs rock gently side to side */
        rot += Math.sin(t * (Math.PI * 2 / 4.0)) * 4 * e;
        ty *= 0.4; tx *= 0.4;
      }
    }

    /* one-shot effects override/compose */
    var base = { ty: ty, tx: tx, rot: rot, sc: sc, sx: sx, sy: s.sy };
    if (p.fx) {
      var fx = p.fx, ft = (t - fx.t0);
      var fp = ft / fx.dur;
      if (fp >= 1) { p.fx = null; }
      else applyFx(p, fx.kind, fp, fx, base);
    }

    if (p.wrap) {
      /* squash spring (base.sy/base.sx) composes with idle breath (base.sc) */
      var finSy = base.sy * base.sc;
      var finSx = base.sx * (1 + (1 - base.sc) * 0.6);
      p.wrap.style.transform =
        "translate(" + base.tx.toFixed(2) + "px," + base.ty.toFixed(2) + "px)" +
        " rotate(" + base.rot.toFixed(2) + "deg)" +
        " scale(" + finSx.toFixed(3) + "," + finSy.toFixed(3) + ")";
    }

    /* eye tracking: look toward the cursor, gently */
    if (p.eyes && !REDUCED) {
      if (t - p.rectT > 0.6 || !p.rect) {
        p.rect = p.svg.getBoundingClientRect();
        p.rectT = t;
      }
      var cx = p.rect.left + p.rect.width / 2;
      var cy = p.rect.top + p.rect.height / 2;
      var dx = mouse.x - cx, dy = mouse.y - cy;
      var dist = Math.sqrt(dx * dx + dy * dy) || 1;
      var reach = Math.min(1, dist / 260);          // closer = stronger look
      p.eyeTgt.x = (dx / dist) * 2.4 * reach;
      p.eyeTgt.y = (dy / dist) * 2.0 * reach;
      p.eyeCur.x += (p.eyeTgt.x - p.eyeCur.x) * Math.min(1, dt * 8);
      p.eyeCur.y += (p.eyeTgt.y - p.eyeCur.y) * Math.min(1, dt * 8);
      if (Math.abs(p.eyeCur.x) > 0.05 || Math.abs(p.eyeCur.y) > 0.05) {
        p.eyes.style.transform =
          "translate(" + p.eyeCur.x.toFixed(2) + "px," + p.eyeCur.y.toFixed(2) + "px)";
      } else {
        p.eyes.style.transform = "";
      }
    }

    /* eggs wiggle every few seconds, unprompted — pure delight */
    if (p.isEgg && !REDUCED) {
      p.wigT -= dt;
      if (p.wigT <= 0) { p.wigT = 5 + Math.random() * 7; wiggle(p); }
    }
  }

  function applyFx(p, kind, fp, fx, base) {
    /* fp: 0..1 progress. Mutates the shared base object in place. */
    if (kind === "celebrate") {
      var ez = 1 - Math.pow(1 - fp, 3);
      base.rot += 360 * ez;
      var bump = Math.sin(fp * Math.PI);
      base.ty -= bump * 8;
    } else if (kind === "wiggle") {
      base.rot += Math.sin(fp * Math.PI * 3) * 10 * (1 - fp);
    } else if (kind === "feed") {
      if (fp < 0.38) {           // lunge toward the food
        var l = fp / 0.38, le = 1 - Math.pow(1 - l, 2);
        base.ty += le * 7; base.sc *= (1 + le * 0.07);
      } else {                   // chomp chomp
        var cp = (fp - 0.38) / 0.62;
        base.sy = 1 - Math.abs(Math.sin(cp * Math.PI * 2)) * 0.22;
        base.sx = 1 + Math.abs(Math.sin(cp * Math.PI * 2)) * 0.16;
      }
    } else if (kind === "play") {
      base.ty -= Math.abs(Math.sin(fp * Math.PI * 3)) * 9 * (1 - fp * 0.4);
      base.rot += Math.sin(fp * Math.PI * 2) * 6 * (1 - fp);
    } else if (kind === "rest") {
      var r = 1 - Math.pow(1 - fp, 2);
      base.ty += r * 3; base.sc *= (1 - r * 0.04);
    }
  }

  /* ---------- public reactions ---------- */
  function pat(svg, withParticles) {
    var p = findPet(svg);
    if (!p) return;
    if (REDUCED) { pulse(svg); return; }
    p.spring.vy = -7.5;                       // squash!
    if (withParticles !== false) burst(svg, ["💧", "✨", "💙"]);
  }
  function wiggle(svg) {
    var p = findPet(svg);
    if (!p || REDUCED) return;
    p.fx = { kind: "wiggle", t0: performance.now() / 1000, dur: 0.7 };
    poke();
  }
  function celebrate(svg) {
    var p = findPet(svg);
    if (!p) return;
    if (REDUCED) { pulse(svg); return; }
    p.fx = { kind: "celebrate", t0: performance.now() / 1000, dur: 1.1 };
    burst(svg, ["✨", "🌟", "💧", "🎉"]);
    poke();
  }
  function feed(svg) { react(svg, "feed"); }
  function play(svg) { react(svg, "play"); }
  function rest(svg) { react(svg, "rest"); }
  function react(svg, kind) {
    var p = findPet(svg);
    if (!p || REDUCED) return;
    p.fx = { kind: kind, t0: performance.now() / 1000, dur: kind === "feed" ? 0.75 : 0.9 };
    poke();
  }
  function pulse(svg) {
    if (!svg || !svg.style) return;
    svg.style.transition = "opacity .35s ease";
    svg.style.opacity = "0.55";
    setTimeout(function () { svg.style.opacity = ""; }, 350);
  }

  /* ---------- particles (transform + opacity only) ---------- */
  function burst(svg, glyphs) {
    if (REDUCED || !svg || !svg.parentElement) return;
    try {
      var host = svg.parentElement;
      var cs = window.getComputedStyle(host);
      if (cs.position === "static") host.style.position = "relative";
      var n = Math.min(5, glyphs.length + 1);
      for (var i = 0; i < n; i++) {
        var s = document.createElement("span");
        s.textContent = glyphs[i % glyphs.length];
        s.setAttribute("aria-hidden", "true");
        s.style.cssText = "position:absolute;left:50%;top:38%;pointer-events:none;" +
          "font-size:1.1rem;z-index:5;transform:translate(-50%,-50%);opacity:1;";
        host.appendChild(s);
        var dx = (Math.random() - 0.5) * 90;
        var dy = -(34 + Math.random() * 46);
        var anim = s.animate([
          { transform: "translate(-50%,-50%)", opacity: 1 },
          { transform: "translate(calc(-50% + " + dx + "px)," + dy + "px)", opacity: 0 }
        ], { duration: 750 + Math.random() * 250, easing: "cubic-bezier(.2,.7,.3,1)" });
        (function (el) { anim.onfinish = function () { el.remove(); }; })(s);
      }
    } catch (e) { /* decorative only */ }
  }

  /* ---------- hatch countdown ---------- */
  function fmtClock(sec) {
    sec = Math.max(0, Math.ceil(sec));
    var m = Math.floor(sec / 60), s = sec % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }
  function initCountdowns() {
    document.querySelectorAll("[data-hatch-seconds]").forEach(function (chip) {
      if (chip._tpClock) return;
      chip._tpClock = true;
      var left = parseInt(chip.getAttribute("data-hatch-seconds") || "0", 10);
      var grant = chip.getAttribute("data-hatch-grant") || "25";
      var clock = chip.querySelector(".hatch-clock");
      var panel = chip.closest(".egg-panel") || document;
      var btn = panel.querySelector("[data-hatch-btn]");
      function render() {
        if (clock) clock.textContent = fmtClock(left);
      }
      function ready() {
        chip.classList.add("ready");
        chip.innerHTML = "🐣 <b>Ready to hatch!</b> Your +" + grant +
          " Signal is waiting — tap that button! 🎉";
        if (btn) { btn.disabled = false; btn.classList.add("btn-pulse"); }
        var svg = panel.querySelector("svg[data-tidepal]");
        if (svg) celebrate(svg);
      }
      if (left <= 0) { ready(); return; }
      render();
      var iv = setInterval(function () {
        left -= 1;
        if (left <= 0) { clearInterval(iv); ready(); return; }
        render();
      }, 1000);
    });
  }

  /* ---------- hero demo egg (logged-out + newcomer delight) ---------- */
  var demoMsgs = [
    "hehe that tickles! 🥚",
    "I'm an Egg! Adopt me and I hatch in 5 minutes ⏱️",
    "Hatching is FREE — and it pays YOU +25 Signal ✨",
    "The Caretaker will wave you in, promise 👋",
    "ok ok, tap the big button below already! 👇"
  ];
  function initDemo() {
    var demo = document.querySelector("[data-tidepal-demo]");
    if (!demo || demo._tpDemo) return;
    demo._tpDemo = true;
    var bubble = demo.querySelector("[data-demo-bubble]");
    var count = document.getElementById("demo-count");
    var taps = 0;
    function say(i) { if (bubble) bubble.textContent = demoMsgs[i % demoMsgs.length]; }
    function onTap() {
      taps++;
      demo.classList.remove("egg-wiggle");
      void demo.offsetWidth;               // restart the CSS wiggle
      demo.classList.add("egg-wiggle");
      say(taps);
      burst(demo, ["🥚", "✨", "💧"]);
      if (taps === 3 && count) startPreview(count);
    }
    demo.addEventListener("click", onTap);
    demo.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onTap(); }
    });
  }
  function startPreview(count) {
    if (count._tpOn || REDUCED) return;
    count._tpOn = true;
    count.hidden = false;
    var left = 5;
    count.innerHTML = "👀 <i>a tiny preview</i> — hatching in <b>" + left + "</b>…";
    var iv = setInterval(function () {
      left -= 1;
      if (left <= 0) {
        clearInterval(iv);
        count.innerHTML = "🐣 <b>pop!</b> …and that's what hatching feels like. " +
          "The real one pays you <b>+25 Signal</b>. <i>(preview — adopt to play for real)</i>";
        var demo = document.querySelector("[data-tidepal-demo]");
        if (demo) burst(demo, ["🐣", "✨", "🎉", "💧", "🌟"]);
        return;
      }
      count.innerHTML = "👀 <i>a tiny preview</i> — hatching in <b>" + left + "</b>…";
    }, 1000);
  }

  /* ---------- just-adopted delight ---------- */
  function initJustAdopted() {
    document.querySelectorAll("[data-tidepal-just-adopted]").forEach(function (panel) {
      if (panel._tpWelcomed) return;
      panel._tpWelcomed = true;
      var svg = panel.querySelector("svg[data-tidepal]");
      setTimeout(function () {
        if (svg) { wiggle(svg); setTimeout(function () { wiggle(svg); }, 800); }
        toast("🎉 Hatching started! Your egg is warming up — come back in a few minutes for the big moment. 🐣");
      }, 600);
    });
  }
  function toast(msg) {
    var t = document.createElement("div");
    t.className = "tp-toast";
    t.setAttribute("role", "status");
    t.textContent = msg;
    document.body.appendChild(t);
    requestAnimationFrame(function () { t.classList.add("show"); });
    setTimeout(function () {
      t.classList.remove("show");
      setTimeout(function () { t.remove(); }, 500);
    }, 6000);
  }

  /* ---------- celebration triggers ---------- */
  function initCelebrate() {
    document.querySelectorAll("[data-tidepal-celebrate]").forEach(function (zone) {
      if (zone._tpCel) return;
      zone._tpCel = true;
      var svg = zone.querySelector("svg[data-tidepal]");
      if (svg) setTimeout(function () { celebrate(svg); }, 700);
    });
  }

  /* ---------- care-button reactions (feed/play/rest forms) ---------- */
  function initCareButtons() {
    document.querySelectorAll("[data-tp-care]").forEach(function (btn) {
      if (btn._tpCare) return;
      btn._tpCare = true;
      btn.addEventListener("click", function (e) {
        if (btn.disabled) return;   // double-submit guard
        var form = btn.closest("form");
        var zone = btn.closest(".mypet") || btn.closest("[data-tidepal-zone]") || document;
        var svg = zone.querySelector("svg[data-tidepal]");
        if (!svg || REDUCED || !form) return;   // degrade: normal submit
        e.preventDefault();
        btn.disabled = true;        // prevent double taps during the delight beat
        var kind = btn.getAttribute("data-tp-care");
        react(svg, kind);
        burst(svg, kind === "feed" ? ["🍽️", "🐟", "✨"] : kind === "play" ? ["🎾", "✨", "💧"] : ["😴", "💤", "🌙"]);
        setTimeout(function () { form.submit(); }, 680);
      });
    });
  }

  /* ---------- adopt-form wiggle ---------- */
  function initAdoptForm() {
    document.querySelectorAll("form[data-tp-adopt]").forEach(function (form) {
      if (form._tpAdopt) return;
      form._tpAdopt = true;
      form.addEventListener("submit", function () {
        var demo = document.querySelector("[data-tidepal-demo]");
        if (demo && !REDUCED) {
          demo.classList.remove("egg-wiggle");
          void demo.offsetWidth;
          demo.classList.add("egg-wiggle");
        }
      });
    });
  }

  /* ---------- pat on click for any tidepal ---------- */
  function initPat() {
    document.querySelectorAll("svg[data-tidepal]").forEach(function (svg) {
      if (svg._tpPat) return;
      svg._tpPat = true;
      svg.style.cursor = "pointer";
      svg.addEventListener("click", function () { pat(svg, true); });
    });
  }

  /* ---------- boot ---------- */
  function init() {
    if (init._done) return;
    init._done = true;

    if (typeof window.IntersectionObserver === "function") {
      io = new IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
          for (var i = 0; i < pets.length; i++) {
            if (pets[i].svg === en.target) {
              pets[i].visible = en.isIntersecting;
              if (en.isIntersecting) { visible.add(pets[i]); poke(); }
              else visible.delete(pets[i]);
            }
          }
        });
      }, { threshold: 0.05 });
    }

    document.querySelectorAll("svg[data-tidepal]").forEach(register);
    if (!io) { pets.forEach(function (p) { p.visible = true; visible.add(p); }); poke(); }

    if (REDUCED) {
      /* still images, please: freeze SMIL too */
      document.querySelectorAll("svg[data-tidepal]").forEach(function (svg) {
        try { svg.pauseAnimations(); } catch (e) {}
      });
    }

    if (typeof window.MutationObserver === "function") {
      new MutationObserver(function (muts) {
        muts.forEach(function (m) {
          m.addedNodes.forEach(function (n) {
            if (n.nodeType !== 1) return;
            if (n.tagName && n.tagName.toLowerCase() === "svg" && n.hasAttribute("data-tidepal")) {
              register(n); initPat();
            } else if (n.querySelectorAll) {
              n.querySelectorAll("svg[data-tidepal]").forEach(function (s) { register(s); });
              initPat();
            }
          });
        });
      }).observe(document.body, { childList: true, subtree: true });
    }

    document.addEventListener("mousemove", function (e) {
      mouse.x = e.clientX; mouse.y = e.clientY;
    }, { passive: true });
    window.addEventListener("scroll", function () {
      pets.forEach(function (p) { p.rect = null; });
    }, { passive: true });

    initCountdowns();
    initDemo();
    initJustAdopted();
    initCelebrate();
    initCareButtons();
    initAdoptForm();
    initPat();
    poke();
  }

  window.TidepalAnim = {
    init: init, pat: pat, wiggle: wiggle, celebrate: celebrate,
    feed: feed, play: play, rest: rest, reduced: REDUCED
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
