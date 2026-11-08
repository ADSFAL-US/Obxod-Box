/**
 * Онбординг первого запуска.
 *
 * Работает поверх настоящего ObxodCube (assets/cube/*): куб не дублируется,
 * а переезжает в правую половину окна через CSS-класс body.onboarding.
 *
 * Что делает этот файл:
 *   - 4 слайда, навигация (точки, стрелка, Esc/Backspace/Enter)
 *   - сцены Обходика: взгляд на элементы, моргание, демо-подключение
 *   - зона реакции на курсор (вне зоны куб «не видит» мышь)
 *   - финальный транзишн: куб уезжает на место, кнопки появляются от центра
 */
(function () {
  "use strict";

  var C = window.ObxodCube;

  var root = document.getElementById("obRoot");
  if (!root) return;

  var slides = Array.prototype.slice.call(root.querySelectorAll(".ob-slide"));
  var layers = Array.prototype.slice.call(root.querySelectorAll(".ob-layer"));
  var dotsBox = document.getElementById("obDots");
  var nextBtn = document.getElementById("obNext");
  var backBtn = document.getElementById("obBack");
  var options = Array.prototype.slice.call(root.querySelectorAll(".ob-option"));
  var importPanel = document.getElementById("obImport");
  var importInput = document.getElementById("obImportUrl");
  var importBtn = document.getElementById("obImportBtn");
  var importStatus = document.getElementById("obImportStatus");

  var index = 0;
  var total = slides.length;
  var selectedOption = null;
  var importDone = false;
  var active = false;
  var finishing = false;

  // ── утилиты ──────────────────────────────────────────────────────
  var timers = [];
  function later(ms, fn) { timers.push(setTimeout(fn, ms)); }
  function every(ms, fn) { var id = setInterval(fn, ms); timers.push(id); return id; }
  function clearTimers() {
    timers.forEach(function (id) { clearTimeout(id); clearInterval(id); });
    timers = [];
  }

  // ── точки ────────────────────────────────────────────────────────
  slides.forEach(function (_, i) {
    var b = document.createElement("button");
    b.className = "ob-dot";
    b.type = "button";
    b.setAttribute("aria-label", "Слайд " + (i + 1));
    b.addEventListener("click", function () { go(i); });
    dotsBox.appendChild(b);
  });
  var dots = Array.prototype.slice.call(dotsBox.children);

  // ── взгляд / моргание ────────────────────────────────────────────
  var face = null;
  var lastRealMove = 0;

  function gazeAtPoint(x, y) {
    var ev = new MouseEvent("mousemove", { clientX: x, clientY: y, bubbles: true });
    ev.__obxodSynthetic = true;
    document.body.dispatchEvent(ev);
  }

  function gazeAt(el) {
    if (!el) return;
    var r = el.getBoundingClientRect();
    gazeAtPoint(r.left + r.width / 2, r.top + r.height / 2);
  }

  /** Сценарный взгляд молчит, если пользователь только что двигал мышь. */
  function autoGaze(el) {
    if (Date.now() - lastRealMove < 1600) return;
    gazeAt(el);
  }

  function autoGazePoint(x, y) {
    if (Date.now() - lastRealMove < 1600) return;
    gazeAtPoint(x, y);
  }

  function blink() {
    if (!face || !C) return;
    var st = C.getStatus();
    if (st === "starting" || st === "network_down") return;
    face.classList.add("ob-blink");
    later(120, function () { if (face) face.classList.remove("ob-blink"); });
  }

  // ── зона реакции на курсор ───────────────────────────────────────
  // Родной cube.js слушает mousemove на document.body. Гасим события вне
  // зоны в capture-фазе (срабатывает раньше body) и возвращаем взгляд
  // в нейтраль на выходе из зоны.
  var ZONE_PAD = 140;
  var zoneEl = null;
  var insideZone = false;

  function inZone(x, y) {
    if (!zoneEl) return false;
    var r = zoneEl.getBoundingClientRect();
    return (
      x >= r.left - ZONE_PAD && x <= r.right + ZONE_PAD &&
      y >= r.top - ZONE_PAD && y <= r.bottom + ZONE_PAD
    );
  }

  function installCursorZone() {
    zoneEl = document.getElementById("cube-root");

    document.addEventListener("mousemove", function (e) {
      if (e.__obxodSynthetic) return;
      if (!active) return;

      if (inZone(e.clientX, e.clientY)) {
        insideZone = true;
        return;
      }

      e.stopPropagation();

      if (insideZone) {
        insideZone = false;
        var r = zoneEl.getBoundingClientRect();
        gazeAtPoint(r.left + r.width / 2, r.top + r.height / 2);
      }
    }, true);

    document.addEventListener("mouseleave", function () {
      if (!insideZone || !active) return;
      insideZone = false;
      var r = zoneEl.getBoundingClientRect();
      gazeAtPoint(r.left + r.width / 2, r.top + r.height / 2);
    });
  }

  // ── демо-подключение ─────────────────────────────────────────────
  var demoRunning = false;

  function demoConnect() {
    if (!C || demoRunning) return;
    demoRunning = true;
    C.setStatus("starting");
    later(1100, function () {
      C.setStatus("running");
      later(2200, function () {
        C.setStatus("stopped");
        demoRunning = false;
      });
    });
  }

  function tapPulse() {
    var ring = document.getElementById("obTapRing");
    if (!ring) return;
    ring.classList.remove("pulse");
    void ring.offsetWidth;
    ring.classList.add("pulse");
  }

  // ── сцены ────────────────────────────────────────────────────────
  function sceneIntro() {
    if (C) C.setStatus("stopped");

    every(2600, function () {
      var r = document.getElementById("cube-root").getBoundingClientRect();
      autoGazePoint(
        r.left + r.width / 2 + (Math.random() * 260 - 130),
        r.top + r.height / 2 + (Math.random() * 160 - 80)
      );
    });
    every(3400, blink);
    every(5200, function () {
      tapPulse();
      later(180, demoConnect);
    });
  }

  function sceneSelectors() {
    if (C) C.setStatus("stopped");
    var provider = document.getElementById("obMiniProvider");
    var config = document.getElementById("obMiniConfig");
    var i = 0;
    every(1900, function () {
      autoGaze(i % 2 === 0 ? provider : config);
      i++;
    });
    every(2900, blink);
  }

  function sceneButtons() {
    if (C) C.setStatus("stopped");
    var btns = [
      document.getElementById("obOrbitAdd"),
      document.getElementById("obOrbitRefresh"),
      document.getElementById("obOrbitPing"),
    ];
    var i = 0;
    every(1700, function () {
      autoGaze(btns[i % btns.length]);
      i++;
    });
    every(3100, blink);
  }

  function sceneChoice() {
    if (C) C.setStatus("stopped");
    var r = document.getElementById("cube-root").getBoundingClientRect();
    var cx = r.left + r.width / 2;
    var cy = r.top + r.height / 2;
    var spots = [[cx + 150, cy - 170], [cx - 150, cy - 170], [cx, cy]];
    var i = 0;
    every(2100, function () {
      var p = spots[i % spots.length];
      autoGazePoint(p[0], p[1]);
      i++;
    });
    every(3300, blink);
  }

  var scenes = [sceneIntro, sceneSelectors, sceneButtons, sceneChoice];

  // ── подсветка элементов сцены ────────────────────────────────────
  var hlTimer = null;

  function startHighlights() {
    if (hlTimer) { clearInterval(hlTimer); hlTimer = null; }

    var targets = [];
    if (index === 1) {
      targets = [document.getElementById("obMiniProvider"), document.getElementById("obMiniConfig")];
    } else if (index === 2) {
      targets = [
        document.getElementById("obOrbitAdd"),
        document.getElementById("obOrbitRefresh"),
        document.getElementById("obOrbitPing"),
      ];
    }
    if (!targets.length) return;

    var cls = index === 1 ? "lit" : "highlight";
    var n = 0;
    function step() {
      targets.forEach(function (t) { if (t) t.classList.remove(cls); });
      var cur = targets[n % targets.length];
      if (cur) {
        cur.classList.add(cls);
        autoGaze(cur);
      }
      n++;
    }
    step();
    hlTimer = setInterval(step, 1600);
  }

  // ── рендер ───────────────────────────────────────────────────────
  function render() {
    slides.forEach(function (s, i) {
      s.classList.toggle("active", i === index);
      s.classList.toggle("leaving", i < index);
    });
    layers.forEach(function (l, i) {
      l.classList.toggle("active", i === index);
    });
    dots.forEach(function (d, i) {
      d.classList.toggle("active", i === index);
    });

    backBtn.classList.toggle("visible", index > 0);
    var last = index === total - 1;
    nextBtn.disabled = last && !selectedOption;
    nextBtn.textContent = last ? "✓" : "→";
    nextBtn.title = last ? "Завершить" : "Дальше (Enter)";

    clearTimers();
    if (demoRunning && C) {
      demoRunning = false;
      C.setStatus("stopped");
    }
    var ring = document.getElementById("obTapRing");
    if (ring) ring.classList.remove("pulse");

    scenes[index]();
    startHighlights();
  }

  function go(n) {
    var next = Math.max(0, Math.min(total - 1, n));
    if (next === index) return;
    index = next;
    render();
  }

  function next() {
    if (index === total - 1) { finish(); return; }
    go(index + 1);
  }

  function back() { go(index - 1); }

  // ── выбор варианта ───────────────────────────────────────────────
  options.forEach(function (opt) {
    opt.addEventListener("click", function () {
      selectedOption = opt.dataset.option;
      options.forEach(function (o) { o.classList.toggle("selected", o === opt); });
      toggleImport(selectedOption === "third");
      render();
    });
  });

  function toggleImport(show) {
    if (!importPanel) return;
    importPanel.classList.toggle("visible", show);
    if (show) {
      setTimeout(function () {
        importPanel.scrollIntoView({ block: "end", behavior: "auto" });
        if (importInput) importInput.focus({ preventScroll: true });
      }, 320);
    }
  }

  function setImportStatus(text, kind) {
    if (!importStatus) return;
    importStatus.textContent = text || "";
    importStatus.className = "ob-import-status" + (kind ? " " + kind : "");
  }

  if (importBtn) {
    importBtn.addEventListener("click", function () {
      var url = importInput ? importInput.value.trim() : "";
      if (!url) {
        setImportStatus("Вставьте ссылку подписки", "error");
        return;
      }
      importBtn.disabled = true;
      setImportStatus("Импортирую…");
      sendToPython({ action: "onboarding_import", url: url });
    });
  }

  if (importInput) {
    importInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        e.preventDefault();
        e.stopPropagation();
        if (importBtn) importBtn.click();
      }
    });
  }

  /** Ответ Python на импорт из онбординга. */
  window.obOnboardingImportResult = function (ok, message) {
    if (importBtn) importBtn.disabled = false;
    importDone = !!ok;
    setImportStatus(message || (ok ? "Готово" : "Ошибка импорта"), ok ? "ok" : "error");
    if (ok) {
      nextBtn.disabled = false;
      if (C) C.setStatus("stopped");
    }
  };

  // ── финал ────────────────────────────────────────────────────────
  function finish() {
    if (finishing) return;
    if (index === total - 1 && !selectedOption) return;
    finishing = true;

    sendToPython({
      action: "onboarding_finish",
      option: selectedOption,
      imported: importDone,
    });

    // 1) куб уезжает на своё место
    document.body.classList.remove("onboarding");
    document.body.classList.add("onboarding-leaving");

    later(640, function () {
      // 2) оверлей убираем, кнопки появляются от центра к краям
      document.body.classList.remove("onboarding-leaving");
      active = false;
      clearTimers();
      if (hlTimer) { clearInterval(hlTimer); hlTimer = null; }
      if (C) {
        C.onToggle(null);   // возвращаем штатный клик по кубу
        C.setStatus("stopped");
      }
      revealUi();
    });
  }

  function revealUi() {
    var els = [
      document.querySelector(".import-btn"),
      document.querySelector(".refresh-btn"),
      document.querySelector(".hamburger"),
      document.querySelector(".toggle-wrapper"),
      document.getElementById("providerCard"),
      document.getElementById("configCard"),
    ].filter(Boolean);

    var cx = window.innerWidth / 2;
    var cy = window.innerHeight / 2;
    var maxDist = 1;

    var items = els.map(function (el) {
      var r = el.getBoundingClientRect();
      var ex = r.left + r.width / 2;
      var ey = r.top + r.height / 2;
      var dist = Math.hypot(ex - cx, ey - cy);
      if (dist > maxDist) maxDist = dist;
      return { el: el, dx: cx - ex, dy: cy - ey, dist: dist };
    });

    items.forEach(function (it) {
      it.el.style.setProperty("--rv-x", it.dx.toFixed(1) + "px");
      it.el.style.setProperty("--rv-y", it.dy.toFixed(1) + "px");
      // ближние к центру появляются раньше — волна идёт от центра к краям
      var delay = Math.round((it.dist / maxDist) * 260);
      it.el.style.setProperty("--rv-delay", delay + "ms");
    });

    document.body.classList.add("ui-reveal");
    void document.body.offsetWidth;

    var released = false;
    function release() {
      if (released) return;
      released = true;
      document.body.classList.remove("ui-reveal");
      later(900, function () {
        items.forEach(function (it) {
          it.el.style.removeProperty("--rv-x");
          it.el.style.removeProperty("--rv-y");
          it.el.style.removeProperty("--rv-delay");
        });
      });
    }

    // rAF может не сработать (фон/headless) — страхуемся таймером
    requestAnimationFrame(function () {
      requestAnimationFrame(release);
    });
    later(120, release);
  }

  // ── клавиатура ───────────────────────────────────────────────────
  document.addEventListener("keydown", function (e) {
    if (!active) return;
    var tag = e.target && e.target.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA") return;

    if (e.key === "Escape" || e.key === "Backspace") {
      e.preventDefault();
      back();
    } else if (e.key === "Enter") {
      e.preventDefault();
      next();
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      next();
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      back();
    }
  });

  nextBtn.addEventListener("click", next);
  backBtn.addEventListener("click", back);

  // ── запуск / остановка ───────────────────────────────────────────
  window.obStartOnboarding = function () {
    if (active) return;
    active = true;
    finishing = false;
    index = 0;
    selectedOption = null;
    importDone = false;
    options.forEach(function (o) { o.classList.remove("selected"); });
    toggleImport(false);
    setImportStatus("");
    if (importBtn) importBtn.disabled = false;

    document.body.classList.add("onboarding");
    document.body.classList.remove("onboarding-leaving", "ui-reveal");

    // в онбординге клик по кубу не должен дёргать подключение
    if (C) {
      C.onToggle(function () {});
      C.setStatus("stopped");
    }

    render();
  };

  window.obIsOnboardingActive = function () { return active; };

  // ── инициализация ────────────────────────────────────────────────
  function init() {
    face = document.querySelector("#cube-root .face-square");
    installCursorZone();

    document.addEventListener("mousemove", function (e) {
      if (e.isTrusted) lastRealMove = Date.now();
    }, true);

    // Показывать ли онбординг — решает Python (флаг в конфиге) и вызывает
    // window.obStartOnboarding() сам, когда состояние уже разложено.
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
