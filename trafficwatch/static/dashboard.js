/* Traffic Watch dashboard: Aircraft / Maritime charts above the legend (Chart.js).
   Current-state charts are pies; the two over-time charts are lines that break where nothing was recording. */
(function () {
  "use strict";
  const TW = window.TW;

  // Dark-surface categorical steps, validated (dataviz validate_palette.js) against the panel surface #141a24,
  // in this order. Pie slices are always drawn in slot order, so neighbours are validated pairs.
  const SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"];
  const OTHER = "#6b7385";
  const SURFACE = "#141a24";
  const INK = "#c3c9d5";
  const MUTED = "#8b94a6";
  const GRID = "rgba(255,255,255,0.07)";
  const RANGES = [[6, "6 h"], [24, "24 h"], [168, "7 d"]];
  const SNAPSHOT_EVERY = 30000;
  const HISTORY_EVERY = 300000;
  const AIRLINE_SLICES = 8;   // at least 8 named airlines (v1.0.3 spec)
  const TOP_SLICES = 5;       // other pies: top 5 + "Other" (<= 6 slices)

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function baseOptions() {
    return {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {
        legend: { display: false },
        tooltip: { backgroundColor: "#0b0e14", borderColor: "rgba(255,255,255,.15)", borderWidth: 1,
          titleColor: "#fff", bodyColor: INK, padding: 8, boxPadding: 4 },
      },
      scales: {},
    };
  }

  function timeAxis(tz, hours) {
    const fmt = new Intl.DateTimeFormat("en-GB", hours > 24
      ? { timeZone: tz, weekday: "short", hour: "2-digit", hour12: false }
      : { timeZone: tz, hour: "2-digit", minute: "2-digit", hour12: false });
    return { type: "linear", grid: { color: GRID }, border: { color: "rgba(255,255,255,.15)" },
      ticks: { color: MUTED, maxTicksLimit: 5, font: { size: 10 }, callback: (v) => fmt.format(new Date(v)) } };
  }
  const tooltipTime = (tz) => new Intl.DateTimeFormat("en-GB", { timeZone: tz, weekday: "short", hour: "2-digit",
    minute: "2-digit", hour12: false });

  /* Keep each named entity on the same colour slot across refreshes (and restarts).
     Slots stay contiguous (0..k-1): a newcomer takes the slot of whoever dropped out,
     survivors are never repainted, and slot order = validated neighbour order. */
  function stableSlots(key, names) {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(key) || "{}"); } catch (e) {}
    const k = names.length, out = {}, used = new Set();
    for (const n of names) {
      const s = saved[n];
      if (s != null && s < k && !used.has(s)) { out[n] = s; used.add(s); }
    }
    const free = [...Array(k).keys()].filter((i) => !used.has(i));
    for (const n of names) if (out[n] == null) out[n] = free.shift();
    for (const [n, s] of Object.entries(saved)) if (Object.values(out).includes(s) && out[n] == null) delete saved[n];
    Object.assign(saved, out);
    try { localStorage.setItem(key, JSON.stringify(saved)); } catch (e) {}
    return out;
  }

  /* counts: {name: n}. Top `n` named entities on stable colour slots, the rest folded into grey slices. */
  function rankedSlices(key, counts, n, extras) {
    const special = new Set((extras || []).map((e) => e[0]));
    const named = Object.entries(counts).filter(([k, v]) => v > 0 && !special.has(k)).sort((a, b) => b[1] - a[1]);
    const top = named.slice(0, n);
    const slots = stableSlots(key, top.map((e) => e[0]));
    const slices = top.map(([label, value]) => ({ label, value, color: SERIES[slots[label]], slot: slots[label] }))
      .sort((a, b) => a.slot - b.slot);
    // Grey slices last: entities beyond the top n fold into the first one ("Other ...").
    const greys = (extras || [["Other", OTHER, 0]]).map((e) => [...e]);
    greys[0][2] += named.slice(n).reduce((a, e) => a + e[1], 0);
    for (const [label, color, value] of greys) if (value > 0) slices.push({ label, value, color });
    return slices;
  }

  function pieConfig(slices) {
    const total = slices.reduce((a, s) => a + s.value, 0) || 1;
    const pct = (v) => Math.round((100 * v) / total);
    const o = baseOptions();
    o.plugins.legend = {
      display: true, position: "right",
      labels: {
        color: INK, boxWidth: 10, boxHeight: 10, padding: 6, font: { size: 10.5 },
        generateLabels: (chart) => slices.map((s, i) => ({
          text: `${s.label} — ${pct(s.value)}%`, fillStyle: s.color, strokeStyle: s.color, fontColor: INK,
          lineWidth: 0, hidden: !chart.getDataVisibility(i), index: i })),
      },
      onClick: (e, item, legend) => { legend.chart.toggleDataVisibility(item.index); legend.chart.update(); },
    };
    o.plugins.tooltip.callbacks = { label: (c) => ` ${c.label}: ${c.parsed} (${pct(c.parsed)}%)` };
    return { type: "pie", options: o, data: {
      labels: slices.map((s) => s.label),
      // 2 px surface-coloured edges keep a visible gap between slices.
      datasets: [{ data: slices.map((s) => s.value), backgroundColor: slices.map((s) => s.color),
        borderColor: SURFACE, borderWidth: 2, hoverOffset: 4 }],
    } };
  }

  /* Points for a line; a null point where the gap between samples means nothing was recording. */
  function withGaps(rows, key, gapS) {
    const out = [];
    for (let i = 0; i < rows.length; i++) {
      if (i && rows[i].ts - rows[i - 1].ts > gapS) out.push({ x: (rows[i - 1].ts + 1) * 1000, y: null });
      out.push({ x: rows[i].ts * 1000, y: rows[i][key] });
    }
    return out;
  }

  TW.dashboard = function (el, opts) {
    const port = opts.port;
    let tab = "aircraft", hours = 24, open = false, lastLive = null, history = null, config = { ships: true };
    const charts = {};
    try {
      const saved = JSON.parse(localStorage.getItem("tw-dash") || "{}");
      tab = saved.tab || tab;
      hours = RANGES.some(([h]) => h === saved.hours) ? saved.hours : hours;
      open = !!saved.open;
    } catch (e) {}
    const persist = () => { try { localStorage.setItem("tw-dash", JSON.stringify({ tab, hours, open })); } catch (e) {} };

    el.innerHTML =
      `<div class="tw-dash-head">` +
      `<button type="button" class="tw-dash-toggle" aria-expanded="false"><span>Dashboard</span><span class="tw-dash-caret">▸</span></button>` +
      `<div class="tw-seg" role="tablist" aria-label="Traffic type">` +
      `<button type="button" role="tab" data-tab="aircraft">Aircraft</button><button type="button" role="tab" data-tab="maritime">Maritime</button></div>` +
      `<div class="tw-seg" aria-label="Time range for the over-time charts">${RANGES.map(([h, l]) => `<button type="button" data-hours="${h}">${l}</button>`).join("")}</div>` +
      `</div><div class="tw-dash-body"></div>`;
    const body = el.querySelector(".tw-dash-body");
    const toggle = el.querySelector(".tw-dash-toggle");
    L.DomEvent.disableClickPropagation(el);
    L.DomEvent.disableScrollPropagation(el);

    function card(id, title, note, kind) {
      return `<figure class="tw-chart"><figcaption>${esc(title)}${note ? ` <span class="tw-muted">${esc(note)}</span>` : ""}</figcaption>` +
        `<div class="tw-chart-box${kind ? " tw-" + kind : ""}" id="${id}-box"><canvas id="${id}" role="img" aria-label="${esc(title)}"></canvas></div>` +
        `<div class="tw-chart-note" id="${id}-note"></div></figure>`;
    }

    function layout() {
      Object.values(charts).forEach((c) => c.destroy());
      for (const k of Object.keys(charts)) delete charts[k];
      el.querySelectorAll("[data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
      el.querySelectorAll("[data-hours]").forEach((b) => b.classList.toggle("on", +b.dataset.hours === hours));
      if (tab === "maritime" && !config.ships) {
        body.innerHTML = `<div class="tw-dash-empty">Live ships need a free AISStream key.<br>` +
          `<a href="/setup" class="tw-btn">Add a key</a></div>`;
        return;
      }
      const gapNote = "over time · gaps = not recording";
      body.innerHTML = tab === "aircraft"
        ? card("twc-ac-time", "Aircraft in area", gapNote) + card("twc-alt", "Altitude", "now", "pie") +
          card("twc-airline", "Airline share", "now, of airline flights", "pie-tall") + card("twc-mfr", "Manufacturer", "now", "pie")
        : card("twc-ship-type", "Ships by type", "now", "pie") + card("twc-moving", "Moving ships", gapNote) +
          card("twc-flag", "Flag registries", "now", "pie");
      draw();
    }

    function setOpen(v, fromUser) {
      open = v;
      el.classList.toggle("tw-collapsed", !open);
      toggle.setAttribute("aria-expanded", String(open));
      el.querySelector(".tw-dash-caret").textContent = open ? "▾" : "▸";
      persist();
      if (open) {
        if (fromUser && opts.onOpen) opts.onOpen();
        layout();
        loadHistory();
      }
    }

    toggle.addEventListener("click", () => setOpen(!open, true));
    el.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => {
      tab = b.dataset.tab; persist();
      if (!open) setOpen(true, true); else layout();
    }));
    el.querySelectorAll("[data-hours]").forEach((b) => b.addEventListener("click", () => {
      hours = +b.dataset.hours; persist();
      if (!open) setOpen(true, true); else { layout(); loadHistory(); }
    }));

    const NOW_EMPTY = "Nothing in this area right now.";
    const HISTORY_EMPTY = "No history yet. It is recorded while the app (or the background collector) runs.";

    function setEmpty(id, msg) {
      if (charts[id]) { charts[id].destroy(); delete charts[id]; }
      const box = document.getElementById(`${id}-box`);
      if (box) { box.dataset.empty = "1"; box.dataset.msg = msg; }
    }

    function put(id, cfg) {
      const canvas = document.getElementById(id);
      if (!canvas || typeof Chart === "undefined") return;
      document.getElementById(`${id}-box`).dataset.empty = "";
      if (charts[id] && charts[id].config.type === cfg.type) {
        charts[id].data = cfg.data;
        charts[id].options = cfg.options;
        charts[id].update("none");
      } else {
        if (charts[id]) charts[id].destroy();
        charts[id] = new Chart(canvas, cfg);
      }
    }

    /* A pie, or a sentence when there is nothing to divide (a one-slice pie says nothing). */
    function putPie(id, slices, what, note) {
      const noteEl = document.getElementById(`${id}-note`);
      if (noteEl) noteEl.textContent = note || "";
      const total = slices.reduce((a, s) => a + s.value, 0);
      if (!total) return setEmpty(id, NOW_EMPTY);
      if (slices.length === 1) return setEmpty(id, `All ${total} ${what} are ${slices[0].label}.`);
      put(id, pieConfig(slices));
    }

    // ---------- current-state pies (from /api/live; no extra API calls) ----------
    function drawSnapshot() {
      if (!lastLive) return;
      if (tab === "aircraft") {
        const ac = lastLive.aircraft;
        // Altitude: fixed band order, same colours as the planes on the map.
        const bands = TW.PALETTE.altitude.map((b) => ({ label: b.label, value: 0, color: b.color }));
        let ground = 0;
        for (const a of ac) {
          if (a.gnd) { ground++; continue; }
          const ft = a.alt ?? 0;
          bands[TW.PALETTE.altitude.findIndex((b) => ft < b.max)].value++;
        }
        bands.push({ label: TW.PALETTE.ground.label, value: ground, color: TW.PALETTE.ground.color });
        putPie("twc-alt", bands.filter((s) => s.value > 0), "aircraft");

        const airlines = {};
        let priv = 0;
        for (const a of ac) {
          if (a.airline) airlines[a.airline] = (airlines[a.airline] || 0) + 1; else priv++;
        }
        // Share of airline flights; private / unidentified aircraft would otherwise swamp the pie.
        putPie("twc-airline", rankedSlices("tw-slots-airline", airlines, AIRLINE_SLICES,
          [["Other airlines", OTHER, 0]]), "airline flights",
          priv ? `${priv} private or unidentified aircraft not included` : "");

        const mfr = {};
        for (const a of ac) if (a.mfr) mfr[a.mfr] = (mfr[a.mfr] || 0) + 1;
        putPie("twc-mfr", rankedSlices("tw-slots-mfr", mfr, TOP_SLICES), "identified aircraft");
      } else if (config.ships) {
        const vs = lastLive.vessels;
        // Ship types keep the map's marker colours, in the legend's fixed order; top 5 + Other / unknown.
        const counts = new Map(TW.PALETTE.ships.map((s) => [s.label, 0]));
        let untyped = 0;
        for (const v of vs) {
          if (v.type == null) { untyped++; continue; }   // type arrives a few minutes after a ship is first seen
          const k = TW.shipClass(v.type).label; counts.set(k, counts.get(k) + 1);
        }
        const otherLabel = TW.PALETTE.ships[TW.PALETTE.ships.length - 1].label;
        const ranked = [...counts.entries()].filter(([k, n]) => n > 0 && k !== otherLabel)
          .sort((a, b) => b[1] - a[1]);
        const keep = new Set(ranked.slice(0, TOP_SLICES).map((e) => e[0]));
        let other = counts.get(otherLabel) + ranked.slice(TOP_SLICES).reduce((a, e) => a + e[1], 0);
        const slices = TW.PALETTE.ships.filter((s) => keep.has(s.label))
          .map((s) => ({ label: s.label, value: counts.get(s.label), color: s.color }));
        if (other) slices.push({ label: "Other types", value: other, color: OTHER });
        putPie("twc-ship-type", slices, "ships with a known type",
          untyped ? `${untyped} ships not yet identified (type is broadcast every few minutes)` : "");

        const flags = {};
        for (const v of vs) if (v.flag) flags[v.flag] = (flags[v.flag] || 0) + 1;
        putPie("twc-flag", rankedSlices("tw-slots-flag", flags, TOP_SLICES), "ships with a known flag");
      }
    }

    // ---------- over-time lines (from /api/history) ----------
    function lineConfig(datasets) {
      const o = baseOptions();
      o.interaction = { mode: "index", intersect: false };
      o.plugins.legend = { display: datasets.length > 1, position: "bottom", align: "start",
        labels: { color: INK, boxWidth: 10, boxHeight: 10, font: { size: 10 }, padding: 8 } };
      const tf = tooltipTime(port.tz);
      o.plugins.tooltip.callbacks = { title: (items) => items.length ? tf.format(new Date(items[0].parsed.x)) : "",
        label: (c) => `${c.dataset.label}: ${c.parsed.y == null ? "not recording" : c.parsed.y}` };
      const now = Date.now();
      o.scales = {
        x: { ...timeAxis(port.tz, hours), min: now - hours * 3600e3, max: now },
        y: { beginAtZero: true, grid: { color: GRID }, border: { display: false },
          ticks: { color: MUTED, maxTicksLimit: 4, font: { size: 10 }, precision: 0 } },
      };
      const points = datasets[0].data.filter((d) => d.y != null).length;
      o.elements = { point: { radius: points < 6 ? 3 : 0, hitRadius: 8, hoverRadius: 4 }, line: { borderWidth: 2, tension: 0.25 } };
      return { type: "line", options: o, data: { datasets: datasets.map((d) => ({ spanGaps: false, ...d })) } };
    }

    function drawHistory() {
      if (!history) return;
      if (tab === "aircraft") {
        const rows = history.aircraft;
        if (!rows.length) return setEmpty("twc-ac-time", HISTORY_EMPTY);
        put("twc-ac-time", lineConfig([{ label: "Aircraft", borderColor: SERIES[0], backgroundColor: SERIES[0],
          data: withGaps(rows, "total", history.gap_s.aircraft) }]));
      } else if (config.ships) {
        const rows = history.vessels;
        if (!rows.length) return setEmpty("twc-moving", HISTORY_EMPTY);
        put("twc-moving", lineConfig([
          { label: "Moving", borderColor: SERIES[0], backgroundColor: SERIES[0], data: withGaps(rows, "moving", history.gap_s.vessels) },
          { label: "All ships", borderColor: SERIES[1], backgroundColor: SERIES[1], data: withGaps(rows, "total", history.gap_s.vessels) },
        ]));
      }
    }

    function draw() { if (open) { drawSnapshot(); drawHistory(); } }

    async function loadHistory() {
      if (!open) return;
      try {
        history = await (await fetch(`/api/history/${encodeURIComponent(port.id)}?hours=${hours}`)).json();
        drawHistory();
      } catch (e) { /* keep the last data; the next refresh retries */ }
    }

    setInterval(() => { if (open) drawSnapshot(); }, SNAPSHOT_EVERY);
    setInterval(loadHistory, HISTORY_EVERY);
    setOpen(open, false);

    return {
      onLive(d) {
        const first = !lastLive;
        lastLive = d;
        if (d.mode && d.mode.ships !== config.ships) { config.ships = d.mode.ships; if (open) layout(); }
        if (first && open) drawSnapshot();
      },
      collapse() { if (open) setOpen(false, false); },
    };
  };
})();
