/**
 * ObxodCube — ядро «лица» / куба.
 *
 * Отвечает за:
 *  - разметку и idle-слежение за курсором
 *  - статус (цвет рамки / глаз / подпись)
 *  - реестр акцентных анимаций и их запуск
 *
 * Анимации подключаются отдельно через ObxodCube.registerAnimation(...)
 * или файлы в assets/cube/animations/ (см. registry.js).
 *
 * Публичный API (window.ObxodCube):
 *   mount(options?)
 *   setStatus(status, message?)
 *   getStatus()
 *   play(name, opts?)
 *   stopAccent()
 *   registerAnimation(name, definition)
 *   setWindowVisible(visible)
 *   onToggle(callback)
 */
(function (global) {
  "use strict";

  const STATUS_LABELS = {
    stopped: "Нажмите на куб для подключения",
    starting: "Подключение...",
    running: "Подключено",
    error: "Ошибка",
    network_down: "Нет сети — ждём подключения...",
  };

  /** @type {Record<string, AnimationDef>} */
  const registry = Object.create(null);

  /**
   * @typedef {object} AnimationDef
   * @property {'loop'|'oneshot'} [type]
   * @property {string} [containerClass]  CSS-класс на .face-container
   * @property {string} [faceClass]       доп. класс на .face-square
   * @property {(ctx: CubeContext, opts?: object) => void|Promise<void>} [enter]
   * @property {(ctx: CubeContext) => void} [exit]
   */

  /** @type {CubeContext|null} */
  let ctx = null;
  let currentStatus = "stopped";
  let activeAccentName = null;
  /** @type {null|((e: Event) => void)} */
  let toggleHandler = null;

  // idle look-at
  const target = { x: 0, y: 0 };
  const current = { x: 0, y: 0 };
  let rafId = null;
  let idleRunning = false;

  function $(sel, root) {
    return (root || document).querySelector(sel);
  }

  function buildMarkup() {
    return (
      '<div class="face-container" id="faceContainer" title="Подключиться / Отключиться">' +
      '  <div class="face-square" id="faceSquare">' +
      '    <div class="eye left"></div>' +
      '    <div class="eye right"></div>' +
      "  </div>" +
      "</div>"
    );
  }

  function ensureMounted(options) {
    if (ctx) return ctx;

    const opts = options || {};
    let area = opts.root
      ? typeof opts.root === "string"
        ? $(opts.root)
        : opts.root
      : $("#cube-root") || $(".face-area");

    if (!area) {
      area = document.createElement("div");
      area.className = "face-area";
      area.id = "cube-root";
      document.body.appendChild(area);
    }

    if (!area.querySelector(".face-container")) {
      area.innerHTML = buildMarkup();
    }

    const container = area.querySelector(".face-container");
    const face = area.querySelector(".face-square");
    const eyes = area.querySelectorAll(".eye");

    ctx = {
      root: area,
      container: container,
      face: face,
      eyes: eyes,
      statusDot: opts.statusDot
        ? typeof opts.statusDot === "string"
          ? $(opts.statusDot)
          : opts.statusDot
        : $("#xrayDot"),
      statusText: opts.statusText
        ? typeof opts.statusText === "string"
          ? $(opts.statusText)
          : opts.statusText
        : $("#xrayText"),
    };

    container.addEventListener("click", function (e) {
      if (typeof toggleHandler === "function") {
        toggleHandler(e);
        return;
      }
      // дефолт: как раньше — toggle connection
      if (currentStatus === "starting") return;
      if (typeof global.sendToPython === "function") {
        global.sendToPython({ action: "toggle_connection" });
      }
    });

    // idle tracking
    document.body.addEventListener("mousemove", onPointerMove);
    document.body.addEventListener("mouseleave", resetPointer);
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) stopIdle();
      else startIdle();
    });
    global.addEventListener("blur", stopIdle);
    global.addEventListener("focus", startIdle);
    startIdle();

    return ctx;
  }

  function onPointerMove(event) {
    if (!ctx) return;
    // Точка привязки взгляда — центр куба, а не окна:
    // куб может быть смещён (например, при открытых настройках).
    // Берём .face-area (без idle-смещения), чтобы не было обратной связи.
    const rect = (ctx.root || ctx.container).getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const halfW = Math.max(1, (document.documentElement.clientWidth || global.innerWidth) / 2);
    const halfH = Math.max(1, (document.documentElement.clientHeight || global.innerHeight) / 2);
    target.x = clamp((event.clientX - cx) / halfW, -1, 1);
    target.y = clamp((event.clientY - cy) / halfH, -1, 1);
  }

  function clamp(v, min, max) {
    return v < min ? min : v > max ? max : v;
  }

  /** Сброс взгляда в нейтраль (после смещения куба). */
  function resetLook() {
    target.x = 0;
    target.y = 0;
  }

  function resetPointer() {
    target.x = 0;
    target.y = 0;
  }

  function tickIdle() {
    if (!idleRunning || !ctx) return;
    current.x += (target.x - current.x) * 0.12;
    current.y += (target.y - current.y) * 0.12;
    ctx.container.style.setProperty("--cube-x", current.x * 4 + "px");
    ctx.container.style.setProperty("--cube-y", current.y * 4 + "px");
    ctx.eyes.forEach(function (eye) {
      eye.style.setProperty("--eye-x", current.x * 11 + "px");
      eye.style.setProperty("--eye-y", current.y * 11 + "px");
    });
    rafId = global.requestAnimationFrame(tickIdle);
  }

  function startIdle() {
    if (idleRunning) return;
    idleRunning = true;
    rafId = global.requestAnimationFrame(tickIdle);
  }

  function stopIdle() {
    idleRunning = false;
    if (rafId !== null) {
      global.cancelAnimationFrame(rafId);
      rafId = null;
    }
  }

  function clearAccentClasses() {
    if (!ctx) return;
    // снимаем известные loop-классы статуса и все registered containerClass
    ctx.container.classList.remove("status-starting", "status-network_down");
    Object.keys(registry).forEach(function (name) {
      const def = registry[name];
      if (def.containerClass) ctx.container.classList.remove(def.containerClass);
      if (def.faceClass) ctx.face.classList.remove(def.faceClass);
    });
  }

  function stopAccent() {
    if (activeAccentName && registry[activeAccentName] && registry[activeAccentName].exit && ctx) {
      try {
        registry[activeAccentName].exit(ctx);
      } catch (e) {
        console.warn("ObxodCube accent exit failed:", activeAccentName, e);
      }
    }
    clearAccentClasses();
    activeAccentName = null;
  }

  /**
   * Запуск акцентной анимации по имени.
   * @param {string} name
   * @param {object} [opts]
   */
  function play(name, opts) {
    ensureMounted();
    const def = registry[name];
    if (!def) {
      console.warn("ObxodCube: animation not registered:", name);
      return;
    }

    // loop сменяет предыдущий loop; oneshot не обязан гасить loop (можно поверх)
    if (def.type !== "oneshot") {
      stopAccent();
      activeAccentName = name;
    }

    if (def.containerClass) ctx.container.classList.add(def.containerClass);
    if (def.faceClass) ctx.face.classList.add(def.faceClass);

    if (typeof def.enter === "function") {
      try {
        def.enter(ctx, opts || {});
      } catch (e) {
        console.warn("ObxodCube accent enter failed:", name, e);
      }
    }
  }

  function registerAnimation(name, definition) {
    if (!name || !definition) return;
    registry[name] = definition;
  }

  /**
   * Карта статус → дефолтный accent (можно переопределить через options.statusMap).
   * oneshot на transition обрабатывается снаружи или здесь по prev→next.
   */
  const defaultStatusAccent = {
    starting: "connecting",
    network_down: "waiting_net",
    error: "error",
    // running / stopped — без loop
  };

  let prevStatus = "stopped";

  function setFaceStatusClass(st) {
    // не затираем accent-классы (cube-connecting, cube-asleep, …)
    var base = ["face-square"];
    var statusClasses = [
      "status-stopped",
      "status-starting",
      "status-running",
      "status-error",
      "status-network_down",
    ];
    statusClasses.forEach(function (c) {
      ctx.face.classList.remove(c);
    });
    if (st) ctx.face.classList.add("status-" + st);
    if (!ctx.face.classList.contains("face-square")) {
      ctx.face.classList.add("face-square");
    }
  }

  function setStatus(status, message) {
    ensureMounted();
    const st = status || "stopped";
    const msg = message || "";
    const was = prevStatus;
    prevStatus = st;
    currentStatus = st;

    setFaceStatusClass(st);

    // status label / dot (если есть в DOM)
    if (ctx.statusDot) {
      ctx.statusDot.className = "status-dot " + (st || "");
    }
    if (ctx.statusText) {
      let label = STATUS_LABELS[st] || st;
      if (st === "error" && msg) label += " — " + msg;
      ctx.statusText.textContent = label;
      ctx.statusText.className =
        "status-text" +
        (st === "error" ? " error" : "") +
        (st === "network_down" ? " network_down" : "");
    }

    // transition oneshots — до смены loop, чтобы connecting.exit не снёс DOM
    if (st === "running" && was === "starting" && registry.success) {
      play("success");
      activeAccentName = null; // success сам чистит connecting
      return;
    }
    if (st === "error" && was !== "error" && registry.error_enter) {
      play("error_enter");
    }

    // accent: loop по статусу
    const accentName = defaultStatusAccent[st];
    if (accentName) {
      // не перезапускаем тот же loop на каждый poll
      if (activeAccentName !== accentName) {
        play(accentName);
      }
    } else {
      stopAccent();
    }
  }

  function getStatus() {
    return currentStatus;
  }

  function mount(options) {
    ensureMounted(options || {});
    return api;
  }

  function setWindowVisible(visible) {
    if (visible) startIdle();
    else stopIdle();
  }

  /** Пересчёт точки привязки взгляда (после смещения куба). */
  function refreshAnchor() {
    // Позиция куба читается заново на каждом mousemove, поэтому достаточно
    // мягко вернуть взгляд в нейтраль — без рывка.
    resetLook();
  }

  function onToggle(cb) {
    toggleHandler = typeof cb === "function" ? cb : null;
  }

  const api = {
    mount: mount,
    setStatus: setStatus,
    getStatus: getStatus,
    play: play,
    stopAccent: stopAccent,
    registerAnimation: registerAnimation,
    setWindowVisible: setWindowVisible,
    refreshAnchor: refreshAnchor,
    onToggle: onToggle,
    /** @private debug */
    _registry: registry,
  };

  global.ObxodCube = api;
})(typeof window !== "undefined" ? window : globalThis);
