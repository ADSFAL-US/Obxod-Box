/**
 * Базовые/legacy accent’ы.
 * connecting / waiting_net / success живут в отдельных файлах:
 *   connecting.js, network_down.js
 */
(function () {
  "use strict";

  if (!window.ObxodCube) {
    console.error("ObxodCube.animations: cube.js must load first");
    return;
  }

  var C = window.ObxodCube;

  // hold: ошибка (цвет на face через status-error)
  C.registerAnimation("error", {
    type: "loop",
  });

  // oneshot: вход в ошибку
  C.registerAnimation("error_enter", {
    type: "oneshot",
    enter: function (ctx) {
      if (!ctx || !ctx.container) return;
      ctx.container.classList.add("cube-accent-error-shake");
      setTimeout(function () {
        ctx.container.classList.remove("cube-accent-error-shake");
      }, 400);
    },
  });
})();
