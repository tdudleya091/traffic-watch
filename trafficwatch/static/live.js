/* Traffic Watch live layer: polls the local server and keeps markers in sync. */
(function () {
  "use strict";
  const TW = (window.TW = {});
  const LIVE_EVERY = 5000;
  const WX_EVERY = 60000;
  const RADAR_EVERY = 600000;

  const PLANE = '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 1.5c.8 0 1.4.8 1.4 1.9v6l8.1 4.6v2.2l-8.1-2.4v4.9l2.3 1.8v1.8L12 21.5l-3.7 1v-1.8l2.3-1.8v-4.9l-8.1 2.4v-2.2l8.1-4.6v-6c0-1.1.6-1.9 1.4-1.9z"/></svg>';
  const HELI = '<svg viewBox="0 0 24 24"><g fill="currentColor" stroke="currentColor"><ellipse cx="12" cy="10.5" rx="3.4" ry="4.8" stroke="none"/><rect x="11.3" y="14" width="1.4" height="7.5" stroke="none"/><line x1="9" y1="21.2" x2="15" y2="21.2" stroke-width="1.4"/><line x1="3" y1="4" x2="21" y2="17" stroke-width="1.3"/><line x1="21" y1="4" x2="3" y2="17" stroke-width="1.3"/></g></svg>';
  const SHIP = '<svg viewBox="0 0 24 24"><path fill="currentColor" stroke="#000" stroke-width="0.8" d="M12 2 L17 20 L12 17 L7 20 Z"/></svg>';
  const MOORED = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="5.5" fill="currentColor" stroke="#000" stroke-width="1"/></svg>';
  const TRIANGLE = (fill) => `<svg viewBox="0 0 14 14" width="14" height="14"><path d="M7 1 L13 13 L1 13 Z" fill="${fill}" stroke="#000"/></svg>`;

  /* One palette drives both the markers and the legend, so they cannot drift apart. */
  const PALETTE = {
    ground: { color: "#8a93a3", label: "On ground" },
    altitude: [
      { max: 5000, color: "#ffd34d", label: "< 5,000 ft" },
      { max: 15000, color: "#ff9f43", label: "5,000–15,000 ft" },
      { max: 30000, color: "#4dd0ff", label: "15,000–30,000 ft" },
      { max: Infinity, color: "#c9b6ff", label: "> 30,000 ft" },
    ],
    ships: [
      { label: "Cargo", color: "#3ecf6e", test: (t) => t >= 70 && t <= 79 },
      { label: "Tanker", color: "#ff5b5b", test: (t) => t >= 80 && t <= 89 },
      { label: "Passenger / ferry", color: "#5aa7ff", test: (t) => (t >= 60 && t <= 69) || (t >= 40 && t <= 49) },
      { label: "Fishing / dredging", color: "#ffb84d", test: (t) => t === 30 || t === 33 },
      { label: "Tug / pilot / tender", color: "#4de3e3", test: (t) => [31, 32, 50, 52, 53].includes(t) },
      { label: "Military / law enf.", color: "#c8c8c8", test: (t) => t === 35 || t === 55 },
      { label: "Search & rescue", color: "#ff7af0", test: (t) => t === 51 },
      { label: "Sailing / pleasure", color: "#b48cff", test: (t) => t === 36 || t === 37 },
      { label: "Other / unknown", color: "#9aa5b8", test: () => true },
    ],
    flightCat: { VFR: "#3ecf6e", MVFR: "#5aa7ff", IFR: "#ff5b5b", LIFR: "#e15bff" },
    pirep: { normal: "#ffd34d", urgent: "#ff3b3b" },
  };

  const NAV = { 0: "Under way (engine)", 1: "At anchor", 2: "Not under command", 3: "Restricted manoeuvrability",
    4: "Constrained by draught", 5: "Moored", 6: "Aground", 7: "Fishing", 8: "Under way (sail)" };

  const shipClass = (t) => PALETTE.ships.find((s) => t != null && s.test(t)) || PALETTE.ships[PALETTE.ships.length - 1];
  TW.PALETTE = PALETTE;
  TW.shipClass = shipClass;
  function altColor(a) {
    if (a.gnd) return PALETTE.ground.color;
    const ft = a.alt ?? 0;
    return PALETTE.altitude.find((b) => ft < b.max).color;
  }

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtNum = (n) => (n == null ? "–" : Number(n).toLocaleString("en-US"));
  const row = (k, v) => (v == null || v === "" ? "" : `<dt>${esc(k)}</dt><dd>${v}</dd>`);
  const link = (href, text) => `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(text)} ↗</a>`;

  // ---------- aircraft text ----------
  function acType(a) {
    if (a.model) return `${esc(a.model)}${a.type ? ` (${esc(a.type)})` : ""}`;
    if (a.type) return esc(a.type);
    return a.cat_label ? `${esc(a.cat_label)} <span class="tw-inferred">(ADS-B category)</span>` : "Type unknown";
  }
  function acTip(a) {
    return `<b>${esc(a.cs)}</b>${a.airline ? ` · ${esc(a.airline)}` : ""}${a.mil ? ' <span class="tw-badge">MIL</span>' : ""}<br>` +
      `${acType(a)}${a.reg ? ` · ${esc(a.reg)}` : ""}<br>` +
      `${a.gnd ? "On ground" : `Alt ${fmtNum(a.alt)} ft`} · ${fmtNum(a.gs)} kt · hdg ${a.hdg ?? "–"}°<br>` +
      `<span style="opacity:.7">click for details</span>`;
  }
  function acPop(a) {
    const cs = (a.cs || "").trim();
    const links = [
      cs && !cs.toLowerCase().startsWith(a.id) ? link(`https://www.flightaware.com/live/flight/${encodeURIComponent(cs)}`, "FlightAware") : "",
      link(`https://globe.adsbexchange.com/?icao=${encodeURIComponent(a.id)}`, "ADS-B Exchange"),
    ].filter(Boolean).join("");
    return `<h3>${esc(a.cs)}${a.mil ? ' <span class="tw-badge">MILITARY</span>' : ""}</h3>` +
      `<div class="tw-pop-sub">${a.airline ? esc(a.airline)
        : cs.toLowerCase() === a.id ? "No callsign broadcast" : "Operator not identified from callsign"}</div><dl>` +
      row("Type", acType(a)) + row("Registration", esc(a.reg)) + row("Built", esc(a.year)) +
      row("Registered owner", esc(a.owner)) + row("ICAO24", esc(a.id)) + row("Reg. country", esc(a.cty)) +
      row("Altitude", a.gnd ? "On ground" : `${fmtNum(a.alt)} ft`) + row("Ground speed", `${fmtNum(a.gs)} kt`) +
      row("Heading", a.hdg == null ? "–" : `${a.hdg}°`) +
      row("Vertical speed", a.vr == null ? "–" : `${a.vr > 0 ? "+" : ""}${fmtNum(a.vr)} fpm`) +
      row("Squawk", esc(a.sq || "–")) + row("Last report", `${a.age}s before map time`) +
      `</dl><div class="tw-links">${links}</div>`;
  }

  // ---------- vessel text ----------
  function vsKind(v) { return v.type_label || shipClass(v.type).label; }
  function vsTip(v) {
    return `<b>${esc(v.name)}</b> · ${esc(vsKind(v))}<br>` +
      [v.flag ? `Flag: ${esc(v.flag)}` : "", v.operator ? `Operator: ${esc(v.operator)} <span class="tw-inferred">(inferred)</span>` : ""]
        .filter(Boolean).join(" · ") + (v.flag || v.operator ? "<br>" : "") +
      `${v.sog == null ? "–" : v.sog.toFixed(1)} kn · ${esc(NAV[v.nav] || "Status n/a")}<br>` +
      (v.dest ? `To ${esc(v.dest)}<br>` : "") + `<span style="opacity:.7">click for details</span>`;
  }
  function vsPop(v) {
    const vf = v.imo ? `https://www.vesselfinder.com/vessels/details/${encodeURIComponent(v.imo)}`
      : `https://www.vesselfinder.com/vessels/details/${encodeURIComponent(v.id)}`;
    return `<h3>${esc(v.name)}</h3><div class="tw-pop-sub">${esc(vsKind(v))}</div><dl>` +
      row("Operator", v.operator ? `${esc(v.operator)} <span class="tw-inferred">(inferred from name)</span>` : null) +
      row("Flag", esc(v.flag)) + row("MMSI", esc(v.id)) + row("IMO", esc(v.imo)) + row("Call sign", esc(v.cs)) +
      row("Length", v.len ? `${v.len} m` : null) + row("Draught", v.draught ? `${v.draught} m` : null) +
      row("Speed", v.sog == null ? "–" : `${v.sog.toFixed(1)} kn`) +
      row("Course", v.cog == null ? "–" : `${Math.round(v.cog)}°`) + row("Heading", v.hdg == null ? null : `${v.hdg}°`) +
      row("Status", esc(NAV[v.nav] || "n/a")) + row("Destination", esc(v.dest)) + row("AIS class", esc(v.cls)) +
      row("Last report", `${v.age}s before map time`) +
      `</dl><div class="tw-links">${link(vf, "VesselFinder")}` +
      `${link(`https://www.marinetraffic.com/en/ais/details/ships/mmsi:${encodeURIComponent(v.id)}`, "MarineTraffic")}</div>`;
  }

  /* Keeps one marker per id; moves/rotates in place instead of rebuilding. */
  function syncLayer(layer, store, items, opts) {
    const seen = new Set();
    for (const it of items) {
      seen.add(it.id);
      const rot = opts.rotation(it);
      const color = opts.color(it);
      const shape = opts.shape(it);
      let mk = store.get(it.id);
      if (mk && mk._twShape !== shape) {
        layer.removeLayer(mk);
        store.delete(it.id);
        mk = null;
      }
      if (!mk) {
        mk = L.marker([it.lat, it.lon], {
          icon: L.divIcon({ className: "tw-mk", html: `<div class="tw-rot">${shape}</div>`,
            iconSize: [opts.size, opts.size], iconAnchor: [opts.size / 2, opts.size / 2] }),
          keyboard: false,
        });
        mk._twShape = shape;
        mk.bindTooltip((m) => opts.tip(m._tw), { className: "tw-tip", direction: "top", offset: [0, -8] });
        mk.bindPopup((m) => opts.pop(m._tw), { className: "tw-pop", maxWidth: 340, autoPanPadding: [40, 40] });
        mk.on("popupopen", () => mk.closeTooltip());
        store.set(it.id, mk);
        layer.addLayer(mk);
      } else {
        mk.setLatLng([it.lat, it.lon]);
      }
      mk._tw = it;
      const el = mk.getElement && mk.getElement();
      if (el) {
        const r = el.firstChild;
        r.style.transform = `rotate(${rot}deg)`;
        r.style.color = color;
      } else {
        mk._twPending = { rot, color };
      }
      if (mk.isTooltipOpen && mk.isTooltipOpen()) mk.setTooltipContent(opts.tip(it));
      if (mk.isPopupOpen && mk.isPopupOpen()) mk.setPopupContent(opts.pop(it));
    }
    for (const [id, mk] of store) {
      if (!seen.has(id)) {
        layer.removeLayer(mk);
        store.delete(id);
      }
    }
  }

  // ---------- legend ----------
  function buildLegend(el, onOpen) {
    const sym = (inner) => `<span class="tw-lg-sym">${inner}</span>`;
    const r = (s, label) => `<div class="tw-lg-row">${s}<span>${esc(label)}</span></div>`;
    const tinted = (svg, color) => `<span style="color:${color};display:inline-flex">${svg}</span>`;
    const sec = (title, rows) => `<div class="tw-lg-sec"><h4>${esc(title)}</h4>${rows.join("")}</div>`;

    const aircraft = [
      ...PALETTE.altitude.map((b) => r(sym(tinted(PLANE, b.color)), b.label)),
      r(sym(tinted(PLANE, PALETTE.ground.color)), PALETTE.ground.label),
      r(sym(tinted(HELI, PALETTE.altitude[0].color)), "Helicopter"),
    ];
    const ships = [
      ...PALETTE.ships.map((s) => r(sym(`<span class="tw-lg-sw" style="background:${s.color}"></span>`), s.label)),
      r(sym(tinted(SHIP, "#3ecf6e")), "Moving (points along course)"),
      r(sym(tinted(MOORED, "#3ecf6e")), "Stopped / moored"),
    ];
    const landmarks = [
      r(sym('<span class="tw-apt tw-apt-major">✈</span>'), "Airport (code)"),
      r(sym('<span class="tw-port tw-port-major">⚓</span>'), "Port / harbor"),
      r(sym('<span class="tw-lg-dot" style="background:#ddd"></span>'), "City"),
      r(sym('<span class="tw-label tw-shield tw-shield-i">I-5</span>'), "US Interstate"),
      r(sym('<span class="tw-label tw-shield tw-shield-e">E1</span>'), "Japan expressway"),
      r(sym('<span class="tw-label tw-shield tw-shield-us">US 1</span>'), "US / state / national route"),
      r(sym('<span class="tw-lg-line"></span>'), "Major highway"),
      r(sym('<span class="tw-lg-box"></span>'), "200-mile coverage box"),
    ];
    const weather = [
      ...Object.entries(PALETTE.flightCat).map(([k, c]) =>
        r(sym(`<span class="tw-lg-dot" style="background:${c}"></span>`), `Weather station — ${k}`)),
      r(sym(TRIANGLE(PALETTE.pirep.normal)), "Pilot report (PIREP)"),
      r(sym(TRIANGLE(PALETTE.pirep.urgent)), "Urgent pilot report"),
    ];
    el.innerHTML = `<button class="tw-lg-head" type="button" aria-expanded="true"><span>Legend</span><span class="tw-lg-caret">▾</span></button>` +
      `<div class="tw-lg-body">${sec("Aircraft (by altitude)", aircraft)}${sec("Ships (by type)", ships)}` +
      `${sec("Landmarks", landmarks)}${sec("Weather", weather)}</div>`;

    const head = el.querySelector(".tw-lg-head");
    const caret = el.querySelector(".tw-lg-caret");
    const apply = (collapsed) => {
      el.classList.toggle("tw-collapsed", collapsed);
      head.setAttribute("aria-expanded", String(!collapsed));
      caret.textContent = collapsed ? "▸" : "▾";
    };
    let collapsed = false;
    try { collapsed = localStorage.getItem("tw-legend-collapsed") === "1"; } catch (e) {}
    apply(collapsed);
    const set = (v) => {
      collapsed = v;
      apply(collapsed);
      try { localStorage.setItem("tw-legend-collapsed", collapsed ? "1" : "0"); } catch (e) {}
    };
    head.addEventListener("click", () => {
      set(!collapsed);
      if (!collapsed && onOpen) onOpen();
    });
    L.DomEvent.disableClickPropagation(el);
    L.DomEvent.disableScrollPropagation(el);
    return { collapse: () => set(true) };
  }

  TW.init = function (cfg) {
    const port = cfg.port;
    const acMarkers = new Map();
    const vsMarkers = new Map();
    let clockOffset = 0;     // server_now - client_now (ms)
    let delayMs = 90000;

    // Markers created while their layer is hidden get styled once they are added.
    for (const layer of [cfg.aircraft, cfg.vessels]) {
      layer.on("layeradd", (e) => {
        const p = e.layer._twPending;
        const el = e.layer.getElement && e.layer.getElement();
        if (p && el) {
          el.firstChild.style.transform = `rotate(${p.rot}deg)`;
          el.firstChild.style.color = p.color;
          e.layer._twPending = null;
        }
      });
    }
    cfg.map.on("overlayadd", () => { refreshLive(); });

    // Dashboard sits above the legend; opening one collapses the other so the map stays visible.
    let dash = null;
    const legend = buildLegend(document.getElementById("tw-legend"), () => dash && dash.collapse());
    dash = TW.dashboard(document.getElementById("tw-dash"), { port, onOpen: () => legend.collapse() });
    for (const id of ["tw-portbar", "tw-panel", "tw-status"]) {
      const el = document.getElementById(id);
      L.DomEvent.disableClickPropagation(el);
      L.DomEvent.disableScrollPropagation(el);
    }

    // ---- UI scale (A- / A+), remembered per viewer ----
    const bar = document.getElementById("tw-portbar");
    const gear = document.createElement("a");
    gear.id = "tw-gear";
    gear.href = "/setup";
    gear.textContent = "⚙";
    gear.title = "API keys and update settings";
    gear.setAttribute("aria-label", "Settings");
    let scale = 1;
    try { scale = parseFloat(localStorage.getItem("tw-scale")) || 1; } catch (e) {}
    const applyScale = () => document.documentElement.style.setProperty("--tw-scale", scale.toFixed(2));
    applyScale();
    for (const [txt, d] of [["A−", -0.1], ["A+", 0.1]]) {
      const b = document.createElement("button");
      b.textContent = txt;
      b.title = d < 0 ? "Smaller text" : "Larger text";
      b.className = "tw-scale-btn";
      b.style.cssText = "font:inherit;color:inherit;background:#1b2230;border:1px solid rgba(255,255,255,.15);border-radius:6px;padding:1px 7px;cursor:pointer";
      b.onclick = () => {
        scale = Math.min(2, Math.max(0.7, scale + d));
        applyScale();
        try { localStorage.setItem("tw-scale", String(scale)); } catch (e) {}
      };
      bar.appendChild(b);
    }
    bar.appendChild(gear);

    // ---- port dropdown, grouped by region ----
    const sel = document.getElementById("tw-port");
    fetch("/api/ports").then((r) => r.json()).then((list) => {
      const groups = new Map();
      for (const p of list) {
        let og = groups.get(p.region);
        if (!og) {
          og = document.createElement("optgroup");
          og.label = p.region;
          groups.set(p.region, og);
          sel.appendChild(og);
        }
        const o = document.createElement("option");
        o.value = p.id;
        o.textContent = p.name;
        if (p.id === port.id) o.selected = true;
        og.appendChild(o);
      }
    });
    sel.addEventListener("change", () => { window.location.href = "/port/" + encodeURIComponent(sel.value); });
    document.title = `Traffic Watch — ${port.name}`;

    // ---- clock (data time = now - delay, in port local time) ----
    const fmtLocal = new Intl.DateTimeFormat("en-GB", { timeZone: port.tz, hour12: false,
      hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const fmtUtc = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", hour12: false,
      hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const fmtZone = new Intl.DateTimeFormat("en-US", { timeZone: port.tz, timeZoneName: "short" });
    const elLocal = document.getElementById("tw-local");
    const elUtc = document.getElementById("tw-utc");
    const elSub = document.getElementById("tw-sub");
    function tick() {
      const t = new Date(Date.now() + clockOffset - delayMs);
      elLocal.textContent = fmtLocal.format(t);
      elUtc.textContent = `(${fmtUtc.format(t)} UTC)`;
      let zone = fmtZone.formatToParts(t).find((p) => p.type === "timeZoneName")?.value || "";
      if (zone.startsWith("GMT") && port.tz_abbr) zone = port.tz_abbr;   // e.g. "GMT+9" -> "JST"
      elSub.textContent = `${port.name} · ${zone} · map ${Math.round(delayMs / 1000)} s behind live`;
    }
    tick();
    setInterval(tick, 1000);

    // ---- live traffic ----
    const elCounts = document.getElementById("tw-counts");
    const elStatus = document.getElementById("tw-status");
    let busy = false;
    async function refreshLive() {
      if (busy) return;
      busy = true;
      try {
        const r = await fetch(`/api/live/${port.id}`);
        const d = await r.json();
        clockOffset = d.server_now * 1000 - Date.now();
        delayMs = d.delay * 1000;
        if (cfg.map.hasLayer(cfg.aircraft)) {
          syncLayer(cfg.aircraft, acMarkers, d.aircraft, {
            size: 20, rotation: (a) => a.hdg ?? 0, color: altColor, shape: (a) => (a.heli ? HELI : PLANE),
            tip: acTip, pop: acPop });
        }
        if (cfg.map.hasLayer(cfg.vessels) && d.mode.ships) {
          syncLayer(cfg.vessels, vsMarkers, d.vessels, {
            size: 16, rotation: (v) => v.hdg ?? v.cog ?? 0, color: (v) => shipClass(v.type).color,
            shape: (v) => ((v.sog ?? 0) < 0.5 ? MOORED : SHIP), tip: vsTip, pop: vsPop });
        }
        const moving = d.vessels.filter((v) => (v.sog ?? 0) >= 0.5).length;
        elCounts.innerHTML = `<span>✈ <b>${d.aircraft.length}</b> aircraft</span>` + (d.mode.ships
          ? `<span>⚓ <b>${d.vessels.length}</b> vessels <span class="tw-muted">(${moving} moving)</span></span>`
          : `<span class="tw-muted">⚓ Ships need a free <a href="/setup">AISStream key</a></span>`);
        renderMode(d.mode);
        renderStatus(d.status);
        dash.onLive(d);
      } catch (e) {
        elStatus.innerHTML = `<span class="tw-warn">Local server unreachable: ${esc(e.message)}</span>`;
      } finally {
        busy = false;
      }
    }

    // No-key mode: say how old the aircraft snapshot is and how often it updates.
    const elMode = document.getElementById("tw-mode");
    const fmtAsOf = new Intl.DateTimeFormat("en-GB", { timeZone: port.tz, hour: "2-digit", minute: "2-digit", hour12: false });
    let shipsLayerMarked = false;
    function renderMode(m) {
      if (m.anonymous) {
        const every = m.interval_s >= 3600 ? (m.interval_s === 3600 ? "hourly" : `every ${m.interval_s / 3600} h`)
          : `every ${Math.round(m.interval_s / 60)} min`;
        const zone = elSub.textContent.split(" · ")[1] || "";
        elMode.textContent = m.snapshot_ts
          ? `Aircraft as of ${fmtAsOf.format(new Date(m.snapshot_ts * 1000))} ${zone} · no-key mode, updates ${every}`
          : `No-key mode: first aircraft snapshot loading (updates ${every})`;
        elMode.hidden = false;
      } else {
        elMode.hidden = true;
      }
      if (!m.ships && !shipsLayerMarked) {
        // Mark the ship toggle in the layer list so it's clear why it stays empty.
        for (const label of document.querySelectorAll(".leaflet-control-layers-overlays label")) {
          if (label.textContent.includes("Maritime traffic")) {
            const input = label.querySelector("input");
            if (input) input.disabled = true;
            const note = document.createElement("span");
            note.className = "tw-muted";
            note.textContent = " (needs AISStream key)";
            label.querySelector("span").appendChild(note);
          }
        }
        shipsLayerMarked = true;
      }
    }

    function renderStatus(s) {
      const os = s.opensky, ais = s.ais, wx = s.wx;
      const parts = [];
      if (os.anonymous) {
        const next = os.group_age == null ? null : Math.max(0, Math.round((os.interval_s - os.group_age) / 60));
        parts.push(`OpenSky (no key): ${os.group_age == null ? "waiting for first snapshot" : `next update in ${next} min`}` +
          ` · ${os.credits_used_today}/${os.credits_daily} requests today` +
          (os.error ? ` <span class="tw-warn">(${esc(os.error)})</span>` : ""));
      } else {
        parts.push(`OpenSky: ${os.group_age == null ? "waiting for first poll" : `polled ${os.group_age}s ago`}` +
          ` · ${os.credits_used_today}/${os.credits_daily} credits today` +
          (os.error ? ` <span class="tw-warn">(${esc(os.error)})</span>` : ""));
      }
      if (!ais.enabled) {
        parts.push(`AIS: off (no key)`);
      } else {
        parts.push(`AIS: ${ais.connected ? `connected · ${ais.msgs_per_min} msg/min` : '<span class="tw-warn">reconnecting</span>'}` +
          (ais.error && !ais.connected ? ` <span class="tw-warn">(${esc(ais.error)})</span>` : ""));
      }
      parts.push(`Weather: ${wx.last_update_age == null ? "loading" : `updated ${Math.round(wx.last_update_age / 60)} min ago`}` +
        (wx.error ? ` <span class="tw-warn">(${esc(wx.error)})</span>` : ""));
      elStatus.innerHTML = parts.join(" &nbsp;|&nbsp; ");
    }

    // ---- weather panel + layer ----
    const elWx = document.getElementById("tw-wx");
    async function refreshWx() {
      try {
        const d = await (await fetch(`/api/wx/${port.id}`)).json();
        renderPanel(d.panel);
        cfg.weather.clearLayers();
        for (const m of d.metars) {
          L.circleMarker([m.lat, m.lon], { radius: 6, color: "#000", weight: 1, fillOpacity: 0.95,
            fillColor: PALETTE.flightCat[m.cat] || "#9aa5b8" })
            .bindTooltip(`<b>${esc(m.id)}</b> ${esc(m.name || "")}<br>${m.cat ? `<span class="tw-cat tw-cat-${esc(m.cat)}">${esc(m.cat)}</span> ` : ""}` +
              `${esc(m.cond)} · ${m.temp == null ? "" : Math.round(m.temp * 9 / 5 + 32) + "°F · "}${esc(m.wind)} · vis ${esc(m.vis)}<br>` +
              `<span style="opacity:.7;font-family:monospace">${esc(m.raw)}</span>`, { className: "tw-tip" })
            .addTo(cfg.weather);
        }
        for (const p of d.pireps) {
          const icon = L.divIcon({ className: "tw-mk", iconSize: [14, 14], iconAnchor: [7, 7],
            html: TRIANGLE(p.urgent ? PALETTE.pirep.urgent : PALETTE.pirep.normal) });
          L.marker([p.lat, p.lon], { icon })
            .bindTooltip(`<b>${p.urgent ? "URGENT PIREP" : "PIREP"}</b> ${esc(p.ac || "")} FL${esc(p.fl ?? "?")}` +
              `${p.turb ? " · turb " + esc(p.turb) : ""}${p.ice ? " · ice " + esc(p.ice) : ""}<br>` +
              `<span style="opacity:.7;font-family:monospace">${esc(p.raw)}</span>`, { className: "tw-tip" })
            .addTo(cfg.weather);
        }
      } catch (e) {
        elWx.innerHTML = `<div class="tw-warn">Weather unavailable</div>`;
      }
    }

    function renderPanel(p) {
      let h = "";
      if (p.now) {
        const cat = p.now.cat ? ` <span class="tw-cat tw-cat-${esc(p.now.cat)}">${esc(p.now.cat)}</span>` : "";
        h += `<div class="tw-wx-block"><div class="tw-wx-h">Now · ${esc(p.station)}${cat}</div><dl class="tw-wx-grid">` +
          `<dt>Temp</dt><dd>${esc(p.now.temp)}</dd><dt>Wind</dt><dd>${esc(p.now.wind)}</dd>` +
          `<dt>Sky</dt><dd>${esc(p.now.cond)}</dd><dt>Visibility</dt><dd>${esc(p.now.vis)}</dd></dl></div>`;
      }
      if (p.forecast && p.forecast.length) {
        h += `<div class="tw-wx-block"><div class="tw-wx-h">Today's forecast (${esc(p.forecast_src || "NWS")})</div><dl class="tw-wx-grid">`;
        for (const f of p.forecast) {
          h += `<dt>${esc(f.name)}</dt><dd>${esc(f.temp)} · ${esc(f.wind)}<br>${esc(f.cond)}</dd>`;
        }
        if (p.min_vis) h += `<dt>Min. visibility</dt><dd>${esc(p.min_vis)} <span class="tw-muted">(${esc(p.min_vis_src)})</span></dd>`;
        h += `</dl></div>`;
      }
      elWx.innerHTML = h || `<div class="tw-muted">Loading weather…</div>`;
    }

    // ---- RainViewer radar (outside the NEXRAD area) ----
    let radarLayer = null;
    async function refreshRadar() {
      try {
        const d = await (await fetch("https://api.rainviewer.com/public/weather-maps.json")).json();
        const frames = (d.radar && d.radar.past) || [];
        if (!frames.length) return;
        const url = `${d.host}${frames[frames.length - 1].path}/256/{z}/{x}/{y}/2/1_1.png`;
        if (radarLayer) {
          radarLayer.setUrl(url);
        } else {
          radarLayer = L.tileLayer(url, { opacity: 0.6, maxNativeZoom: 7, maxZoom: 18,
            attribution: 'Radar &copy; <a href="https://www.rainviewer.com" target="_blank" rel="noopener">RainViewer</a>' });
          cfg.radar.addLayer(radarLayer);
        }
      } catch (e) { /* radar is optional; the toggle just stays empty */ }
    }

    refreshLive();
    refreshWx();
    setInterval(refreshLive, LIVE_EVERY);
    setInterval(refreshWx, WX_EVERY);
    if (cfg.radar) {
      refreshRadar();
      setInterval(refreshRadar, RADAR_EVERY);
    }
  };
})();
