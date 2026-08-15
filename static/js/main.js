/* Camera control behaviour. Loaded on every page; each feature attaches only
 * if its markup is present, so the login and archive pages share this file.
 *
 * Interaction rules that must not regress:
 *  - All press-and-hold uses POINTER events (mousedown never fires during a
 *    touch hold), with pointer capture, and stop is failsafed on pointerup,
 *    pointercancel, lostpointercapture, window blur and page-hidden.
 *  - While anything is held, the current motion command is re-sent every 4 s:
 *    Pelco receivers may implement a ~15 s runaway-protect motion timeout
 *    (unconfirmed on this camera; the resend is harmless if absent).
 *  - Every response is judged by its JSON "ok" field, never the HTTP status -
 *    PTZ routes return 200 with ok:false on serial failure.
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  // ---------------------------------------------------------------- clock --
  var clockEls = [$("clock"), $("strip-clock")].filter(Boolean);
  if (clockEls.length) {
    var pad2 = function (n) { return String(n).padStart(2, "0"); };
    var tick = function () {
      var d = new Date();
      var date = d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
      var time = pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
      clockEls.forEach(function (el) {
        el.textContent = el.id === "clock" ? date + " " + time : time;
      });
    };
    tick();
    setInterval(tick, 1000);
  }

  var app = $("app");
  if (!app) return;                       // login / archive page: clock only

  var DIAGONALS = app.dataset.diagonals === "1";
  var speed = parseInt(localStorage.getItem("ptzSpeed"), 10)
              || parseInt(app.dataset.defaultSpeed, 10) || 25;

  // ------------------------------------------------------- status + fetch --
  var flashEl = $("st-flash");
  var flashTimer = null;
  function flash(msg, bad) {
    if (!flashEl) return;
    flashEl.textContent = msg;
    flashEl.classList.toggle("bad", !!bad);
    flashEl.classList.add("show");
    clearTimeout(flashTimer);
    flashTimer = setTimeout(function () { flashEl.classList.remove("show"); },
                            bad ? 4000 : 1200);
  }

  function postCmd(path, body) {
    return fetch(path, {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) flash(j.error || "command failed", true);
        return j;
      })
      .catch(function () { flash("no response", true); return { ok: false }; });
  }

  // ------------------------------------------------------ speed segmented --
  var spdLabel = $("st-spd");
  function setSpeed(v, btn) {
    speed = v;
    localStorage.setItem("ptzSpeed", String(v));
    if (spdLabel) spdLabel.textContent = "SPD " + v;
    document.querySelectorAll("#speed-seg button").forEach(function (b) {
      b.classList.toggle("active", b === btn);
    });
  }
  document.querySelectorAll("#speed-seg button").forEach(function (b) {
    b.addEventListener("click", function () {
      setSpeed(parseInt(b.dataset.speed, 10), b);
    });
    if (parseInt(b.dataset.speed, 10) === speed) setSpeed(speed, b);
  });
  if (spdLabel) spdLabel.textContent = "SPD " + speed;

  // ------------------------------------------- shared motion keepalive ----
  var keepalive = { timer: null, send: null };
  function startKeepalive(sendFn) {
    stopKeepalive();
    keepalive.send = sendFn;
    keepalive.timer = setInterval(function () {
      if (keepalive.send) keepalive.send();
    }, 4000);
  }
  function stopKeepalive() {
    clearInterval(keepalive.timer);
    keepalive.timer = null;
    keepalive.send = null;
  }

  // ------------------------------------------------- press-and-hold pills --
  var HOLD_ENDPOINTS = {
    pan_left: "/pan_left", pan_right: "/pan_right",
    tilt_up: "/tilt_up", tilt_down: "/tilt_down",
    zoom_tele: "/zoom_tele", zoom_wide: "/zoom_wide",
    focus_near: "/focus_near", focus_far: "/focus_far",
    iris_open: "/iris_open", iris_close: "/iris_close"
  };

  var holding = null;

  function beginHold(btn, ev) {
    ev.preventDefault();
    if (holding) return;
    holding = btn;
    btn.classList.add("is-held");
    try { btn.setPointerCapture(ev.pointerId); } catch (e) {}
    var path = HOLD_ENDPOINTS[btn.dataset.hold];
    var send = function () { postCmd(path, { speed: speed }); };
    send();
    startKeepalive(send);
  }

  function endHold() {
    if (!holding) return;
    holding.classList.remove("is-held");
    holding = null;
    stopKeepalive();
    postCmd("/stop");
  }

  document.querySelectorAll("[data-hold]").forEach(function (btn) {
    btn.addEventListener("pointerdown", function (ev) { beginHold(btn, ev); });
    btn.addEventListener("pointerup", endHold);
    btn.addEventListener("pointercancel", endHold);
    btn.addEventListener("lostpointercapture", endHold);
    btn.addEventListener("contextmenu", function (ev) { ev.preventDefault(); });
  });

  // --------------------------------------------------------------- D-pad --
  var pad = $("dpad");
  var padActive = false;
  var padVec = null;                       // current {pan, tilt} or null

  function arrowEls(vec) {
    var out = [];
    if (!vec) return out;
    if (vec.tilt > 0) out.push($("arr-up"));
    if (vec.tilt < 0) out.push($("arr-down"));
    if (vec.pan < 0) out.push($("arr-left"));
    if (vec.pan > 0) out.push($("arr-right"));
    return out;
  }

  function paintPad(vec, stopped) {
    document.querySelectorAll(".dpad-arrow").forEach(function (a) {
      a.classList.remove("on");
    });
    arrowEls(vec).forEach(function (a) { if (a) a.classList.add("on"); });
    var stop = $("dpad-stop");
    if (stop) stop.classList.toggle("on", !!stopped);
  }

  function sendMove(vec) {
    postCmd("/move", { pan: vec.pan, tilt: vec.tilt,
                       panSpeed: speed, tiltSpeed: speed });
  }

  function padStop() {
    if (padVec !== null || padActive) {
      padVec = null;
      stopKeepalive();
      postCmd("/stop");
    }
    paintPad(null, padActive);
  }

  function vectorFrom(ev) {
    var r = pad.getBoundingClientRect();
    var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    var dx = ev.clientX - cx, dy = ev.clientY - cy;
    var radius = Math.hypot(dx, dy) / (r.width / 2);
    if (radius < 0.28) return null;                    // dead zone = stop
    var ang = Math.atan2(-dy, dx) * 180 / Math.PI;     // 0 = right, CCW
    if (ang < 0) ang += 360;
    if (DIAGONALS) {
      var oct = Math.round(ang / 45) % 8;
      return [
        { pan: 1, tilt: 0 }, { pan: 1, tilt: 1 }, { pan: 0, tilt: 1 },
        { pan: -1, tilt: 1 }, { pan: -1, tilt: 0 }, { pan: -1, tilt: -1 },
        { pan: 0, tilt: -1 }, { pan: 1, tilt: -1 }
      ][oct];
    }
    var quad = Math.round(ang / 90) % 4;
    return [
      { pan: 1, tilt: 0 }, { pan: 0, tilt: 1 },
      { pan: -1, tilt: 0 }, { pan: 0, tilt: -1 }
    ][quad];
  }

  function padUpdate(ev) {
    var vec = vectorFrom(ev);
    if (vec === null) { padStop(); return; }
    if (!padVec || vec.pan !== padVec.pan || vec.tilt !== padVec.tilt) {
      padVec = vec;
      sendMove(vec);
      startKeepalive(function () { sendMove(vec); });
    }
    paintPad(vec, false);
  }

  function padRelease() {
    if (!padActive) return;
    padActive = false;
    padVec = null;
    stopKeepalive();
    postCmd("/stop");
    paintPad(null, false);
  }

  if (pad) {
    pad.addEventListener("pointerdown", function (ev) {
      ev.preventDefault();
      padActive = true;
      try { pad.setPointerCapture(ev.pointerId); } catch (e) {}
      padUpdate(ev);
    });
    pad.addEventListener("pointermove", function (ev) {
      if (padActive) padUpdate(ev);
    });
    ["pointerup", "pointercancel", "lostpointercapture"].forEach(function (t) {
      pad.addEventListener(t, padRelease);
    });
    pad.addEventListener("contextmenu", function (ev) { ev.preventDefault(); });

    // Keyboard: arrows move while held, anything else stops.
    var KEYVEC = {
      ArrowUp: { pan: 0, tilt: 1 }, ArrowDown: { pan: 0, tilt: -1 },
      ArrowLeft: { pan: -1, tilt: 0 }, ArrowRight: { pan: 1, tilt: 0 }
    };
    pad.addEventListener("keydown", function (ev) {
      var vec = KEYVEC[ev.key];
      if (!vec) return;
      ev.preventDefault();
      if (ev.repeat) return;
      padVec = vec;
      sendMove(vec);
      startKeepalive(function () { sendMove(vec); });
      paintPad(vec, false);
    });
    pad.addEventListener("keyup", function (ev) {
      if (KEYVEC[ev.key]) { ev.preventDefault(); padStop(); }
    });
  }

  // Global failsafes: any release anywhere, or the page going away, stops.
  window.addEventListener("pointerup", function () { endHold(); padRelease(); });
  window.addEventListener("blur", function () { endHold(); padRelease(); });
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { endHold(); padRelease(); }
  });

  // ------------------------------------------------------- tap commands ---
  document.querySelectorAll("[data-cmd]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      postCmd(btn.dataset.cmd).then(function (j) {
        if (j.ok) flash("OK");
      });
    });
  });

  // ------------------------------------------------------------- presets --
  var presetInput = $("preset-num");
  function presetNumber() {
    var n = parseInt(presetInput && presetInput.value, 10);
    if (isNaN(n) || n < 1 || n > 79 || n === 33 || n === 34 || n === 62) {
      flash("presets: 1-79, not 33/34/62", true);
      return null;
    }
    return n;
  }
  document.querySelectorAll("[data-goto]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      postCmd("/Goto_preset", { preset: parseInt(btn.dataset.goto, 10) })
        .then(function (j) { if (j.ok) flash("preset " + btn.dataset.goto); });
    });
  });
  if ($("preset-go")) $("preset-go").addEventListener("click", function () {
    var n = presetNumber();
    if (n) postCmd("/Goto_preset", { preset: n })
      .then(function (j) { if (j.ok) flash("preset " + n); });
  });
  if ($("preset-set")) $("preset-set").addEventListener("click", function () {
    var n = presetNumber();
    if (n && confirm("Store the current position as preset " + n + "?")) {
      postCmd("/Set_preset", { preset: n })
        .then(function (j) { if (j.ok) flash("stored " + n); });
    }
  });

  // ------------------------------------------------------------- imager ---
  function markImager(state) {
    var vis = $("btn-vis"), th = $("btn-therm"), st = $("st-img");
    if (vis) vis.setAttribute("aria-pressed", state === "visible");
    if (vis) vis.classList.toggle("active", state === "visible");
    if (th) th.setAttribute("aria-pressed", state === "thermal");
    if (th) th.classList.toggle("active", state === "thermal");
    if (st) st.textContent =
      "IMG:" + (state === "visible" ? "VIS" : state === "thermal" ? "THERM" : "?");
  }

  // -------------------------------------------------------- health poll ---
  var lastFrames = -1;
  function poll() {
    fetch("/health", { credentials: "include" })
      .then(function (r) { return r.json(); })
      .then(function (h) {
        var rec = $("rec-dot");
        if (rec) rec.classList.toggle("live", !!h.recording);
        var link = $("st-link");
        if (link) {
          var moving = lastFrames >= 0 && h.preview_frames > lastFrames;
          var first = lastFrames < 0;
          link.textContent = first ? "LINK ·" : (moving ? "LINK OK" : "LINK STALL");
          link.classList.toggle("bad", !first && !moving);
          lastFrames = h.preview_frames;
        }
        markImager(h.imager || "unknown");
        if ($("sys-file")) $("sys-file").textContent = h.current_file || "-";
        if ($("sys-size")) $("sys-size").textContent = h.current_size_gb + " GB";
        if ($("sys-archive")) $("sys-archive").textContent = h.archive_gb + " GB";
        if ($("sys-free")) $("sys-free").textContent = h.free_gb + " GB";
      })
      .catch(function () {
        var link = $("st-link");
        if (link) { link.textContent = "LINK ?"; link.classList.add("bad"); }
      });
  }
  poll();
  setInterval(poll, 15000);

  // ---------------------------------------------------------------- tabs --
  document.querySelectorAll("#tabs button").forEach(function (btn) {
    btn.addEventListener("click", function () {
      document.querySelectorAll("#tabs button").forEach(function (b) {
        b.classList.toggle("active", b === btn);
      });
      document.querySelectorAll(".tabpane").forEach(function (p) {
        p.classList.toggle("active", p.dataset.pane === btn.dataset.tab);
      });
    });
  });

  // ---------------------------------------------------------- fullscreen --
  var frame = $("video-frame");
  if ($("fs-btn") && frame) {
    $("fs-btn").addEventListener("click", function () {
      if (document.fullscreenElement) {
        document.exitFullscreen();
      } else if (frame.requestFullscreen) {
        frame.requestFullscreen();
      } else {
        frame.classList.toggle("pseudo-fs");   // fallback (old iOS Safari)
      }
    });
  }

  // ------------------------------------------------------------ OSD mode --
  var osdOn = false;
  if ($("btn-osd")) $("btn-osd").addEventListener("click", function () {
    if (!osdOn) {
      postCmd("/OSD_menu").then(function (j) {
        if (!j.ok) return;
        osdOn = true;
        $("btn-osd").classList.add("active");
        if ($("osd-banner")) $("osd-banner").hidden = false;
        if ($("btn-osd-select")) $("btn-osd-select").hidden = false;
        flash("OSD open");
      });
    } else {
      osdOn = false;
      $("btn-osd").classList.remove("active");
      if ($("osd-banner")) $("osd-banner").hidden = true;
      if ($("btn-osd-select")) $("btn-osd-select").hidden = true;
    }
  });
  if ($("btn-osd-select")) $("btn-osd-select").addEventListener("click", function () {
    postCmd("/iris_open").then(function (j) { if (j.ok) flash("SELECT"); });
  });

  // ------------------------------------------------------------- system ---
  if ($("btn-snap")) $("btn-snap").addEventListener("click", function () {
    var a = document.createElement("a");
    a.href = "/snapshot";
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
    flash("snapshot");
  });
  if ($("btn-restart")) $("btn-restart").addEventListener("click", function () {
    if (confirm("Restart the camera service? The recording is interrupted " +
                "for a few seconds and the stream will reconnect.")) {
      postCmd("/Exit_program").then(function () { flash("restarting…"); });
    }
  });
})();
