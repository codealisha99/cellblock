// Replays the recorded session and draws the latency comparison from the JSON written by
// scripts/record_session.py. Nothing here calls a server; the page has no backend.
(function () {
  "use strict";

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var MAX_GAP_MS = 600; // long waits in the recording are shortened so the replay stays watchable
  var TYPE_MS_PER_CHAR = 12;
  var MAX_TYPE_MS = 900;
  var SCALE_MAX_MS = 400; // shared axis for both latency rows, so the gap is drawn at its real size
  var STEP_MS = 50;

  // Section markers: each jumps to the first note line matching its pattern.
  var MARKERS = {
    sdk: /^SDK:/,
    fork: /fork bomb/,
    oom: /^memory:/,
    net: /network is disabled/,
    fs: /write outside/,
    snap: /snapshot \/workspace/
  };

  var body = document.getElementById("term-body");
  var playButton = document.getElementById("replay");
  var speedButton = document.getElementById("speed");
  var markerButtons = document.querySelectorAll("[data-jump]");
  var replayButtons = document.querySelectorAll("[data-replay]");

  var state = { run: 0, paused: false, speed: 1, lines: [], playing: false };

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  // Waits game-time ms, honouring pause and speed. Returns false if this run was superseded.
  async function wait(ms, id) {
    var left = ms;
    while (left > 0) {
      if (id !== state.run) return false;
      if (state.paused) { await sleep(STEP_MS); continue; }
      var step = Math.min(STEP_MS, left);
      await sleep(step / state.speed);
      left -= step;
    }
    return id === state.run;
  }

  function isPython(text) {
    return /^\s|^(with |sb|template|import |try:|except )/.test(text);
  }

  function lineEl(cls) {
    var el = document.createElement("span");
    el.className = "line" + (cls ? " " + cls : "");
    body.appendChild(el);
    return el;
  }

  function stripNewline(text) {
    return text.replace(/\r?\n$/, "");
  }

  function promptFor(text) {
    var python = isPython(text);
    var prompt = document.createElement("span");
    prompt.className = "prompt";
    prompt.textContent = python ? ">>> " : "$ ";
    return { prompt: prompt, python: python };
  }

  async function typeCommand(text, id) {
    var el = lineEl("");
    var parts = promptFor(text);
    var typed = document.createElement("span");
    if (parts.python) typed.className = "py";
    el.appendChild(parts.prompt);
    el.appendChild(typed);
    var perChar = Math.min(TYPE_MS_PER_CHAR, MAX_TYPE_MS / Math.max(text.length, 1));
    for (var i = 0; i < text.length; i++) {
      typed.textContent += text.charAt(i);
      if (i % 3 === 0 && !(await wait(perChar * 3, id))) return false;
    }
    return true;
  }

  function outputLine(entry) {
    var text = stripNewline(entry.text);
    lineEl(entry.kind === "stderr" ? "stderr" : "").textContent = text === "" ? " " : text;
  }

  function exitLine(entry) {
    var label = "exit " + entry.code + (entry.timed_out ? " (timed out)" : "");
    lineEl(entry.code === 0 ? "exit" : "exit bad").textContent = "# " + label;
  }

  function noteLine(entry) {
    lineEl("note").textContent = "# " + entry.text;
  }

  // Writes one entry with no delay. Used for lines before a jump point and for reduced motion.
  function renderEntry(entry) {
    if (entry.kind === "cmd") {
      var el = lineEl("");
      var parts = promptFor(entry.text);
      var typed = document.createElement("span");
      if (parts.python) typed.className = "py";
      typed.textContent = entry.text;
      el.appendChild(parts.prompt);
      el.appendChild(typed);
    } else if (entry.kind === "exit") {
      exitLine(entry);
    } else if (entry.kind === "note") {
      noteLine(entry);
    } else {
      outputLine(entry);
    }
  }

  function setControls(playing) {
    state.playing = playing;
    playButton.disabled = false;
    playButton.textContent = playing ? (state.paused ? "Resume" : "Pause") : "Replay";
    playButton.setAttribute("aria-label", playing ? (state.paused ? "Resume replay" : "Pause replay") : "Replay");
  }

  function markerEntries(lines) {
    var found = {};
    Object.keys(MARKERS).forEach(function (key) {
      for (var i = 0; i < lines.length; i++) {
        if (lines[i].kind === "note" && MARKERS[key].test(lines[i].text)) {
          found[key] = i;
          break;
        }
      }
    });
    return found;
  }

  // Plays from index `from`, first drawing everything before it so a jump keeps the earlier context.
  async function play(from) {
    var id = ++state.run;
    var lines = state.lines;
    body.textContent = "";
    state.paused = false;
    for (var k = 0; k < from; k++) renderEntry(lines[k]);
    setControls(true);
    var previous = from > 0 ? lines[from - 1].at_ms : (lines[0] ? lines[0].at_ms : 0);
    for (var i = from; i < lines.length; i++) {
      var entry = lines[i];
      var gap = Math.min(Math.max(entry.at_ms - previous, 0), MAX_GAP_MS);
      previous = entry.at_ms;
      if (!(await wait(gap, id))) return;
      if (entry.kind === "cmd") {
        if (!(await typeCommand(entry.text, id))) return;
      } else {
        renderEntry(entry);
      }
      body.scrollTop = body.scrollHeight;
    }
    if (id === state.run) setControls(false);
  }

  function jumpTo(key) {
    var index = state.markers[key];
    if (index === undefined) return;
    markerButtons.forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.jump === key)); });
    play(index);
  }

  function togglePlay() {
    if (!state.playing) { play(0); return; }
    state.paused = !state.paused;
    setControls(true);
  }

  function setSpeed() {
    state.speed = state.speed === 1 ? 2 : 1;
    speedButton.textContent = state.speed + "x";
  }

  function renderLatency(data) {
    var chart = document.getElementById("chart");
    var caption = document.getElementById("latency-caption");
    caption.textContent = data.samples + " samples per path, client-measured around the SDK call, Docker " +
      data.docker + " on an M-series Mac. Warm is a database update, so it leaves out the first exec and the pool refill.";

    var ratio = Math.round(data.cold_median_ms / data.warm_median_ms);
    var summary = document.createElement("div");
    summary.className = "latency-summary";
    summary.innerHTML =
      '<div class="big cold"><span class="num">' + data.cold_median_ms + '</span><span class="unit">ms</span>' +
      '<span class="cap">cold create, median</span></div>' +
      '<div class="ratio" aria-hidden="true">' + ratio + '&times;</div>' +
      '<div class="big warm"><span class="num">' + data.warm_median_ms + '</span><span class="unit">ms</span>' +
      '<span class="cap">warm create, median</span></div>';
    chart.appendChild(summary);

    var strip = document.createElement("div");
    strip.className = "strip";
    strip.setAttribute("role", "img");
    strip.setAttribute("aria-label", "Dot plot of " + data.samples + " samples per path on a 0 to 400 ms axis. " +
      "Cold: median " + data.cold_median_ms + " ms, range " + rangeText(data.cold_ms) + ". " +
      "Warm: median " + data.warm_median_ms + " ms, range " + rangeText(data.warm_ms) + ".");

    strip.appendChild(buildRow("cold", "Cold create", data.cold_ms, data.cold_median_ms));
    strip.appendChild(buildRow("warm", "Warm create", data.warm_ms, data.warm_median_ms));
    strip.appendChild(buildAxis());
    chart.appendChild(strip);

    var note = document.createElement("p");
    note.className = "strip-note";
    note.textContent = "Both rows share one axis. The warm samples sit between " + Math.min.apply(null, data.warm_ms) +
      " and " + Math.max.apply(null, data.warm_ms) + " ms, so they pile up at the origin.";
    chart.appendChild(note);
  }

  function rangeText(xs) {
    return Math.min.apply(null, xs) + " to " + Math.max.apply(null, xs) + " ms";
  }

  function pct(ms) {
    return (ms / SCALE_MAX_MS * 100) + "%";
  }

  function buildRow(kind, title, samples, median) {
    var row = document.createElement("div");
    row.className = "strip-row " + kind;

    var label = document.createElement("div");
    label.className = "strip-label";
    label.innerHTML = "<strong></strong><span></span>";
    label.querySelector("strong").textContent = title;
    label.querySelector("span").textContent = "range " + rangeText(samples);
    row.appendChild(label);

    var track = document.createElement("div");
    track.className = "track";
    track.setAttribute("aria-hidden", "true");
    var band = document.createElement("span");
    band.className = "band";
    band.style.left = pct(Math.min.apply(null, samples));
    band.style.width = (Math.max.apply(null, samples) - Math.min.apply(null, samples)) / SCALE_MAX_MS * 100 + "%";
    track.appendChild(band);
    samples.forEach(function (ms) {
      var dot = document.createElement("span");
      dot.className = "dot";
      dot.style.left = pct(ms);
      track.appendChild(dot);
    });
    var med = document.createElement("span");
    med.className = "median";
    med.style.left = pct(median);
    track.appendChild(med);
    row.appendChild(track);
    return row;
  }

  function buildAxis() {
    var axisRow = document.createElement("div");
    axisRow.className = "strip-axis";
    var spacer = document.createElement("div");
    axisRow.appendChild(spacer);
    var ticks = document.createElement("div");
    ticks.className = "ticks-axis";
    [0, 100, 200, 300, 400].forEach(function (ms) {
      var span = document.createElement("span");
      span.textContent = ms + " ms";
      span.style.left = pct(ms);
      ticks.appendChild(span);
    });
    axisRow.appendChild(ticks);
    return axisRow;
  }

  function enable(lines) {
    state.lines = lines;
    state.markers = markerEntries(lines);
    playButton.disabled = false;
    speedButton.disabled = false;
    markerButtons.forEach(function (b) {
      var key = b.dataset.jump;
      b.disabled = state.markers[key] === undefined;
    });
  }

  function showFailure(message) {
    body.textContent = message;
  }

  Promise.all([
    fetch("data/session.json").then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }),
    fetch("data/latency.json").then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
  ]).then(function (results) {
    var session = results[0];
    renderLatency(results[1]);
    enable(session.lines);

    playButton.addEventListener("click", togglePlay);
    speedButton.addEventListener("click", setSpeed);
    replayButtons.forEach(function (b) {
      b.addEventListener("click", function () { play(0); });
    });
    markerButtons.forEach(function (b) {
      b.addEventListener("click", function () { jumpTo(b.dataset.jump); });
    });

    if (reduceMotion) {
      body.textContent = "";
      state.lines.forEach(renderEntry);
      setControls(false);
      return;
    }
    var started = false;
    var observer = new IntersectionObserver(function (entries) {
      if (started || !entries[0].isIntersecting) return;
      started = true;
      observer.disconnect();
      play(0);
    }, { threshold: 0.35 });
    observer.observe(document.getElementById("terminal"));
  }).catch(function () {
    showFailure("The recording could not be loaded. Run scripts/record_session.py to regenerate site/data/.");
    document.getElementById("latency-caption").textContent = "Latency data could not be loaded.";
  });
})();
