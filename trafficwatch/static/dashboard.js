/* Traffic Watch dashboard: Aircraft / Maritime charts above the legend (Chart.js). */
(function () {
  "use strict";
  const TW = window.TW;

  // Dark-surface categorical steps, validated (dataviz validate_palette.js) against the panel surface #141a24.
  const SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300"];
  const OTHER = "#6b7385";
  const PRIVATE = "#454c5a";
  const INK = "#c3c9d5";
  const MUTED = "#8b94a6";
  const GRID = "rgba(255,255,255,0.07)";
  const RANGES = [[6, "6 h"], [24, "24 h"], [168, "7 d"]];
  const SNAPSHOT_EVERY = 30000;
  const HISTORY_EVERY = 300000;

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

  function countAxis(stacked, max) {
    return { beginAtZero: true, stacked: !!stacked, max, grid: { color: GRID }, border: { display: false },
      ticks: { color: MUTED, maxTicksLimit: 4, font: { size: 10 }, precision: 0 } };
  }

  /* Horizontal bar of {label: count}, already sorted; colors optional per label. */
  function barConfig(entries, colorFor) {
    const o = baseOptions();
    o.indexAxis = "y";
    o.scales = {
      x: countAxis(false), y: { grid: { display: false }, border: { display: false },
        ticks: { color: INK, font: { size: 10.5 }, autoSkip: false } },
    };
    return { type: "bar", options: o, data: {
      labels: entries.map((e) => e[0]),
      datasets: [{ data: entries.map((e) => e[1]), backgroundColor: entries.map((e, i) => colorFor(e[0], i)),
        borderRadius: 4, borderSkipped: false, maxBarThickness: 12, categoryPercentage: 0.8, barPercentage: 0.9 }],
    } };
  }

  TW.dashboard = function (el, opts) {
    const port = opts.port;
    let tab = "aircraft", hours = 24, open = false, lastLive = null, samples = [], config = { ships: true };
    const charts = {};
    try {
      const saved = JSON.parse(localStorage.getItem("tw-dash") || "{}");
      tab = saved.tab || tab;
      hours = saved.hours || hours;
      open = !!saved.open;
    } catch (e) {}
    const persist = () => { try { localStorage.setItem("tw-dash", JSON.stringify({ tab, hours, open })); } catch (e) {} };

    el.innerHTML =
      `<div class="tw-dash-head">` +
      `<button type="button" class="tw-dash-toggle" aria-expanded="false"><span>Dashboard</span><span class="tw-dash-caret">▸</span></button>` +
      `<div class="tw-seg" role="tablist" aria-label="Traffic type">` +
      `<button type="button" role="tab" data-tab="aircraft">Aircraft</button><button type="button" role="tab" data-tab="maritime">Maritime</button></div>` +
      `<div class="tw-seg" aria-label="Time range">${RANGES.map(([h, l]) => `<button type="button" data-hours="${h}">${l}</button>`).join("")}</div>` +
      `</div><div class="tw-dash-body"></div>`;
    const body = el.querySelector(".tw-dash-body");
    const toggle = el.querySelector(".tw-dash-toggle");
    L.DomEvent.disableClickPropagation(el);
    L.DomEvent.disableScrollPropagation(el);

    function card(id, title, note, tall) {
      return `<figure class="tw-chart"><figcaption>${esc(title)}${note ? ` <span class="tw-muted">${esc(note)}</span>` : ""}</figcaption>` +
        `<div class="tw-chart-box${tall ? " tw-tall" : ""}" id="${id}-box"><canvas id="${id}" role="img" aria-label="${esc(title)}"></canvas></div></figure>`;
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
      body.innerHTML = tab === "aircraft"
        ? card("twc-ac-time", "Aircraft in area", "over time") + card("twc-alt", "Altitude", "now") +
          card("twc-airline", "Airline share", "over time, % of aircraft", true) + card("twc-mfr", "Manufacturer", "now, top 8")
        : card("twc-ship-type", "Ships by type", "now") + card("twc-moving", "Moving ships", "over time") +
          card("twc-flag", "Flag registries", "now, top 8");
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
    const HISTORY_EMPTY = "No history yet. It fills in while the app runs (a sample every 10 min in live mode, each update in no-key mode).";

    function put(id, cfgFactory, emptyMsg) {
      const canvas = document.getElementById(id);
      if (!canvas || typeof Chart === "undefined") return;
      const cfg = cfgFactory();
      if (!cfg) {
        if (charts[id]) { charts[id].destroy(); delete charts[id]; }
        const box = document.getElementById(`${id}-box`);
        box.dataset.empty = "1";
        box.dataset.msg = emptyMsg || NOW_EMPTY;
        return;
      }
      document.getElementById(`${id}-box`).dataset.empty = "";
      if (charts[id]) {
        charts[id].data = cfg.data;
        charts[id].options = cfg.options;
        charts[id].update("none");
      } else {
        charts[id] = new Chart(canvas, cfg);
      }
    }

    // ---------- current-snapshot charts (from /api/live, no extra API calls) ----------
    function drawSnapshot() {
      if (!lastLive) return;
      if (tab === "aircraft") {
        const ac = lastLive.aircraft;
        const bands = TW.PALETTE.altitude.map((b) => [b.label, 0, b.color]);
        let ground = 0;
        for (const a of ac) {
          if (a.gnd) { ground++; continue; }
          const ft = a.alt ?? 0;
          bands[TW.PALETTE.altitude.findIndex((b) => ft < b.max)][1]++;
        }
        const alt = [...bands, [TW.PALETTE.ground.label, ground, TW.PALETTE.ground.color]];
        put("twc-alt", () => ac.length ? barConfig(alt, (_, i) => alt[i][2]) : null);
        const mfr = {};
        for (const a of ac) if (a.mfr) mfr[a.mfr] = (mfr[a.mfr] || 0) + 1;
        const top = Object.entries(mfr).sort((a, b) => b[1] - a[1]).slice(0, 8);
        put("twc-mfr", () => top.length ? barConfig(top, () => SERIES[0]) : null);
      } else if (config.ships) {
        const vs = lastLive.vessels;
        const types = new Map(TW.PALETTE.ships.map((s) => [s.label, 0]));
        for (const v of vs) { const k = TW.shipClass(v.type).label; types.set(k, types.get(k) + 1); }
        const typeEntries = [...types.entries()].filter((e) => e[1] > 0).sort((a, b) => b[1] - a[1]);
        const colorOf = Object.fromEntries(TW.PALETTE.ships.map((s) => [s.label, s.color]));
        put("twc-ship-type", () => typeEntries.length ? barConfig(typeEntries, (l) => colorOf[l]) : null);
        const flags = {};
        for (const v of vs) if (v.flag) flags[v.flag] = (flags[v.flag] || 0) + 1;
        const topFlags = Object.entries(flags).sort((a, b) => b[1] - a[1]).slice(0, 8);
        put("twc-flag", () => topFlags.length ? barConfig(topFlags, () => SERIES[0]) : null);
      }
    }

    // ---------- over-time charts (from /api/history) ----------
    function lineConfig(datasets, opts2) {
      const o = baseOptions();
      o.interaction = { mode: "index", intersect: false };
      o.plugins.legend = { display: datasets.length > 1, position: "bottom", align: "start",
        labels: { color: INK, boxWidth: 10, boxHeight: 10, font: { size: 10 }, padding: 8 } };
      const tf = tooltipTime(port.tz);
      o.plugins.tooltip.callbacks = { title: (items) => items.length ? tf.format(new Date(items[0].parsed.x)) : "",
        label: (c) => `${c.dataset.label}: ${c.parsed.y == null ? "–" : (opts2.pct ? c.parsed.y.toFixed(0) + "%" : c.parsed.y)}` };
      const now = Date.now();
      o.scales = { x: { ...timeAxis(port.tz, hours), min: now - hours * 3600e3, max: now },
                   y: countAxis(opts2.stacked, opts2.pct ? 100 : undefined) };
      if (opts2.pct) o.scales.y.ticks.callback = (v) => `${v}%`;
      const sparse = (datasets[0]?.data || []).filter((d) => d.y != null).length < 6;
      o.elements = { point: { radius: sparse ? 3 : 0, hitRadius: 8, hoverRadius: 4 }, line: { borderWidth: 2, tension: 0.25 } };
      o.spanGaps = false;
      return { type: "line", options: o, data: { datasets } };
    }

    function drawHistory() {
      const xs = samples;
      if (tab === "aircraft") {
        const pts = xs.filter((s) => s.ac_total != null);
        put("twc-ac-time", () => pts.length ? lineConfig([{ label: "Aircraft", borderColor: SERIES[0],
          backgroundColor: SERIES[0], data: xs.map((s) => ({ x: s.ts * 1000, y: s.ac_total })) }], {}) : null, HISTORY_EMPTY);
        // Airline share: top 6 airlines over the whole range, then Other airlines and Private / unknown.
        const totals = {};
        for (const s of pts) for (const [k, v] of Object.entries(s.ac_airlines || {})) {
          if (k !== "Other" && k !== "Private / unknown") totals[k] = (totals[k] || 0) + v;
        }
        const top = Object.entries(totals).sort((a, b) => b[1] - a[1]).slice(0, SERIES.length).map((e) => e[0]);
        const series = [...top.map((name, i) => [name, SERIES[i]]), ["Other airlines", OTHER], ["Private / unknown", PRIVATE]];
        const share = (s, name) => {
          if (s.ac_total == null || !s.ac_airlines) return null;
          const all = Object.values(s.ac_airlines).reduce((a, b) => a + b, 0) || 1;
          let n;
          if (name === "Private / unknown") n = s.ac_airlines["Private / unknown"] || 0;
          else if (name === "Other airlines") {
            n = Object.entries(s.ac_airlines).filter(([k]) => k !== "Private / unknown" && !top.includes(k))
              .reduce((a, [, v]) => a + v, 0);
          } else n = s.ac_airlines[name] || 0;
          return (100 * n) / all;
        };
        put("twc-airline", () => pts.length ? lineConfig(series.map(([name, color]) => ({
          // Surface-coloured edges keep a visible gap between stacked bands.
          label: name, borderColor: "#141a24", backgroundColor: color, fill: true, borderWidth: 1.5,
          data: xs.map((s) => ({ x: s.ts * 1000, y: share(s, name) })) })), { stacked: true, pct: true }) : null, HISTORY_EMPTY);
      } else if (config.ships) {
        const pts = xs.filter((s) => s.ves_total != null);
        put("twc-moving", () => pts.length ? lineConfig([
          { label: "Moving", borderColor: SERIES[0], backgroundColor: SERIES[0], data: pts.map((s) => ({ x: s.ts * 1000, y: s.ves_moving })) },
          { label: "All ships", borderColor: SERIES[1], backgroundColor: SERIES[1], data: pts.map((s) => ({ x: s.ts * 1000, y: s.ves_total })) },
        ], {}) : null, HISTORY_EMPTY);
      }
    }

    function draw() { if (open) { drawSnapshot(); drawHistory(); } }

    async function loadHistory() {
      if (!open) return;
      try {
        const d = await (await fetch(`/api/history/${encodeURIComponent(port.id)}?hours=${hours}`)).json();
        samples = d.samples || [];
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
