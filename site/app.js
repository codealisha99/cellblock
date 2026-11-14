// Replays the recorded session and draws the latency range chart from the JSON written by
// scripts/record_session.py. Nothing here calls a server; the page has no backend.
(function () {
  "use strict";

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var MAX_GAP_MS = 600; // long waits in the recording are shortened so the replay stays watchable
  var TYPE_MS_PER_CHAR = 12;
  var MAX_TYPE_MS = 900;

  var body = document.getElementById("term-body");
  var button = document.getElementById("replay");
  var run = 0;

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
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

  async function typeCommand(text) {
    var python = isPython(text);
    var el = lineEl("");
    var prompt = document.createElement("span");
    prompt.className = "prompt";
    prompt.textContent = python ? ">>> " : "$ ";
    var typed = document.createElement("span");
    if (python) typed.className = "py";
    el.appendChild(prompt);
    el.appendChild(typed);
    var perChar = Math.min(TYPE_MS_PER_CHAR, MAX_TYPE_MS / Math.max(text.length, 1));
    for (var i = 0; i < text.length; i++) {
      typed.textContent += text.charAt(i);
      if (i % 3 === 0) await sleep(perChar * 3);
    }
  }

  function outputLine(entry) {
    var text = stripNewline(entry.text);
    var el = lineEl(entry.kind === "stderr" ? "stderr" : "");
    el.textContent = text === "" ? " " : text;
  }

  function exitLine(entry) {
    var label = "exit " + entry.code + (entry.timed_out ? " (timed out)" : "");
    lineEl("exit").textContent = "# " + label;
  }

  function noteLine(entry) {
    lineEl("note").textContent = "# " + entry.text;
  }

  function renderInstant(lines) {
    body.textContent = "";
    lines.forEach(function (entry) {
      if (entry.kind === "cmd") {
        var el = lineEl("");
        var python = isPython(entry.text);
        var prompt = document.createElement("span");
        prompt.className = "prompt";
        prompt.textContent = python ? ">>> " : "$ ";
        var typed = document.createElement("span");
        if (python) typed.className = "py";
        typed.textContent = entry.text;
        el.appendChild(prompt);
        el.appendChild(typed);
      } else if (entry.kind === "exit") {
        exitLine(entry);
      } else if (entry.kind === "note") {
        noteLine(entry);
      } else {
        outputLine(entry);
      }
    });
    body.scrollTop = body.scrollHeight;
  }

  async function play(lines) {
    var id = ++run;
    body.textContent = "";
    button.disabled = true;
    var previous = lines.length ? lines[0].at_ms : 0;
    for (var i = 0; i < lines.length; i++) {
      if (id !== run) return;
      var entry = lines[i];
      var gap = Math.min(Math.max(entry.at_ms - previous, 0), MAX_GAP_MS);
      previous = entry.at_ms;
      if (gap > 0) await sleep(gap);
      if (id !== run) return;
      if (entry.kind === "cmd") await typeCommand(entry.text);
      else if (entry.kind === "exit") exitLine(entry);
      else if (entry.kind === "note") noteLine(entry);
      else outputLine(entry);
      body.scrollTop = body.scrollHeight;
    }
    button.disabled = false;
  }

  function renderLatency(data) {
    var chart = document.getElementById("chart");
    var caption = document.getElementById("latency-caption");
    var scaleMax = 400; // ms; fixed so the two rows are comparable
    caption.textContent = data.samples + " samples per path, client-measured around the SDK call, Docker " +
      data.docker + " on an M-series Mac. Warm is a database update, so it leaves out the first exec and the pool refill.";

    [["cold", data.cold_ms, data.cold_median_ms, ""], ["warm", data.warm_ms, data.warm_median_ms, " warm"]]
      .forEach(function (row) {
        var xs = row[1];
        var min = Math.min.apply(null, xs);
        var max = Math.max.apply(null, xs);
        var wrap = document.createElement("div");
        wrap.className = "chart-row" + row[3];

        var label = document.createElement("div");
        label.className = "label";
        var name = document.createElement("strong");
        name.textContent = row[0] + " " + row[2] + " ms";
        label.appendChild(name);
        label.appendChild(document.createTextNode("median, range " + min + " to " + max + " ms"));

        var track = document.createElement("div");
        track.className = "track";
        track.setAttribute("role", "img");
        track.setAttribute("aria-label", row[0] + " create: median " + row[2] + " ms, range " + min + " to " + max + " ms");
        var range = document.createElement("span");
        range.className = "range";
        range.style.left = (min / scaleMax * 100) + "%";
        range.style.width = Math.max((max - min) / scaleMax * 100, 0.4) + "%";
        var tick = document.createElement("span");
        tick.className = "tick";
        tick.style.left = (row[2] / scaleMax * 100) + "%";
        track.appendChild(range);
        track.appendChild(tick);

        wrap.appendChild(label);
        wrap.appendChild(track);
        chart.appendChild(wrap);
      });

    var axisRow = document.createElement("div");
    axisRow.className = "axis-row";
    var spacer = document.createElement("div");
    axisRow.appendChild(spacer);
    var axis = document.createElement("div");
    axis.className = "axis";
    [0, 100, 200, 300, 400].forEach(function (ms) {
      var span = document.createElement("span");
      span.textContent = ms + " ms";
      axis.appendChild(span);
    });
    axisRow.appendChild(axis);
    chart.appendChild(axisRow);
  }

  function showFailure(message) {
    body.textContent = message;
  }

  Promise.all([
    fetch("data/session.json").then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }),
    fetch("data/latency.json").then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
  ]).then(function (results) {
    var session = results[0];
    var latency = results[1];
    var lines = session.lines;
    renderLatency(latency);
    button.disabled = false;
    button.addEventListener("click", function () { play(lines); });

    if (reduceMotion) {
      renderInstant(lines);
      return;
    }
    var terminal = document.getElementById("terminal");
    var started = false;
    var observer = new IntersectionObserver(function (entries) {
      if (started || !entries[0].isIntersecting) return;
      started = true;
      observer.disconnect();
      play(lines);
    }, { threshold: 0.35 });
    observer.observe(terminal);
  }).catch(function () {
    showFailure("The recording could not be loaded. Run scripts/record_session.py to regenerate site/data/.");
    document.getElementById("latency-caption").textContent = "Latency data could not be loaded.";
  });
})();
