/**
 * Network-down accent:
 *  enter → янтарь, глазки закрываются (дуги), Zzz из правого верхнего угла
 *  exit  → резкое пробуждение (открытие глаз); цвет дальше ставит setStatus
 */
(function () {
  "use strict";

  if (!window.ObxodCube) {
    console.error("network_down.js: ObxodCube missing");
    return;
  }

  var C = window.ObxodCube;
  var wakeTimer = null;

  function clearWake() {
    if (wakeTimer) {
      clearTimeout(wakeTimer);
      wakeTimer = null;
    }
  }

  function ensureZzz(container) {
    var layer = container.querySelector(".cube-zzz");
    if (layer) return layer;
    layer = document.createElement("div");
    layer.className = "cube-zzz";
    layer.setAttribute("aria-hidden", "true");
    layer.innerHTML = "<span>z</span><span>z</span><span>Z</span>";
    container.appendChild(layer);
    return layer;
  }

  function removeZzz(container) {
    if (!container) return;
    var layer = container.querySelector(".cube-zzz");
    if (layer) layer.remove();
  }

  function removeCenter(face) {
    var el = face && face.querySelector(".eye.center");
    if (el) el.remove();
  }

  function refreshEyes(ctx) {
    if (!ctx || !ctx.face) return;
    ctx.eyes = ctx.face.querySelectorAll(".eye");
  }

  C.registerAnimation("waiting_net", {
    type: "loop",
    containerClass: "cube-accent-asleep",
    enter: function (ctx) {
      clearWake();
      if (!ctx || !ctx.face || !ctx.container) return;

      // сброс connecting-артефактов
      ctx.face.classList.remove(
        "cube-connecting",
        "cube-connecting-ready",
        "cube-connecting-wave",
        "cube-connecting-level",
        "cube-connecting-merge",
        "cube-connecting-done",
        "cube-wake"
      );
      removeCenter(ctx.face);
      refreshEyes(ctx);

      ctx.face.classList.add("cube-asleep");
      ensureZzz(ctx.container);
    },
    exit: function (ctx) {
      clearWake();
      if (!ctx || !ctx.face) return;

      removeZzz(ctx.container);
      ctx.face.classList.remove("cube-asleep");
      ctx.face.classList.add("cube-wake");

      // короткое «открытие», потом класс снимаем
      wakeTimer = setTimeout(function () {
        if (ctx.face) ctx.face.classList.remove("cube-wake");
        if (ctx.container) {
          ctx.container.classList.remove("cube-accent-asleep");
        }
        refreshEyes(ctx);
      }, 180);
    },
  });
})();
