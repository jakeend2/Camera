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

  /* ---------------------------------------------------------- cameras --
   * One feed at a time. The selection drives the live image, the health
   * poll, every PTZ command and the archive links, and it is remembered so
   * a reload comes back where you were. Sections that need motors carry
   * data-needs="ptz" and are hidden for a camera that has none - hidden
   * rather than disabled, because a dead PTZ pad on a fixed camera is just
   * clutter that looks broken.
   */
  var feed = $("feed");

  var camButtons = Array.prototype.slice.call(
    document.querySelectorAll("[data-select-cam]"));
  var currentCam = app.dataset.cam || "";
  try {
    var remembered = localStorage.getItem("selectedCam");
    if (remembered && camButtons.some(function (b) {
          return b.dataset.selectCam === remembered; })) {
      currentCam = remembered;
    }
  } catch (e) {}

  function camInfo(cid) {
    var btn = camButtons.filter(function (b) {
      return b.dataset.selectCam === cid; })[0];
    return {
      cid: cid,
      ptz: !btn || btn.dataset.ptz === "1",   // single-camera page: assume yes
      aspect: (btn && btn.dataset.aspect) || "16/9",
      name: btn ? btn.textContent.trim() : cid
    };
  }

  // Append ?cam= to any path, preserving an existing query string.
  function withCam(path) {
    if (!currentCam) return path;
    return path + (path.indexOf("?") >= 0 ? "&" : "?") + "cam=" +
           encodeURIComponent(currentCam);
  }

  function applyCamera(cid, reloadFeed) {
    currentCam = cid;
    try { localStorage.setItem("selectedCam", cid); } catch (e) {}
    var info = camInfo(cid);

    camButtons.forEach(function (b) {
      var on = b.dataset.selectCam === cid;
      b.classList.toggle("active", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });

    // Motors decide what the rail shows.
    document.querySelectorAll("[data-needs=\"ptz\"]").forEach(function (el) {
      el.hidden = !info.ptz;
    });


    var frame = $("video-frame");
    if (frame) {
      frame.classList.toggle("ar-4x3", info.aspect === "4/3");
      frame.classList.toggle("ar-16x9", info.aspect !== "4/3");
    }
    var label = $("st-cam");
    if (label) label.textContent = info.name;

    ["link-watch", "link-files"].forEach(function (id) {
      var a = $(id);
      if (!a) return;
      a.href = withCam(a.href.split("?")[0]);
    });

    if (reloadFeed !== false && feed) {
      // Cache-bust so switching back to a camera restarts its stream rather
      // than reattaching to a response the browser considers finished.
      lastFrames = -1;
      var src = withCam("/camera");
      feed.src = src + (src.indexOf("?") >= 0 ? "&" : "?") +
                 "_=" + Date.now();
      var seg = $("st-link");
      if (seg) { seg.textContent = "LINK ·"; seg.classList.remove("bad"); }
    }
    poll();
  }

  camButtons.forEach(function (btn) {
    btn.addEventListener("click", function () {
      if (btn.dataset.selectCam !== currentCam) {
        applyCamera(btn.dataset.selectCam, true);
      }
    });
  });
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
    return fetch(withCam(path), {
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
    fetch(withCam("/health"), { credentials: "include" })
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
        if (h.preview_mode === "substream" && !h.preview_running) {
          var s = $("st-link");
          if (s) s.textContent = "LINK starting…";
        }
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

  // Apply the remembered camera once everything above is wired.
  if (camButtons.length) {
    applyCamera(currentCam, currentCam !== app.dataset.cam);
  }

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

  /* ------------------------------------------------------------ garage --
   * Separate from the cameras in every way: its own device, its own MQTT
   * identity, its own poll. The door buttons confirm first and state the
   * state they want, never toggling.
   */
  var garagePane = $("garage-pane");

  function paintGarage(g) {
    if (!garagePane || !g) return;
    var door = $("g-door");
    if (door) {
      door.textContent = g.online ? (g.door || "?") : "offline";
      door.classList.toggle("bad", !g.online || g.door === "Open");
    }
    var obst = $("g-obst");
    if (obst) {
      obst.textContent = g.obstructed ? "BLOCKED" : "clear";
      obst.classList.toggle("bad", !!g.obstructed);
    }
    if ($("g-light")) $("g-light").textContent = g.light ? "on" : "off";
    if ($("g-lock")) $("g-lock").textContent = g.locked ? "locked" : "unlocked";
    if ($("g-meta")) {
      $("g-meta").textContent = g.online
        ? (g.device || "garage") + " · " + (g.openings || 0) + " openings · " +
          (g.wifi_rssi || "")
        : "not responding";
    }
  }

  function garageSet(what, value) {
    return fetch("/garage/" + encodeURIComponent(what), {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ value: value })
    }).then(function (r) { return r.json(); })
      .then(function (j) {
        flash(j.ok ? (what + " " + value) : (j.error || "refused"), !j.ok);
        setTimeout(refreshGarage, 700);
        return j;
      })
      .catch(function () { flash("garage unreachable", true); });
  }

  function refreshGarage() {
    if (!garagePane) return;
    fetch("/garage", { credentials: "include" })
      .then(function (r) { return r.json(); })
      .then(paintGarage)
      .catch(function () { paintGarage({ online: false }); });
  }

  document.querySelectorAll("[data-garage]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      garageSet(btn.dataset.garage, btn.dataset.value);
    });
  });

  if ($("g-open")) $("g-open").addEventListener("click", function () {
    if (confirm("Open the garage door?")) garageSet("door", "open");
  });
  if ($("g-close")) $("g-close").addEventListener("click", function () {
    if (confirm("Close the garage door?\n\nMake sure nothing is in the way."))
      garageSet("door", "close");
  });

  if (garagePane) {
    refreshGarage();
    setInterval(refreshGarage, 5000);
  }

  /* ------------------------------------------------------------ climate --
   * The thermostat is a Z-Wave device behind a gateway, so every command is
   * a radio round-trip. Setpoint taps are therefore accumulated locally and
   * sent once the user stops adjusting: holding "+" five times should be one
   * command at the end, not five queued at the radio.
   */
  var hvacPane = $("hvac-pane");
  var hvacState = null;
  var pendingSp = { heat: null, cool: null };   // typed, not sent yet
  var sentSp = { heat: null, cool: null };      // sent, not confirmed yet

  /* What the user should see for a setpoint, most recent intent first. The
   * sent value has to outrank the reported one until a poll confirms it, or a
   * tap landing between dispatch and confirmation rebuilds from the old
   * number and quietly undoes the command just sent. */
  function spBase(which, h) {
    if (pendingSp[which] != null) return pendingSp[which];
    if (sentSp[which] != null) return sentSp[which];
    return h ? h["setpoint_" + which] : null;
  }
  var spTimer = null;

  function paintHvac(h) {
    if (!hvacPane || !h) return;
    hvacState = h;
    function put(id, text, bad) {
      var el = $(id);
      if (!el) return;
      el.textContent = text;
      el.classList.toggle("bad", !!bad);
    }
    var off = !h.online;
    // "not answering" and "nothing heard lately" are different faults and the
    // page says which. Anything else presents an hour-old temperature exactly
    // like a live one.
    var link = h.alive === false ? "not responding"
             : !h.fresh ? ("stale" + (h.age_s ? " " + Math.round(h.age_s / 60) + " min" : ""))
             : h.alive === true ? "ok" : "no report";
    put("h-link", link, off);
    // Fahrenheit when the gateway has told us the unit; the raw reading and
    // its unit when it has not. Never a number under an assumed scale.
    put("h-temp", h.temperature_f != null ? h.temperature_f + " °F"
                : h.temperature_raw != null
                  ? h.temperature_raw + " " + (h.temperature_unit || "?")
                  : "–", off);
    put("h-hum", h.humidity == null ? "–" : h.humidity + " %");
    put("h-mode", h.mode_label || "–");
    // Running is not a fault, so it gets its own colour rather than the red
    // that means offline or obstructed. The word says which; the colour only
    // separates heating from cooling.
    var opEl = $("h-op");
    if (opEl) {
      opEl.textContent = h.operating_label || "–";
      opEl.classList.remove("bad", "state-heat", "state-cool", "state-fan");
      if (h.running_kind) opEl.classList.add("state-" + h.running_kind);
    }
    put("h-fan", (h.fan_label || "–") + " / " + (h.fan_state_label || "–"));
    put("h-batt", h.battery == null ? "–" : h.battery + " %", h.battery != null && h.battery < 20);

    // A pending edit wins over the reported value, or the display would snap
    // back to the old number between taps.
    // A poll reporting the value we asked for retires the in-flight copy.
    ["heat", "cool"].forEach(function (w) {
      if (sentSp[w] != null && h["setpoint_" + w] === sentSp[w]) sentSp[w] = null;
    });
    var heat = spBase("heat", h);
    var cool = spBase("cool", h);
    put("h-sp-heat", heat == null ? "–" : heat + "°");
    put("h-sp-cool", cool == null ? "–" : cool + "°");
    $("h-sp-heat").classList.toggle(
      "pending", pendingSp.heat != null || sentSp.heat != null);
    $("h-sp-cool").classList.toggle(
      "pending", pendingSp.cool != null || sentSp.cool != null);

    document.querySelectorAll("[data-hvac=\"mode\"]").forEach(function (b) {
      b.classList.toggle("active", String(h.mode) === b.dataset.value);
    });
    document.querySelectorAll("[data-hvac=\"fan\"]").forEach(function (b) {
      b.classList.toggle("active", String(h.fan_mode) === b.dataset.value);
    });
  }

  function refreshHvac() {
    if (!hvacPane) return;
    fetch("/hvac", { credentials: "include" })
      .then(function (r) { return r.json(); })
      .then(paintHvac)
      .catch(function () { paintHvac({ online: false }); });
  }

  function sendPending() {
    var jobs = [];
    ["heat", "cool"].forEach(function (which) {
      if (pendingSp[which] == null) return;
      var value = pendingSp[which];
      pendingSp[which] = null;
      sentSp[which] = value;           // still ours until a poll confirms it
      jobs.push(fetch("/hvac/" + which, {
        method: "POST",
        credentials: "include",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ value: value })
      }).then(function (r) { return r.json(); })
        .then(function (res) {
          if (res.ok) {
            flash(which + " " + value + "\u00B0" + (res.note ? " - " + res.note : ""));
          } else {
            sentSp[which] = null;      // it did not take; show the truth again
            flash(res.error || "refused", true);
            paintHvac(hvacState);
          }
        })
        .catch(function () {
          sentSp[which] = null;
          flash("thermostat unreachable", true);
          paintHvac(hvacState);
        }));
    });
    if ($("h-pending")) $("h-pending").innerHTML = "&nbsp;";
    // allSettled, so one failed setpoint cannot cancel the refresh that would
    // have shown what the other one actually did.
    Promise.allSettled(jobs).then(function () { setTimeout(refreshHvac, 2500); });
  }

  function nudgeSetpoint(which, delta) {
    if (!hvacState) return;
    var base = spBase(which, hvacState);
    if (base == null) { flash("no setpoint reported yet", true); return; }
    var next = Math.round(base) + delta;
    var lo = hvacState.min_f || 45, hi = hvacState.max_f || 90;
    if (next < lo || next > hi) {
      flash("setpoint limit is " + lo + "-" + hi + "°", true);
      return;
    }
    pendingSp[which] = next;
    paintHvac(hvacState);
    if ($("h-pending")) {
      $("h-pending").textContent = "sending " + which + " " + next + "° shortly…";
    }
    clearTimeout(spTimer);
    spTimer = setTimeout(sendPending, 2000);
  }

  document.querySelectorAll("[data-sp]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      nudgeSetpoint(btn.dataset.sp, parseInt(btn.dataset.delta, 10));
    });
  });

  document.querySelectorAll("[data-hvac]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var what = btn.dataset.hvac, value = btn.dataset.value;
      // Changing mode can start or stop the whole system; the fan cannot.
      if (what === "mode") {
        var label = btn.textContent.trim();
        if (!confirm("Set the thermostat to " + label + "?")) return;
      }
      fetch("/hvac/" + what, {
        method: "POST",
        credentials: "include",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ value: value })
      }).then(function (r) { return r.json(); })
        .then(function (j) {
          flash(j.ok ? (what + " set") : (j.error || "refused"), !j.ok);
          setTimeout(refreshHvac, 2500);
        })
        .catch(function () { flash("thermostat unreachable", true); });
    });
  });

  if (hvacPane) {
    refreshHvac();
    setInterval(refreshHvac, 10000);
  }

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
    a.href = withCam("/snapshot");
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

