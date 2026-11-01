/**
 * Connecting accent:
 *  enter  → 2 глаза уменьшаются, третья точка «выливается» в центр,
 *           три точки волной вверх-вниз (янтарь), пока status=starting
 *  success (starting→running) → точки выравниваются, центр вливается
 *           в боковые, глаза зелёные
 */
(function () {
  "use strict";

  if (!window.ObxodCube) {
    console.error("connecting.js: ObxodCube missing");
    return;
  }

  var C = window.ObxodCube;
  var timers = [];

  function clearTimers() {
    timers.forEach(function (id) {
      clearTimeout(id);
    });
    timers = [];
  }

  function later(ms, fn) {
    var id = setTimeout(fn, ms);
    timers.push(id);
    return id;
  }

  function ensureCenter(face) {
    var el = face.querySelector(".eye.center");
    if (!el) {
      el = document.createElement("div");
      el.className = "eye center";
      face.appendChild(el);
    }
    return el;
  }

  function removeCenter(face) {
    var el = face.querySelector(".eye.center");
    if (el) el.remove();
  }

  function resetFaceClasses(face) {
    face.classList.remove(
      "cube-connecting",
      "cube-connecting-ready",
      "cube-connecting-wave",
      "cube-connecting-level",
      "cube-connecting-merge",
      "cube-connecting-done"
    );
  }

  function refreshEyes(ctx) {
    if (!ctx || !ctx.face) return;
    ctx.eyes = ctx.face.querySelectorAll(".eye");
  }

  C.registerAnimation("connecting", {
    type: "loop",
    containerClass: "cube-accent-connecting",
    enter: function (ctx) {
      clearTimers();
      if (!ctx || !ctx.face) return;

      resetFaceClasses(ctx.face);
      // убрать sleep-классы если были
      ctx.face.classList.remove("cube-asleep", "cube-wake");
      var zzz = ctx.container && ctx.container.querySelector(".cube-zzz");
      if (zzz) zzz.remove();

      ensureCenter(ctx.face);
      refreshEyes(ctx);

      ctx.face.classList.add("cube-connecting");

      // фаза 1: появление центральной точки
      later(40, function () {
        ctx.face.classList.add("cube-connecting-ready");
      });

      // фаза 2: волна
      later(480, function () {
        ctx.face.classList.add("cube-connecting-wave");
      });
    },
    exit: function (ctx) {
      clearTimers();
      if (!ctx || !ctx.face) return;
      // если уходим не в success-merge — жёсткий сброс
      if (!ctx.face.classList.contains("cube-connecting-merge") &&
          !ctx.face.classList.contains("cube-connecting-done")) {
        resetFaceClasses(ctx.face);
        removeCenter(ctx.face);
        refreshEyes(ctx);
      }
      if (ctx.container) {
        ctx.container.classList.remove("cube-accent-connecting");
      }
    },
  });

  C.registerAnimation("success", {
    type: "oneshot",
    enter: function (ctx) {
      clearTimers();
      if (!ctx || !ctx.face) return;

      // если не были в connecting — просто вспышка зелёного
      if (!ctx.face.classList.contains("cube-connecting")) {
        ensureCenter(ctx.face);
        ctx.face.classList.add("cube-connecting", "cube-connecting-ready");
        refreshEyes(ctx);
      }

      ctx.face.classList.remove("cube-connecting-wave");
      ctx.face.classList.add("cube-connecting-level");

      later(220, function () {
        ctx.face.classList.remove("cube-connecting-level");
        ctx.face.classList.add("cube-connecting-merge", "cube-connecting-done");
      });

      later(650, function () {
        resetFaceClasses(ctx.face);
        removeCenter(ctx.face);
        refreshEyes(ctx);
        // финальный статус-класс поставит setStatus (status-running)
        if (ctx.container) {
          ctx.container.classList.remove("cube-accent-connecting");
        }
        // лёгкая вспышка успеха
        ctx.container.classList.add("cube-accent-success");
        later(600, function () {
          if (ctx.container) {
            ctx.container.classList.remove("cube-accent-success");
          }
        });
      });
    },
  });
})();
