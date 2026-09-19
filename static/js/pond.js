/* Town Pond caretaker + living-scene behaviors.
   Transform/opacity only. Everything goes still under prefers-reduced-motion. */
(function () {
  "use strict";
  var stage = document.getElementById("pond-stage");
  if (!stage) return;
  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var residents = Array.prototype.slice.call(stage.querySelectorAll(".resident"));
  var mine = residents.filter(function (r) { return r.dataset.mine === "1"; });
  var caretaker = document.getElementById("caretaker");
  var bubble = document.getElementById("caretaker-bubble");
  var bubbleText = bubble ? bubble.querySelector(".ct-line") : null;

  // SMIL (inside the pet art) ignores CSS media queries — pause it directly.
  if (reduced) {
    Array.prototype.forEach.call(stage.querySelectorAll("svg"), function (s) {
      if (typeof s.pauseAnimations === "function") s.pauseAnimations();
    });
  }

  function nameOf(r) { return r.dataset.name || "everyone"; }
  function pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }
  function anyName() {
    return residents.length ? nameOf(pick(residents)) : "everyone";
  }
  function mineName() { return mine.length ? nameOf(pick(mine)) : anyName(); }
  function fill(tpl) { return tpl.split("{name}").join(anyName())
                                .split("{mine}").join(mineName()); }

  /* ---------- dialogue ---------- */
  var GENERAL = [
    "The water's warm, the pads are sunny, and the treats flow like a creek.",
    "I sweep the lily pads every morning. Someone has to keep them photo-ready.",
    "Releasing a Tidepal isn't a goodbye — it's a pond upgrade.",
    "No sad fish in my pond. Happy fish. Soggy, happy fish.",
    "Everyone here gets fed twice a day. And snacks. Snacks are important.",
    "Every Tidepal here is loved by name. I make sure of it.",
    "The moon feeds the pond at night. I just make the deliveries.",
    "{name} is doing great — raced the lily pads all morning and won.",
    "Oh, {name}? Three breakfasts today. Don't tell the others.",
    "{name} claimed the sunny pad again. Third week running. Fair's fair.",
    "I tucked {name} in last night. Sound asleep by moonrise.",
    "{name}'s favorite game is ripple-chase. {name} always wins.",
    "{name} ate lunch, then ate lunch's lunch. Growing keeper, that one.",
    "Just taught {name} to wave with a fin. Very official now."
  ];
  var RECOGNITION = [
    "oh — {mine} knows you're here.",
    "Look who's swimming over — {mine} spotted you from the deep end.",
    "{mine} remembers you. They never forgot.",
    "Well well — {mine} swam right over. Somebody's missed this face.",
    "Hi hi! {mine} has been practicing their happiest wiggle all week."
  ];
  var INVITE = [
    "No residents from you yet — the water's warm whenever you're ready.",
    "Your future Tidepal would love it here. Just saying. The snacks, mostly.",
    "Whenever you're ready, there's a sunny pad with your name on it."
  ];
  var EMPTY = [
    "The pond's resting today — every Tidepal is home with their keeper.",
    "It's just me and the ripples right now. Peaceful, honestly.",
    "Quiet pond, happy keepers. My favorite kind of afternoon."
  ];

  var linePool, greeted = false;
  function say(text) {
    if (!bubbleText) return;
    bubbleText.textContent = text;
    bubble.classList.add("show");
  }

  if (residents.length === 0) {
    linePool = EMPTY;
  } else if (mine.length > 0) {
    linePool = GENERAL.concat(RECOGNITION);
  } else if (window.__POND_VISITOR) {
    linePool = GENERAL.concat(INVITE);
  } else {
    linePool = GENERAL;
  }

  if (!reduced) {
    // The tear-jerker beat: the visitor's own pets recognize them.
    if (mine.length > 0) {
      setTimeout(function () {
        mine.forEach(function (r) { r.classList.add("greet"); });
        say(fill(pick(RECOGNITION)));
        greeted = true;
      }, 1600);
    } else {
      say(fill(pick(linePool)));
    }
    var lineTimer = setInterval(function () {
      say(fill(pick(linePool)));
    }, 11000);
    void lineTimer;
  } else {
    // Reduced motion: one calm line, no rotation.
    say(fill(linePool[0]));
  }

  /* ---------- caretaker behaviors ---------- */
  function flashClass(cls, ms) {
    caretaker.classList.add(cls);
    setTimeout(function () { caretaker.classList.remove(cls); }, ms);
  }

  function tossFood() {
    if (reduced || !caretaker) return;
    flashClass("feeding", 3600);
    var ct = caretaker.getBoundingClientRect();
    var sb = stage.getBoundingClientRect();
    var targets = residents.length ? residents : [null];
    for (var i = 0; i < 7; i++) (function (i) {
      setTimeout(function () {
        var pellet = document.createElement("div");
        pellet.className = "pellet";
        var sx = (ct.left - sb.left) + ct.width * 0.4;
        var sy = (ct.top - sb.top) + ct.height * 0.35;
        var t = targets[i % targets.length];
        var ex, ey;
        if (t) {
          var r = t.getBoundingClientRect();
          ex = (r.left - sb.left) + r.width / 2 + (Math.random() * 30 - 15);
          ey = (r.top - sb.top) + r.height / 2;
        } else {
          ex = sb.width * (0.3 + Math.random() * 0.4);
          ey = sb.height * (0.45 + Math.random() * 0.2);
        }
        pellet.style.left = sx + "px";
        pellet.style.top = sy + "px";
        stage.appendChild(pellet);
        var anim = pellet.animate([
          { transform: "translate(0,0) scale(1)", opacity: 1 },
          { transform: "translate(" + ((ex - sx) * 0.5) + "px," +
                       ((ey - sy) * 0.6 - 26) + "px) scale(.85)", opacity: 1,
            offset: 0.55 },
          { transform: "translate(" + (ex - sx) + "px," + (ey - sy) + "px) scale(.5)",
            opacity: 0 }
        ], { duration: 1100, easing: "ease-in", fill: "forwards" });
        anim.onfinish = function () {
          pellet.remove();
          if (t) {
            t.classList.add("gobble");
            setTimeout(function () { t.classList.remove("gobble"); }, 1100);
          }
        };
      }, i * 320);
    })(i);
  }

  if (!reduced) {
    setInterval(function () {
      var roll = Math.random();
      if (roll < 0.38) {
        // sweeping / tidying the pond
        flashClass("sweeping", 6400);
        if (Math.random() < 0.5) say("Pads swept, ripples fluffed. A pond keeper's work is never done.");
      } else if (roll < 0.72) {
        tossFood();
        say(fill(pick([
          "Snack time! {name}, share with the others.",
          "Feeding the residents — everyone gets seconds. House rule.",
          "Fresh pond crunchies! {name}, that's your third bowl. Impressive."
        ])));
      } else {
        flashClass("waving", 3600);
        say(fill(pick([
          "Oh, hello visitor! Come see the pond — {name} is showing off today.",
          "Hi hi! Welcome to the pond. Mind the splashes, they're friendly.",
          greeted
            ? "Stay as long as you like. {mine} is so glad you came."
            : "Welcome to the Town Pond. Every Tidepal here is happy and loved."
        ])));
      }
    }, 17000);
  }

  caretaker.addEventListener("click", function () {
    if (!reduced) flashClass("waving", 3600);
    say(fill(pick(linePool)));
  });

  /* residents: click to visit their card */
  residents.forEach(function (r) {
    r.addEventListener("click", function () {
      var card = document.getElementById("pondcard-" + r.dataset.fmid);
      if (card) {
        card.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "center" });
        card.classList.add("pulse");
        setTimeout(function () { card.classList.remove("pulse"); }, 2200);
      }
    });
  });
})();