/* Archive player.
 *
 * The page owns the day timeline; the <video> element is only a viewport
 * onto one bounded window of it. Seeking anywhere reloads the element from
 * /play at that second, which is why no media-source library is needed - the
 * server hands over an ordinary MP4 every time.
 *
 * Times shown are wall-clock, derived server-side from each file's length and
 * last write. They are good to a couple of seconds; the clock burned into the
 * picture is the authority, and the UI says so rather than implying otherwise.
 */
(function () {
  "use strict";

  var root = document.getElementById("watch");
  if (!root) return;

  var $ = function (id) { return document.getElementById(id); };
  var DAY_SECONDS = 86400;

  var day = root.dataset.day;
  var watchCam = root.dataset.cam || "";

  function camQ(path) {
    if (!watchCam) return path;
    return path + (path.indexOf("?") >= 0 ? "&" : "?") + "cam=" +
           encodeURIComponent(watchCam);
  }

  // Switching camera on the archive page reloads it: the timeline, the
  // segment list and every cached window belong to one camera, and
  // rebuilding all of that in place is more code than a navigation.
  document.querySelectorAll("#watch-cam-seg [data-select-cam]").forEach(
    function (btn) {
      btn.addEventListener("click", function () {
        var cid = btn.dataset.selectCam;
        if (cid === watchCam) return;
        window.location.href = "/watch/" + encodeURIComponent(day) +
                               "?cam=" + encodeURIComponent(cid);
      });
    });
  var data = null;            // last /timeline payload
  var windowStart = null;     // second-of-day the loaded window begins at
  var windowLength = 0;

  var player = $("player");
  var timeline = $("tl");

  // ------------------------------------------------------------- helpers --
  function pad2(n) { return String(n).padStart(2, "0"); }

  function hms(sec) {
    sec = Math.max(0, Math.round(sec));
    return pad2(Math.floor(sec / 3600)) + ":" +
           pad2(Math.floor(sec / 60) % 60) + ":" + pad2(sec % 60);
  }

  function parseHms(text) {
    var parts = String(text).trim().split(":").map(Number);
    if (parts.some(isNaN) || !parts.length || parts.length > 3) return null;
    while (parts.length < 3) parts.push(0);
    var sec = parts[0] * 3600 + parts[1] * 60 + parts[2];
    return sec >= 0 && sec <= DAY_SECONDS ? sec : null;
  }

  var flashTimer = null;
  function flash(msg, bad) {
    var el = $("w-flash");
    el.textContent = msg;
    el.classList.toggle("bad", !!bad);
    el.classList.add("show");
    clearTimeout(flashTimer);
    flashTimer = setTimeout(function () { el.classList.remove("show"); },
                            bad ? 5000 : 2000);
  }

  // Where the player currently is, in seconds-of-day.
  function position() {
    if (windowStart === null) return null;
    return windowStart + (player.currentTime || 0);
  }

  // ------------------------------------------------------------ timeline --
  function drawTimeline() {
    var track = $("tl-track");
    track.textContent = "";
    (data.segments || []).forEach(function (seg) {
      var el = document.createElement("div");
      el.className = "tl-seg";
      el.style.left = (seg.from / DAY_SECONDS * 100) + "%";
      el.style.width = Math.max(0.15, (seg.to - seg.from) / DAY_SECONDS * 100) + "%";
      el.title = hms(seg.from) + " – " + hms(seg.to) + "  (" + seg.name + ")";
      track.appendChild(el);
    });

    var ticks = $("tl-ticks");
    ticks.textContent = "";
    for (var h = 0; h <= 24; h += 3) {
      var tick = document.createElement("span");
      tick.className = "tl-tick";
      tick.style.left = (h / 24 * 100) + "%";
      tick.textContent = pad2(h) + ":00";
      ticks.appendChild(tick);
    }

    var covered = data.covered_seconds || 0;
    var gapCount = (data.gaps || []).length;
    $("tl-summary").textContent =
      (data.segments || []).length + " recording" +
      ((data.segments || []).length === 1 ? "" : "s") + " · " +
      hms(covered).slice(0, 5).replace(":", "h ") + "m of footage" +
      (gapCount ? " · " + gapCount + " gap" + (gapCount === 1 ? "" : "s") +
                  " where the recorder was down" : " · no gaps") +
      " · times ±2s (the clock in the picture is exact)";
  }

  function drawMarkers() {
    var from = parseHms($("clip-from").value);
    var to = parseHms($("clip-to").value);
    var sel = $("tl-sel");
    if (from === null || to === null || to <= from) { sel.hidden = true; return; }
    sel.hidden = false;
    sel.style.left = (from / DAY_SECONDS * 100) + "%";
    sel.style.width = ((to - from) / DAY_SECONDS * 100) + "%";
  }

  function drawCursor() {
    var at = position();
    var cur = $("tl-cursor");
    if (at === null) { cur.hidden = true; return; }
    cur.hidden = false;
    cur.style.left = (at / DAY_SECONDS * 100) + "%";
    timeline.setAttribute("aria-valuenow", Math.round(at));
    timeline.setAttribute("aria-valuetext", hms(at));
  }

  // ---------------------------------------------------------- load a day --
  function loadDay(which) {
    return fetch(camQ("/timeline?day=" + encodeURIComponent(which)),
                 { credentials: "include" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) { flash(j.error || "no such day", true); return; }
        day = which;
        data = j;
        root.dataset.day = which;
        $("w-day").textContent = which;

        var pick = $("day-pick");
        pick.textContent = "";
        (j.days || []).forEach(function (d) {
          var opt = document.createElement("option");
          opt.value = d;
          opt.textContent = d;
          opt.selected = d === which;
          pick.appendChild(opt);
        });

        drawTimeline();
        drawMarkers();
        windowStart = null;
        player.removeAttribute("src");
        player.load();
        $("w-pos").textContent = "--:--:--";
        $("w-seg").textContent = (j.segments || []).length
          ? "pick a moment on the timeline" : "nothing recorded this day";
      })
      .catch(function () { flash("could not load that day", true); });
  }

  // ------------------------------------------------------------ playback --
  /* The server cuts playback in fixed windows aligned to each recording's
   * start, so that scrubbing around one moment keeps hitting the same cut
   * instead of making a new one per position. The page works out the same
   * boundary from the segment list it already has - no extra round-trip, and
   * the displayed clock stays exact because the window start is known rather
   * than assumed. */
  function windowFor(second) {
    var segs = (data && data.segments) || [];
    var size = (data && data.window) || 120;
    for (var i = 0; i < segs.length; i++) {
      var s = segs[i];
      if (second >= s.from && second < s.to) {
        var aligned = Math.floor((second - s.from) / size) * size;
        return {
          start: s.from + aligned,
          length: Math.min(size, (s.to - s.from) - aligned),
          name: s.name
        };
      }
    }
    return null;
  }

  var pendingSeek = 0;

  function seekTo(second, autoplay) {
    if (!data) return;
    second = Math.max(0, Math.min(DAY_SECONDS - 1, second));

    var w = windowFor(second);
    if (!w) {
      // The timeline already says where the holes are, so skip without
      // asking the server and getting an error back.
      var next = null;
      (data.segments || []).forEach(function (s) {
        if (s.from > second && (next === null || s.from < next)) next = s.from;
      });
      if (next === null) {
        flash("no recording after " + hms(second), true);
        $("w-seg").textContent = "nothing further this day";
        return;
      }
      flash("gap — skipping to " + hms(next));
      seekTo(next + 0.5, autoplay);
      return;
    }

    // Already holding this window: move inside it, no request at all.
    if (windowStart === w.start && player.readyState >= 1) {
      player.currentTime = Math.max(0, second - w.start);
      if (autoplay !== false && player.paused) {
        var again = player.play();
        if (again && again.catch) again.catch(function () {});
      }
      $("w-pos").textContent = hms(second);
      drawCursor();
      return;
    }

    windowStart = w.start;
    windowLength = w.length;
    pendingSeek = second - w.start;
    $("w-seg").textContent = w.name;
    player.src = camQ("/play?day=" + encodeURIComponent(day) +
                      "&t=" + w.start.toFixed(1));
    player.load();
    if (autoplay !== false) {
      var go = player.play();
      if (go && go.catch) go.catch(function () {});
    }
    $("w-pos").textContent = hms(second);
    drawCursor();
  }

  /* Move by a small amount. Staying inside the window that is already loaded
   * is just a currentTime assignment - instant, and no request at all. Only
   * a jump past the loaded window costs a new one. */
  function nudge(delta) {
    var at = position();
    if (at === null) { flash("pick a moment first", true); return; }
    seekTo(Math.max(0, at + delta), !player.paused);
  }

  /* Gaps are handled before asking, from the timeline, so reaching here means
   * the window really failed. The <video> element cannot show a JSON body, so
   * ask the same URL again and report what the server actually said - a
   * read-only cache directory and a missing file should not look alike. */
  player.addEventListener("error", function () {
    if (windowStart === null) return;
    fetch(camQ("/play?day=" + encodeURIComponent(day) +
               "&t=" + windowStart.toFixed(1)),
          { credentials: "include" })
      .then(function (r) {
        if (r.ok) return null;                 // transient: it works now
        return r.json().catch(function () {
          return { error: "server said " + r.status };
        });
      })
      .then(function (j) {
        if (j) {
          flash(j.error || "could not load that window", true);
          $("w-seg").textContent = "playback failed";
        }
      })
      .catch(function () { flash("could not load that window", true); });
  });

  player.addEventListener("loadedmetadata", function () {
    if (isFinite(player.duration) && player.duration > 0) {
      windowLength = player.duration;
    }
    if (pendingSeek > 0.2 && pendingSeek < windowLength) {
      player.currentTime = pendingSeek;    // land on the moment asked for
    }
    pendingSeek = 0;
    drawCursor();
  });

  player.addEventListener("timeupdate", function () {
    var at = position();
    if (at === null) return;
    $("w-pos").textContent = hms(at);
    drawCursor();
  });

  // Window over: roll straight into the next one so playback continues.
  player.addEventListener("ended", function () {
    if (windowStart === null) return;
    var next = windowStart + (windowLength || 0);
    if (next >= DAY_SECONDS) return;
    seekTo(next, true);
  });

  // -------------------------------------------------------- interactions --
  function secondFromEvent(ev) {
    var r = timeline.getBoundingClientRect();
    var x = (ev.clientX - r.left) / r.width;
    return Math.max(0, Math.min(1, x)) * DAY_SECONDS;
  }

  timeline.addEventListener("click", function (ev) {
    seekTo(secondFromEvent(ev), true);
  });

  timeline.addEventListener("keydown", function (ev) {
    var step = ev.shiftKey ? 600 : 60;
    var at = position();
    if (at === null) at = 0;
    if (ev.key === "ArrowRight") { ev.preventDefault(); seekTo(at + step, true); }
    if (ev.key === "ArrowLeft") { ev.preventDefault(); seekTo(at - step, true); }
  });

  document.querySelectorAll("[data-seek]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      nudge(parseInt(btn.dataset.seek, 10));
    });
  });

  $("day-prev").addEventListener("click", function () { stepDay(-1); });
  $("day-next").addEventListener("click", function () { stepDay(1); });
  $("day-pick").addEventListener("change", function () { loadDay(this.value); });

  function stepDay(delta) {
    var parts = day.split("-").map(Number);
    var d = new Date(parts[0], parts[1] - 1, parts[2]);
    d.setDate(d.getDate() + delta);
    loadDay(d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()));
  }

  // ----------------------------------------------------------- clip marks --
  $("mark-in").addEventListener("click", function () {
    var at = position();
    if (at === null) { flash("play something first", true); return; }
    $("clip-from").value = hms(at);
    drawMarkers();
  });

  $("mark-out").addEventListener("click", function () {
    var at = position();
    if (at === null) { flash("play something first", true); return; }
    $("clip-to").value = hms(at);
    drawMarkers();
  });

  document.querySelectorAll("[data-nudge]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var from = parseHms($("clip-from").value);
      if (from === null) { flash("start time is not a time", true); return; }
      $("clip-to").value = hms(from + parseInt(btn.dataset.nudge, 10));
      drawMarkers();
    });
  });

  ["clip-from", "clip-to"].forEach(function (id) {
    $(id).addEventListener("change", drawMarkers);
  });

  $("clip-preview").addEventListener("click", function () {
    var from = parseHms($("clip-from").value);
    if (from === null) { flash("start time is not a time", true); return; }
    seekTo(from, true);
  });

  $("clip-go").addEventListener("click", function () {
    var from = parseHms($("clip-from").value);
    var to = parseHms($("clip-to").value);
    if (from === null || to === null) { flash("times must be HH:MM:SS", true); return; }
    if (to <= from) { flash("end must be after start", true); return; }
    var max = (data && data.clip_max) || 1800;
    if (to - from > max) {
      flash("longest clip is " + Math.round(max / 60) + " minutes", true);
      return;
    }
    var btn = this;
    btn.disabled = true;
    var was = btn.textContent;
    btn.textContent = "CUTTING…";
    flash("cutting " + hms(to - from) + "…");

    var url = camQ("/clip?day=" + encodeURIComponent(day) +
                   "&from=" + from + "&to=" + to);
    // Fetched rather than linked so a failure surfaces as a message instead
    // of replacing the page with JSON.
    fetch(url, { credentials: "include" })
      .then(function (r) {
        if (!r.ok) {
          return r.json().then(function (j) { throw new Error(j.error || "failed"); });
        }
        var missing = parseFloat(r.headers.get("X-Missing-Seconds") || "0");
        return r.blob().then(function (blob) { return { blob: blob, missing: missing }; });
      })
      .then(function (res) {
        var href = URL.createObjectURL(res.blob);
        var a = document.createElement("a");
        a.href = href;
        a.download = (watchCam || "cam") + "-" + day + "-" +
                   hms(from).replace(/:/g, "") + ".mp4";
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(function () { URL.revokeObjectURL(href); }, 30000);
        flash(res.missing > 1
              ? "saved — " + Math.round(res.missing) + "s missing (recorder was down)"
              : "clip saved");
      })
      .catch(function (err) { flash(err.message || "clip failed", true); })
      .finally(function () { btn.disabled = false; btn.textContent = was; });
  });

  loadDay(day);
})();
